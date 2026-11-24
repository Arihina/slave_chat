"""Внутренняя аутентификация master -> агент и request-контекст.

Проверяет `X-Internal-Token` (per-agent секрет), валидирует `X-User-Id` как
UUID, кладёт `user_id` и `request_id` в `request.state` и ведёт access-лог с
корреляцией по `X-Request-Id`.

Реализовано чистым ASGI-middleware (без `BaseHTTPMiddleware`): тот оборачивает
ответ в дополнительную задачу и ломает потоковые SSE-ответы
(`/v1/chat/completions`, `/v1/responses`). Здесь тело не буферизуется и не
трогается.

Инварианты (docs/agent-side-changes.md):
  * агент доверяет `X-User-Id` только после проверки внутреннего credential;
  * публичные пути — точное совпадение, не `startswith`;
  * секреты не попадают в логи;
  * роли не проверяются — это ответственность master.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
import uuid
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("chat.internal_auth")

INTERNAL_TOKEN_HEADER = "X-Internal-Token"
USER_ID_HEADER = "X-User-Id"
REQUEST_ID_HEADER = "X-Request-Id"

# Значение X-Request-Id принимается от nginx/master только в этом виде.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# Минимальная длина токена для предупреждения в dev (в prod master fail-closed).
_MIN_TOKEN_LENGTH = 32


def new_request_id() -> str:
    return uuid.uuid4().hex


def normalize_request_id(value: str | None) -> str:
    """Принять валидный X-Request-Id или сгенерировать новый.

    Некорректный id — не повод отдавать 400: это диагностический заголовок,
    а не часть аутентификации, но пропускать его в логи как есть нельзя.
    """
    if value and _REQUEST_ID_RE.match(value):
        return value
    return new_request_id()


def _error_response(status: int, message: str) -> JSONResponse:
    """Ошибка в едином с master формате."""
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": "authentication_error",
                "param": None,
                "code": None,
            }
        },
    )


def _constant_time_equals(a: str | None, b: str | None) -> bool:
    """Сравнение без раннего выхода по длине.

    `hmac.compare_digest` возвращает False сразу при разной длине; чтобы не
    давать подсказку о длине секрета, сравниваем SHA-256-дайджесты (фикс.
    длина) через `compare_digest`.
    """
    da = hashlib.sha256((a or "").encode()).digest()
    db = hashlib.sha256((b or "").encode()).digest()
    return hmac.compare_digest(da, db)


def _safe_log_value(value: object, limit: int = 512) -> str:
    """Убрать из значения для лога управляющие символы."""
    text = str(value)[:limit]
    return "".join(ch if ch.isprintable() else "?" for ch in text)


class InternalAuthMiddleware:
    """Проверка внутреннего credential + request_id + access-лог."""

    def __init__(self, app: ASGIApp, settings: Any) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        headers = Headers(scope=scope)
        request_id = normalize_request_id(headers.get(REQUEST_ID_HEADER))

        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        started = time.perf_counter()
        status = 500
        user_id: str = "-"

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            if path not in self.settings.public_paths:
                response = self._check(headers, state)
                if response is not None:
                    await response(scope, receive, send_with_request_id)
                    return
            uid = state.get("user_id")
            user_id = str(uid) if uid else "-"
            await self.app(scope, receive, send_with_request_id)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            log.info(
                "request_id=%s agent_id=%s method=%s path=%s status=%d "
                "user_id=%s duration_ms=%.1f",
                request_id,
                self.settings.agent_id,
                _safe_log_value(scope.get("method", "")),
                _safe_log_value(path),
                status,
                user_id,
                duration_ms,
            )

    def _check(self, headers: Headers, state: dict) -> JSONResponse | None:
        """Проверить credential и заполнить state.user_id.

        Возвращает 401-ответ при ошибке или None, если запрос можно
        пропустить дальше.
        """
        if self.settings.internal_auth_mode != "off":
            expected = self.settings.master_internal_token
            if self.settings.internal_auth_mode == "token":
                if not expected:
                    # fail-closed должен не допустить старта; это страховка.
                    return _error_response(500, "Внутренний токен не настроен")
                if not _constant_time_equals(
                    headers.get(INTERNAL_TOKEN_HEADER), expected
                ):
                    return _error_response(401, "Требуется внутренний токен master")
            elif self.settings.internal_auth_mode == "assertion":
                # Этап 2 реализуется отдельно (PyJWT/cryptography).
                return _error_response(
                    501, "Режим assertion ещё не реализован на агенте"
                )

        raw_user = headers.get(USER_ID_HEADER)
        try:
            uid = uuid.UUID(raw_user) if raw_user else None
        except ValueError:
            uid = None
        if uid is None:
            return _error_response(
                401, "Некорректный идентификатор пользователя (ожидается UUID)"
            )

        state["user_id"] = uid
        return None


def install_internal_auth(app: ASGIApp, settings: Any) -> None:
    """Подключить middleware к приложению. Вызывать до включения роутеров."""
    app.add_middleware(InternalAuthMiddleware, settings=settings)
