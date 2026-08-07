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

router = APIRouter(prefix="/v1/responses", tags=["responses"])
log = logging.getLogger(__name__)

DEFAULT_MODEL = "chat"
TITLE_MAX_LEN = 80
SOURCES_FOOTER = ""


def _parse_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text", "")) for p in content
            if isinstance(p, dict) and p.get("type") in ("input_text", "output_text")
        )
    return ""


def _extract(body: dict) -> tuple[str, list[dict], str, bool, str | None]:
    model = body.get("model") or DEFAULT_MODEL
    input_data = body.get("input")
    stream = body.get("stream", False)
    conversation_id_raw = body.get("conversation_id")

    if input_data is None:
        raise HTTPException(422, "input обязателен")

    if isinstance(input_data, str):
        question = input_data.strip()
        if not question:
            raise HTTPException(422, "Пустой input")
        return model, [], question, bool(stream), conversation_id_raw

    if not isinstance(input_data, list) or not input_data:
        raise HTTPException(
            422, "input должен быть строкой или непустым списком items")

    last = input_data[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        raise HTTPException(
            422, 'последний item в input должен иметь role="user"')

    question = _parse_content(last.get("content")).strip()
    if not question:
        raise HTTPException(422, "Пустой вопрос")

    history = [
        {"role": m["role"], "content": _parse_content(m.get("content"))}
        for m in input_data[:-1]
        if isinstance(m, dict) and m.get("type", "message") == "message"
        and m.get("role") in ("user", "assistant")
    ]

    return model, history, question, bool(stream), conversation_id_raw


async def _open_turn_and_load_history(
    uid: UUID, conversation_id: UUID | None, question: str, need_history: bool,
) -> list[dict]:
    factory = session_factory()
    history: list[dict] = []
    async with factory() as s, s.begin():
        conv = None
        if conversation_id is not None:
            conv = await s.get(Conversation, conversation_id)
            if conv is None or conv.user_id != uid:
                raise HTTPException(404, "Чат (conversation_id) не найден")

            if need_history:
                rows = (await s.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at.desc())
                    .limit(settings.history_limit)
                )).scalars().all()
                history = [{"role": m.role, "content": m.content}
                           for m in reversed(rows)]

        s.add(Message(user_id=uid, conversation_id=conversation_id,
                      role="user", content=question))

        if conv is not None:
            conv.updated_at = datetime.now(timezone.utc)
            if conv.title is None:
                conv.title = question[:TITLE_MAX_LEN] + \
                    ("…" if len(question) > TITLE_MAX_LEN else "")

    return history


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


def _usage_out(prompt_tokens: int, completion_tokens: int) -> dict:
    return {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _message_item(item_id: str, text: str, status: str) -> dict:
    return {
        "id": item_id,
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _response_object(
    response_id: str, created: int, model: str, conversation_id_str: str | None,
    status: str, output: list[dict], usage: dict | None = None,
) -> dict:
    obj = {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "model": model,
        "conversation_id": conversation_id_str,  # платформенное расширение
        "output": output,
    }
    if usage is not None:
        obj["usage"] = usage
    return obj


def _sse_event(seq: int, event_type: str, **fields) -> str:
    payload = {"type": event_type, "sequence_number": seq, **fields}
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("")
async def create_response(
    request: Request,
    body: dict = Body(...),
    uid: UUID = Depends(get_user_id),
):
    model, req_history, question, stream, conversation_id_raw = _extract(body)

    conversation_id: UUID | None = None
    if conversation_id_raw is not None:
        try:
            conversation_id = UUID(str(conversation_id_raw))
        except ValueError:
            raise HTTPException(422, "conversation_id должен быть UUID")

    if conversation_id is not None and req_history:
        raise HTTPException(
            422,
            "При переданном conversation_id input должен содержать "
            "только новый ход (без истории) — история собирается "
            "агентом из БД по conversation_id",
        )

    db_history = await _open_turn_and_load_history(
        uid, conversation_id, question, need_history=conversation_id is not None,
    )
    history = db_history if conversation_id is not None else req_history

    assistant_id = uuid4()
    response_id = f"resp_{assistant_id}"
    item_id = f"msg_{assistant_id}"
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
        if full_answer:
            full_answer += SOURCES_FOOTER

        await _persist_assistant(uid, conversation_id, assistant_id, full_answer,
                                 model, prompt_tokens, completion_tokens)

        return _response_object(
            response_id, created, model, conversation_id_str, "completed",
            output=[_message_item(item_id, full_answer, "completed")],
            usage=_usage_out(prompt_tokens, completion_tokens),
        )

    async def _agen():
        buf: list[str] = []
        seq = 0

        def _next():
            nonlocal seq
            seq += 1
            return seq

        try:
            yield _sse_event(
                _next(), "response.created",
                response=_response_object(
                    response_id, created, model, conversation_id_str,
                    "in_progress", output=[]),
            )
            yield _sse_event(
                _next(), "response.output_item.added",
                output_index=0, item=_message_item(item_id, "", "in_progress"),
            )
            yield _sse_event(
                _next(), "response.content_part.added",
                item_id=item_id, output_index=0, content_index=0,
                part={"type": "output_text", "text": "", "annotations": []},
            )

            async for chunk in ollama.stream_chat(messages_payload, settings.ollama_model):
                delta = (chunk.get("message") or {}).get("content", "")
                if delta:
                    buf.append(delta)
                    yield _sse_event(
                        _next(), "response.output_text.delta",
                        item_id=item_id, output_index=0, content_index=0,
                        delta=delta,
                    )

                if chunk.get("done"):
                    prompt_tokens = chunk.get("prompt_eval_count", 0) or 0
                    completion_tokens = chunk.get("eval_count", 0) or 0

                    full_answer = "".join(buf)
                    if full_answer:
                        yield _sse_event(
                            _next(), "response.output_text.delta",
                            item_id=item_id, output_index=0, content_index=0,
                            delta=SOURCES_FOOTER,
                        )
                        full_answer += SOURCES_FOOTER

                    yield _sse_event(
                        _next(), "response.output_text.done",
                        item_id=item_id, output_index=0, content_index=0,
                        text=full_answer,
                    )
                    yield _sse_event(
                        _next(), "response.content_part.done",
                        item_id=item_id, output_index=0, content_index=0,
                        part={"type": "output_text",
                              "text": full_answer, "annotations": []},
                    )

                    final_item = _message_item(
                        item_id, full_answer, "completed")
                    yield _sse_event(_next(), "response.output_item.done",
                                     output_index=0, item=final_item)

                    yield _sse_event(
                        _next(), "response.completed",
                        response=_response_object(
                            response_id, created, model, conversation_id_str,
                            "completed", output=[final_item],
                            usage=_usage_out(prompt_tokens, completion_tokens)),
                    )

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
            yield _sse_event(9999, "error", message=str(e), code=None, param=None)

    return StreamingResponse(
        _agen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _parse_response_id(response_id: str) -> UUID:
    raw = response_id.removeprefix("resp_")
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(422, "Некорректный id response'а")


@router.get("/{response_id}")
async def get_response(
    response_id: str,
    uid: UUID = Depends(get_user_id),
    s: AsyncSession = Depends(get_session),
):
    message_id = _parse_response_id(response_id)
    msg = await s.scalar(
        select(Message).where(Message.id == message_id, Message.user_id == uid)
    )
    if msg is None or msg.role != "assistant":
        raise HTTPException(404, "Response не найден")

    prompt_tokens = msg.prompt_tokens or 0
    completion_tokens = msg.completion_tokens or 0

    return _response_object(
        f"resp_{msg.id}", int(msg.created_at.timestamp()), msg.model,
        str(msg.conversation_id) if msg.conversation_id else None,
        "completed",
        output=[_message_item(f"msg_{msg.id}", msg.content, "completed")],
        usage=_usage_out(prompt_tokens, completion_tokens),
    )
