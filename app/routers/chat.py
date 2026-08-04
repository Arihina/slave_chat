from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session, session_factory
from ..deps import user_id as get_user_id
from ..models import Conversation, Message
from ..ollama import OllamaClient, OllamaError

router = APIRouter(prefix="/v1/chat/completions", tags=["chat"])
log = logging.getLogger(__name__)

DEFAULT_MODEL = "chat"
TITLE_MAX_LEN = 80


def _extract(body: dict) -> tuple[str, list[dict], str, bool, str | None]:
    model = body.get("model") or DEFAULT_MODEL
    messages = body.get("messages")
    stream = body.get("stream", False)
    conversation_id_raw = body.get("conversation_id")

    if not isinstance(messages, list) or not messages:
        raise HTTPException(422, "messages обязателен и не должен быть пустым")

    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        raise HTTPException(
            422, 'последнее сообщение должно иметь role="user"')

    question = str(last.get("content", "")).strip()
    if not question:
        raise HTTPException(422, "Пустое сообщение")

    history = [
        {"role": m["role"], "content": m["content"]}
        for m in messages[:-1]
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    ]

    return model, history, question, bool(stream), conversation_id_raw


async def _open_turn(uid: UUID, conversation_id: UUID | None, question: str) -> None:
    """Сохраняет сообщение пользователя ДО начала генерации (как и раньше —
    так оно не теряется, даже если генерация оборвётся или упадёт), заодно
    проверяет владение conversation_id и обновляет updated_at/title."""
    factory = session_factory()
    async with factory() as s, s.begin():
        conv = None
        if conversation_id is not None:
            conv = await s.get(Conversation, conversation_id)
            if conv is None or conv.user_id != uid:
                raise HTTPException(404, "Чат (conversation_id) не найден")

        s.add(Message(user_id=uid, conversation_id=conversation_id,
                      role="user", content=question))

        if conv is not None:
            conv.updated_at = datetime.now(timezone.utc)
            if conv.title is None:
                conv.title = question[:TITLE_MAX_LEN] + \
                    ("…" if len(question) > TITLE_MAX_LEN else "")


async def _persist_assistant(
    uid: UUID, conversation_id: UUID | None, assistant_id: UUID, content: str,
    model: str, prompt_tokens: int | None, completion_tokens: int | None,
) -> None:
    if not content:
        return
    factory = session_factory()
    async with factory() as s, s.begin():
        s.add(Message(
            id=assistant_id, user_id=uid, conversation_id=conversation_id,
            role="assistant", content=content, model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        ))


@router.post("")
async def chat_completions(
    request: Request,
    body: dict = Body(...),
    uid: UUID = Depends(get_user_id),
):
    model, history, question, stream, conversation_id_raw = _extract(body)

    conversation_id: UUID | None = None
    if conversation_id_raw is not None:
        try:
            conversation_id = UUID(str(conversation_id_raw))
        except ValueError:
            raise HTTPException(422, "conversation_id должен быть UUID")

    await _open_turn(uid, conversation_id, question)

    assistant_id = uuid4()
    completion_id = f"chatcmpl-{assistant_id}"
    created = int(time.time())
    conversation_id_str = str(conversation_id) if conversation_id else None

    messages_payload = [
        {"role": "system", "content": settings.system_prompt},
        *history,
        {"role": "user", "content": question},
    ]

    ollama: OllamaClient = request.app.state.ollama

    if not stream:
        buf: list[str] = []
        prompt_tokens = completion_tokens = 0
        try:
            async for chunk in ollama.stream_chat(messages_payload, settings.ollama_model):
                delta = (chunk.get("message") or {}).get("content", "")
                if delta:
                    buf.append(delta)
                if chunk.get("done"):
                    prompt_tokens = chunk.get("prompt_eval_count", 0) or 0
                    completion_tokens = chunk.get("eval_count", 0) or 0
        except OllamaError as e:
            raise HTTPException(502, f"Ошибка LLM: {e}")

        full_answer = "".join(buf)

        await _persist_assistant(uid, conversation_id, assistant_id, full_answer,
                                 model, prompt_tokens, completion_tokens)

        usage = {
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "conversation_id": conversation_id_str,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": full_answer},
                "finish_reason": "stop",
            }],
            "usage": usage,
        }

    async def _agen():
        buf: list[str] = []

        try:
            first_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model, "conversation_id": conversation_id_str,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n".encode()

            async for chunk in ollama.stream_chat(messages_payload, settings.ollama_model):
                delta = (chunk.get("message") or {}).get("content", "")
                if delta:
                    buf.append(delta)
                    piece = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model, "conversation_id": conversation_id_str,
                        "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(piece, ensure_ascii=False)}\n\n".encode()

                if chunk.get("done"):
                    prompt_tokens = chunk.get("prompt_eval_count", 0) or 0
                    completion_tokens = chunk.get("eval_count", 0) or 0

                    full_answer = "".join(buf)
                    if full_answer:
                        src_chunk = {
                            "id": completion_id, "object": "chat.completion.chunk",
                            "created": created, "model": model, "conversation_id": conversation_id_str,
                            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(src_chunk, ensure_ascii=False)}\n\n".encode()

                    final_chunk = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model, "conversation_id": conversation_id_str,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode()
                    yield b"data: [DONE]\n\n"

                    await _persist_assistant(uid, conversation_id, assistant_id, full_answer,
                                             model, prompt_tokens, completion_tokens)
                    return

        except asyncio.CancelledError:
            partial = "".join(buf)
            if partial:
                try:
                    await _persist_assistant(uid, conversation_id, assistant_id, partial,
                                             model, None, None)
                except Exception:
                    log.exception("не удалось сохранить частичный ответ")
            raise

        except Exception as e:
            log.exception("стрим ошибка")
            err = {"error": {
                "message": str(e), "type": "server_error", "param": None, "code": None,
            }}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()

    return StreamingResponse(
        _agen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{completion_id}")
async def get_completion(
    completion_id: str,
    uid: UUID = Depends(get_user_id),
    s: AsyncSession = Depends(get_session),
):
    raw = completion_id.removeprefix("chatcmpl-")
    try:
        message_id = UUID(raw)
    except ValueError:
        raise HTTPException(422, "Некорректный id completion'а")

    msg = await s.scalar(
        select(Message).where(Message.id == message_id, Message.user_id == uid)
    )
    if msg is None or msg.role != "assistant":
        raise HTTPException(404, "Completion не найден")

    prompt_tokens = msg.prompt_tokens or 0
    completion_tokens = msg.completion_tokens or 0

    return {
        "id": f"chatcmpl-{msg.id}",
        "object": "chat.completion",
        "created": int(msg.created_at.timestamp()),
        "model": msg.model,
        "conversation_id": str(msg.conversation_id) if msg.conversation_id else None,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": msg.content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
