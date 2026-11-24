from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request


async def user_id(request: Request) -> UUID:
    """Единственная точка получения user_id в бизнес-коде.

    Значение кладёт InternalAuthMiddleware в `request.state` после проверки
    внутреннего credential; из заголовка здесь ничего не читается, чтобы новую
    ручку нельзя было случайно оставить без проверки.
    """
    uid = getattr(request.state, "user_id", None)
    if uid is None:
        raise HTTPException(401, "Не передан идентификатор пользователя")
    return uid
