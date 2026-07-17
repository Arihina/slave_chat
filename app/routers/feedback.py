from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import user_id
from ..models import ChatSession, Feedback, Message

router = APIRouter(prefix="/messages", tags=["feedback"])


async def _ensure_owns(s: AsyncSession, message_id: UUID, uid: UUID) -> None:
    owns = await s.scalar(
        select(Message.id)
        .join(ChatSession, ChatSession.id == Message.session_id)
        .where(Message.id == message_id, ChatSession.user_id == uid)
    )
    if not owns:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Сообщение не найдено"
        )


@router.post("/{message_id}/feedback")
async def set_feedback(
    message_id: UUID,
    payload: dict = Body(...),
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> dict:
    await _ensure_owns(s, message_id, uid)

    stmt = pg_insert(Feedback).values(
        message_id=message_id, user_id=uid, payload=payload
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Feedback.message_id],
        set_={
            "payload": Feedback.payload.op("||")(stmt.excluded.payload),
            "updated_at": func.now(),
        },
    ).returning(Feedback.payload)
    merged_payload = (await s.execute(stmt)).scalar_one()
    await s.commit()
    return {"message_id": str(message_id), "payload": merged_payload}


@router.get("/{message_id}/feedback")
async def get_feedback(
    message_id: UUID,
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> dict:
    await _ensure_owns(s, message_id, uid)
    obj = await s.get(Feedback, message_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Фидбэк не найден")
    return {
        "message_id": str(obj.message_id),
        "payload": obj.payload,
        "created_at": obj.created_at.isoformat(),
        "updated_at": obj.updated_at.isoformat(),
    }


@router.delete("/{message_id}/feedback", 
               status_code=status.HTTP_204_NO_CONTENT)
async def delete_feedback(
    message_id: UUID,
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> None:
    await _ensure_owns(s, message_id, uid)
    result = await s.execute(
        delete(Feedback).where(Feedback.message_id == message_id)
    )
    if result.rowcount == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Фидбэк не найден")
    await s.commit()