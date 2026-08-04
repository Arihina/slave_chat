from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import user_id
from ..models import Feedback, Message

router = APIRouter(
    prefix="/v1/chat/completions/{completion_id}/feedback", tags=["feedback"])


def _parse_completion_id(completion_id: str) -> UUID:
    raw = completion_id.removeprefix("chatcmpl-")
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(422, "Некорректный id completion'а")


async def _ensure_owns(s: AsyncSession, message_id: UUID, uid: UUID) -> None:
    owns = await s.scalar(
        select(Message.id).where(Message.id ==
                                 message_id, Message.user_id == uid)
    )
    if not owns:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Сообщение не найдено"
        )


@router.post("")
async def set_feedback(
    completion_id: str,
    payload: dict = Body(...),
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> dict:
    message_id = _parse_completion_id(completion_id)
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


@router.get("")
async def get_feedback(
    completion_id: str,
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> dict:
    message_id = _parse_completion_id(completion_id)
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


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feedback(
    completion_id: str,
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> None:
    message_id = _parse_completion_id(completion_id)
    await _ensure_owns(s, message_id, uid)
    result = await s.execute(
        delete(Feedback).where(Feedback.message_id == message_id)
    )
    if result.rowcount == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Фидбэк не найден")
    await s.commit()
