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
from ..context import build_window, message_tokens
from ..db import get_session, session_factory
from ..deps import user_id as get_user_id
from ..models import Conversation, Message
from ..ollama import OllamaClient, OllamaError
from ..openai_format import (
    message_item, response_object, responses_usage, sse_event, text_part,
)
from ..openai_request import (
    DIALOG_ROLES, RESPONSES_TEXT_TYPES, collect_instructions, extract_text,
    sampling_options,
)

router = APIRouter(prefix="/v1/responses", tags=["responses"])
log = logging.getLogger(__name__)

DEFAULT_MODEL = "chat"
TITLE_MAX_LEN = 80


def _conversation_field(body: dict):
    conversation = body.get("conversation")

    if isinstance(conversation, dict):
        return conversation.get("id")
    if conversation is not None:
        return conversation

    return body.get("conversation_id")


def _extract(body: dict) -> dict:
    model = body.get("model") or DEFAULT_MODEL
    input_data = body.get("input")

    common = {
        "model": model,
        "stream": bool(body.get("stream", False)),
        "store": bool(body.get("store", True)),
        "conversation_raw": _conversation_field(body),
        "previous_response_id": body.get("previous_response_id"),
        "instructions": body.get("instructions"),
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "max_output_tokens": body.get("max_output_tokens"),
        "metadata": body.get("metadata") or {},
        "options": sampling_options(
            temperature=body.get("temperature"),
            top_p=body.get("top_p"),
            max_tokens=body.get("max_output_tokens"),
            num_ctx=settings.context_window,
        ),
    }

    if input_data is None:
        raise HTTPException(400, "input обязателен")

    if isinstance(input_data, str):
        question = input_data.strip()
        if not question:
            raise HTTPException(400, "Пустой input")
        return {**common, "history": [], "question": question}

    if not isinstance(input_data, list) or not input_data:
        raise HTTPException(
            400, "input должен быть строкой или непустым списком items")

    last = input_data[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        raise HTTPException(
            400, 'последний item в input должен иметь role="user"')

    question = extract_text(last.get("content"), RESPONSES_TEXT_TYPES).strip()
    if not question:
        raise HTTPException(400, "Пустой вопрос")

    items = [m for m in input_data[:-1]
             if isinstance(m, dict) and m.get("type", "message") == "message"]

    inline = collect_instructions(items, RESPONSES_TEXT_TYPES)
    if inline:
        common["instructions"] = "\n".join(
            filter(None, [common["instructions"], inline]))

    history = [
        {"role": m["role"],
         "content": extract_text(m.get("content"), RESPONSES_TEXT_TYPES)}
        for m in items if m.get("role") in DIALOG_ROLES
    ]

    return {**common, "history": history, "question": question}


def _system_prompt(instructions: str | None) -> str:
    if not instructions:
        return settings.system_prompt
    return (f"{settings.system_prompt}\n\n"
            f"Дополнительные инструкции пользователя:\n{instructions}")


def _parse_message_id(raw: str) -> UUID:
    value = raw
    for prefix in ("resp_", "chatcmpl-"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    try:
        return UUID(value)
    except ValueError:
        raise HTTPException(400, "Некорректный id response'а")


async def _resolve_conversation(
    s: AsyncSession, uid: UUID, req: dict,
) -> UUID | None:
    raw = req["conversation_raw"]

    if raw is None and req["previous_response_id"]:
        prev = await s.scalar(
            select(Message).where(
                Message.id == _parse_message_id(req["previous_response_id"]),
                Message.user_id == uid,
            )
        )
        if prev is None:
            raise HTTPException(404, "previous_response_id не найден")
        return prev.conversation_id

    if raw is None:
        return None

    try:
        return UUID(str(raw))
    except ValueError:
        raise HTTPException(400, "conversation должен быть UUID")


async def _open_turn_and_load_history(
    uid: UUID, conversation_id: UUID | None, question: str,
    need_history: bool, store: bool, question_tokens: int | None = None,
) -> list[dict]:
    factory = session_factory()
    history: list[dict] = []

    async with factory() as s, s.begin():
        conv = None
        if conversation_id is not None:
            conv = await s.get(Conversation, conversation_id)
            if conv is None or conv.user_id != uid:
                raise HTTPException(404, "Чат (conversation) не найден")

            if need_history:
                rows = (await s.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at.desc())
                    .limit(settings.history_max_messages)
                )).scalars().all()
                history = [{"role": m.role, "content": m.content,
                            "tokens": m.tokens}
                           for m in reversed(rows)]

        if not store:
            return history

        s.add(Message(user_id=uid, conversation_id=conversation_id,
                      role="user", content=question, tokens=question_tokens))

        if conv is not None:
            conv.updated_at = datetime.now(timezone.utc)
            if conv.title is None:
                conv.title = question[:TITLE_MAX_LEN] + \
                    ("…" if len(question) > TITLE_MAX_LEN else "")

    return history


async def _persist_assistant(
    uid: UUID, conversation_id: UUID | None, assistant_id: UUID, content: str,
    model: str, prompt_tokens: int | None, completion_tokens: int | None,
    tokens: int | None = None,
) -> None:
    if not content:
        return
    factory = session_factory()
    async with factory() as s, s.begin():
        s.add(Message(
            id=assistant_id, user_id=uid, conversation_id=conversation_id,
            role="assistant", content=content, model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            tokens=tokens,
        ))


@router.post("")
async def create_response(
    request: Request,
    body: dict = Body(...),
    uid: UUID = Depends(get_user_id),
    s: AsyncSession = Depends(get_session),
):
    req = _extract(body)
    model = req["model"]

    conversation_id = await _resolve_conversation(s, uid, req)

    if conversation_id is not None and req["history"]:
        raise HTTPException(
            400,
            "При переданном conversation input должен содержать только новый "
            "ход (без истории) — история собирается агентом из БД",
        )

    counter = request.app.state.counter

    db_history = await _open_turn_and_load_history(
        uid, conversation_id, req["question"],
        need_history=conversation_id is not None, store=req["store"],
        question_tokens=message_tokens("user", req["question"], counter),
    )
    history = db_history if conversation_id is not None else req["history"]

    assistant_id = uuid4()
    response_id = f"resp_{assistant_id}"
    item_id = f"msg_{assistant_id}"
    created = int(time.time())
    conversation_id_str = str(conversation_id) if conversation_id else None

    messages_payload, stats = build_window(
        system=_system_prompt(req["instructions"]),
        history=history,
        question=req["question"],
        counter=counter,
        num_ctx=settings.context_window,
        reserve_output=(req["max_output_tokens"]
                        or settings.reserve_output_tokens),
        safety=settings.context_safety_tokens,
    )
    log.info("окно контекста: %s", stats.as_log())

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
            await _persist_assistant(
                uid, conversation_id, assistant_id, full_answer, model,
                prompt_tokens, completion_tokens,
                tokens=message_tokens("assistant", full_answer, counter),
            )

        return response_object(
            response_id, created, model, conversation_id_str, "completed",
            output=[message_item(item_id, full_answer, "completed")],
            usage=responses_usage(prompt_tokens, completion_tokens), req=req,
        )

    async def _agen():
        buf: list[str] = []
        seq = 0

        def _next():
            nonlocal seq
            seq += 1
            return seq

        try:
            yield sse_event(
                _next(), "response.created",
                response=response_object(
                    response_id, created, model, conversation_id_str,
                    "in_progress", output=[], req=req),
            )
            yield sse_event(
                _next(), "response.in_progress",
                response=response_object(
                    response_id, created, model, conversation_id_str,
                    "in_progress", output=[], req=req),
            )
            yield sse_event(
                _next(), "response.output_item.added",
                output_index=0, item=message_item(item_id, "", "in_progress"),
            )
            yield sse_event(
                _next(), "response.content_part.added",
                item_id=item_id, output_index=0, content_index=0,
                part=text_part(""),
            )

            prompt_tokens = completion_tokens = 0
            async for piece in ollama.stream_chat(
                messages_payload, settings.ollama_model, req["options"]
            ):
                delta = (piece.get("message") or {}).get("content", "")
                if delta:
                    buf.append(delta)
                    yield sse_event(
                        _next(), "response.output_text.delta",
                        item_id=item_id, output_index=0, content_index=0,
                        delta=delta, logprobs=[],
                    )

                if piece.get("done"):
                    prompt_tokens = piece.get("prompt_eval_count", 0) or 0
                    completion_tokens = piece.get("eval_count", 0) or 0
                    break

            full_answer = "".join(buf)

            yield sse_event(
                _next(), "response.output_text.done",
                item_id=item_id, output_index=0, content_index=0,
                text=full_answer, logprobs=[],
            )
            yield sse_event(
                _next(), "response.content_part.done",
                item_id=item_id, output_index=0, content_index=0,
                part=text_part(full_answer),
            )

            final_item = message_item(item_id, full_answer, "completed")
            yield sse_event(_next(), "response.output_item.done",
                            output_index=0, item=final_item)

            yield sse_event(
                _next(), "response.completed",
                response=response_object(
                    response_id, created, model, conversation_id_str,
                    "completed", output=[final_item],
                    usage=responses_usage(prompt_tokens, completion_tokens),
                    req=req),
            )

            if req["store"]:
                await _persist_assistant(
                    uid, conversation_id, assistant_id, full_answer, model,
                    prompt_tokens, completion_tokens,
                    tokens=message_tokens("assistant", full_answer, counter),
                )

        except asyncio.CancelledError:
            partial = "".join(buf)
            if partial and req["store"]:
                try:
                    await _persist_assistant(
                        uid, conversation_id, assistant_id, partial, model,
                        None, None,
                        tokens=message_tokens("assistant", partial, counter),
                    )
                except Exception:
                    log.exception("не удалось сохранить частичный ответ")
            raise

        except Exception as e:
            log.exception("стрим ошибка")
            yield sse_event(_next(), "error",
                            message=str(e), code=None, param=None)

    return StreamingResponse(
        _agen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{response_id}")
async def get_response(
    response_id: str,
    uid: UUID = Depends(get_user_id),
    s: AsyncSession = Depends(get_session),
):
    message_id = _parse_message_id(response_id)
    msg = await s.scalar(
        select(Message).where(Message.id == message_id, Message.user_id == uid)
    )
    if msg is None or msg.role != "assistant":
        raise HTTPException(404, "Response не найден")

    return response_object(
        f"resp_{msg.id}", int(msg.created_at.timestamp()), msg.model,
        str(msg.conversation_id) if msg.conversation_id else None,
        "completed",
        output=[message_item(f"msg_{msg.id}", msg.content, "completed")],
        usage=responses_usage(msg.prompt_tokens, msg.completion_tokens),
    )


@router.delete("/{response_id}")
async def delete_response(
    response_id: str,
    uid: UUID = Depends(get_user_id),
    s: AsyncSession = Depends(get_session),
):
    message_id = _parse_message_id(response_id)
    msg = await s.scalar(
        select(Message).where(Message.id == message_id, Message.user_id == uid)
    )
    if msg is None or msg.role != "assistant":
        raise HTTPException(404, "Response не найден")

    await s.delete(msg)
    await s.commit()

    return {"id": f"resp_{message_id}", "object": "response.deleted", "deleted": True}
