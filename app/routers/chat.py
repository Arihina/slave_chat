from __future__ import annotations

import asyncio
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
from ..openai_format import (
    DONE, chat_usage, chunk, completion_object,
)
from ..openai_request import (
    CHAT_TEXT_TYPES, DIALOG_ROLES, collect_instructions, extract_text,
    sampling_options,
)

router = APIRouter(prefix="/v1/chat/completions", tags=["chat"])
log = logging.getLogger(__name__)

DEFAULT_MODEL = "chat"
TITLE_MAX_LEN = 80


def _extract(body: dict) -> dict:
    model = body.get("model") or DEFAULT_MODEL
    messages = body.get("messages")

    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "messages обязателен и не должен быть пустым")

    n = body.get("n")
    if n is not None and n != 1:
        raise HTTPException(
            400, "Поддерживается только n=1: сервис возвращает один вариант ответа")

    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        raise HTTPException(
            400, 'последнее сообщение должно иметь role="user"')

    question = extract_text(last.get("content", ""), CHAT_TEXT_TYPES).strip()
    if not question:
        raise HTTPException(400, "Пустое сообщение")

    history = [
        {"role": m["role"],
         "content": extract_text(m.get("content", ""), CHAT_TEXT_TYPES)}
        for m in messages[:-1]
        if isinstance(m, dict) and m.get("role") in DIALOG_ROLES
    ]

    stream_options = body.get("stream_options") or {}
    if not isinstance(stream_options, dict):
        raise HTTPException(400, "stream_options должен быть объектом")

    return {
        "model": model,
        "history": history,
        "question": question,
        "instructions": collect_instructions(messages, CHAT_TEXT_TYPES),
        "stream": bool(body.get("stream", False)),
        "include_usage": bool(stream_options.get("include_usage", False)),
        "store": bool(body.get("store", True)),
        "conversation_id_raw": body.get("conversation_id"),
        "options": sampling_options(
            temperature=body.get("temperature"),
            top_p=body.get("top_p"),
            max_tokens=body.get("max_completion_tokens",
                                body.get("max_tokens")),
        ),
    }


def _system_prompt(instructions: str | None) -> str:
    if not instructions:
        return settings.system_prompt
    return (f"{settings.system_prompt}\n\n"
            f"Дополнительные инструкции пользователя:\n{instructions}")


async def _open_turn(
    uid: UUID, conversation_id: UUID | None, question: str, store: bool,
) -> None:
    factory = session_factory()
    async with factory() as s, s.begin():
        conv = None
        if conversation_id is not None:
            conv = await s.get(Conversation, conversation_id)
            if conv is None or conv.user_id != uid:
                raise HTTPException(404, "Чат (conversation_id) не найден")

        if not store:
            return

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
    req = _extract(body)
    model = req["model"]

    conversation_id: UUID | None = None
    if req["conversation_id_raw"] is not None:
        try:
            conversation_id = UUID(str(req["conversation_id_raw"]))
        except ValueError:
            raise HTTPException(400, "conversation_id должен быть UUID")

    await _open_turn(uid, conversation_id, req["question"], req["store"])

    assistant_id = uuid4()
    completion_id = f"chatcmpl-{assistant_id}"
    created = int(time.time())
    conversation_id_str = str(conversation_id) if conversation_id else None

    messages_payload = [
        {"role": "system", "content": _system_prompt(req["instructions"])},
        *req["history"],
        {"role": "user", "content": req["question"]},
    ]

    ollama: OllamaClient = request.app.state.ollama

    if not req["stream"]:
        buf: list[str] = []
        prompt_tokens = completion_tokens = 0
        try:
            async for piece in ollama.stream_chat(
                messages_payload, settings.ollama_model, req["options"]
            ):
                delta = (piece.get("message") or {}).get("content", "")
                if delta:
                    buf.append(delta)
                if piece.get("done"):
                    prompt_tokens = piece.get("prompt_eval_count", 0) or 0
                    completion_tokens = piece.get("eval_count", 0) or 0
        except OllamaError as e:
            raise HTTPException(502, f"Ошибка LLM: {e}")

        full_answer = "".join(buf)

        if req["store"]:
            await _persist_assistant(uid, conversation_id, assistant_id, full_answer,
                                     model, prompt_tokens, completion_tokens)

        return completion_object(
            completion_id, created, model, conversation_id_str, full_answer,
            chat_usage(prompt_tokens, completion_tokens),
        )

    async def _agen():
        buf: list[str] = []
        prompt_tokens = completion_tokens = 0

        try:
            yield chunk(completion_id, created, model, conversation_id_str,
                        {"role": "assistant", "content": ""})

            async for piece in ollama.stream_chat(
                messages_payload, settings.ollama_model, req["options"]
            ):
                delta = (piece.get("message") or {}).get("content", "")
                if delta:
                    buf.append(delta)
                    yield chunk(completion_id, created, model,
                                conversation_id_str, {"content": delta})

                if piece.get("done"):
                    prompt_tokens = piece.get("prompt_eval_count", 0) or 0
                    completion_tokens = piece.get("eval_count", 0) or 0
                    break

            yield chunk(completion_id, created, model, conversation_id_str,
                        {}, finish_reason="stop")

            if req["include_usage"]:
                yield chunk(completion_id, created, model, conversation_id_str,
                            None, usage=chat_usage(prompt_tokens, completion_tokens))

            yield DONE

            if req["store"]:
                await _persist_assistant(uid, conversation_id, assistant_id,
                                         "".join(buf), model,
                                         prompt_tokens, completion_tokens)

        except asyncio.CancelledError:
            partial = "".join(buf)
            if partial and req["store"]:
                try:
                    await _persist_assistant(uid, conversation_id, assistant_id,
                                             partial, model, None, None)
                except Exception:
                    log.exception("не удалось сохранить частичный ответ")
            raise

        except Exception as e:
            log.exception("стрим ошибка")
            yield chunk(completion_id, created, model, conversation_id_str,
                        {}, finish_reason="stop")
            yield DONE
            del e

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
        raise HTTPException(400, "Некорректный id completion'а")

    msg = await s.scalar(
        select(Message).where(Message.id == message_id, Message.user_id == uid)
    )
    if msg is None or msg.role != "assistant":
        raise HTTPException(404, "Completion не найден")

    return completion_object(
        f"chatcmpl-{msg.id}", int(msg.created_at.timestamp()), msg.model,
        str(msg.conversation_id) if msg.conversation_id else None,
        msg.content, chat_usage(msg.prompt_tokens, msg.completion_tokens),
    )


@router.delete("/{completion_id}")
async def delete_completion(
    completion_id: str,
    uid: UUID = Depends(get_user_id),
    s: AsyncSession = Depends(get_session),
):
    raw = completion_id.removeprefix("chatcmpl-")
    try:
        message_id = UUID(raw)
    except ValueError:
        raise HTTPException(400, "Некорректный id completion'а")

    msg = await s.scalar(
        select(Message).where(Message.id == message_id, Message.user_id == uid)
    )
    if msg is None or msg.role != "assistant":
        raise HTTPException(404, "Completion не найден")

    await s.delete(msg)
    await s.commit()

    return {"id": f"chatcmpl-{message_id}",
            "object": "chat.completion.deleted", "deleted": True}
