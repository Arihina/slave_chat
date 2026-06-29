from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from ..config import settings
from ..db import session_factory
from ..deps import user_id
from ..models import ChatSession, Message
from ..ollama import OllamaClient, OllamaError
from ..sse import sse

router = APIRouter(tags=["chat"])
log = logging.getLogger(__name__)


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1)


@router.post("/sessions/{session_id}/chat")
async def chat(
    session_id: UUID,
    body: ChatBody,
    request: Request,
    uid: UUID = Depends(user_id),
):
    # Не используем Depends(get_session): сессия БД нужна и до стрима,
    # и внутри генератора. Держим две короткие транзакции.

    factory = session_factory()

    # --- 1) проверка владения + сохранение user-сообщения + загрузка истории ---
    async with factory() as s, s.begin():
        owns = await s.scalar(
            select(ChatSession.id).where(
                ChatSession.id == session_id, ChatSession.user_id == uid
            )
        )
        if not owns:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "Сессия не найдена"
            )

        user_msg = Message(
            session_id=session_id, role="user", content=body.message
        )
        s.add(user_msg)
        await s.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(updated_at=func.now())
        )
        await s.flush()
        user_msg_id = user_msg.id

        history_rows = (
            await s.execute(
                select(Message.role, Message.content, Message.created_at)
                .where(Message.session_id == session_id)
                .order_by(Message.created_at.desc())
                .limit(settings.history_limit)
            )
        ).all()

    history = [
        {"role": r.role, "content": r.content}
        for r in reversed(history_rows)
    ]
    messages_payload = [
        {"role": "system", "content": settings.system_prompt},
        *history,
    ]

    ollama: OllamaClient = request.app.state.ollama

    async def _persist_assistant(text_: str) -> UUID | None:
        if not text_:
            return None
        async with factory() as s2, s2.begin():
            msg = Message(
                session_id=session_id, role="assistant", content=text_
            )
            s2.add(msg)
            await s2.flush()
            return msg.id

    async def gen():
        buf: list[str] = []
        try:
            yield sse({"type": "user_message", "id": str(user_msg_id)})

            async for chunk in ollama.stream_chat(
                messages_payload, settings.ollama_model
            ):
                delta = (chunk.get("message") or {}).get("content", "")
                if delta:
                    buf.append(delta)
                    yield sse({"type": "delta", "content": delta})

                if chunk.get("done"):
                    full = "".join(buf)
                    assistant_id = await _persist_assistant(full)
                    yield sse(
                        {
                            "type": "done",
                            "message_id": str(assistant_id)
                            if assistant_id
                            else None,
                            "usage": {
                                "prompt_eval_count": chunk.get(
                                    "prompt_eval_count"
                                ),
                                "eval_count": chunk.get("eval_count"),
                                "total_duration_ns": chunk.get(
                                    "total_duration"
                                ),
                            },
                        }
                    )
                    return

        except asyncio.CancelledError:
            partial = "".join(buf)
            if partial:
                try:
                    await _persist_assistant(partial)
                except Exception:
                    log.exception("не удалось сохранить частичный ответ")
            raise
        except OllamaError as e:
            log.warning("ollama error: %s", e)
            yield sse({"type": "error", "error": str(e)})
        except Exception as e:
            log.exception("внутренняя ошибка в стриме чата")
            yield sse({"type": "error", "error": f"внутренняя ошибка: {e}"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
