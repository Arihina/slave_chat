from __future__ import annotations

from datetime import datetime
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


_KNOWN_KEYS = ("vote", "comment")


def _parse_completion_id(completion_id: str) -> UUID:
    raw = completion_id

    for prefix in ("chatcmpl-", "resp_"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(400, "Некорректный id сообщения")


def _validate(payload: dict) -> dict:
    """Раньше принималось что угодно: `vote: 5` молча сохранялся и потом
    приезжал во фронт. Валидируем ровно известные ключи, остальные — как и
    прежде, произвольные."""
    if not isinstance(payload, dict):
        raise HTTPException(400, "Тело должно быть объектом")

    if "vote" in payload and payload["vote"] not in (1, -1, None):
        raise HTTPException(400, "vote должен быть 1, -1 или null")

    if "comment" in payload and not isinstance(payload["comment"], (str, type(None))):
        raise HTTPException(400, "comment должен быть строкой или null")

    return payload


def _feedback_out(
    message_id: UUID, payload: dict | None,
    created_at: datetime | None = None, updated_at: datetime | None = None,
) -> dict:
    payload = payload or {}

    out: dict = {"message_id": str(message_id)}
    out.update({key: payload.get(key) for key in _KNOWN_KEYS})

    if created_at is not None:
        out["created_at"] = created_at.isoformat()
    if updated_at is not None:
        out["updated_at"] = updated_at.isoformat()

    out["payload"] = payload

    return out


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
    _validate(payload)

    stmt = pg_insert(Feedback).values(
        message_id=message_id, user_id=uid, payload=payload
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Feedback.message_id],
        set_={
            "payload": Feedback.payload.op("||")(stmt.excluded.payload),
            "updated_at": func.now(),
        },
    ).returning(Feedback.payload, Feedback.created_at, Feedback.updated_at)

    merged, created_at, updated_at = (await s.execute(stmt)).one()
    await s.commit()

    return _feedback_out(message_id, merged, created_at, updated_at)


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
        return _feedback_out(message_id, None)

    return _feedback_out(message_id, obj.payload, obj.created_at, obj.updated_at)


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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Оценка не найдена")
    await s.commit()
