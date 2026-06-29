from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import user_id
from ..models import ChatSession

router = APIRouter(prefix="/sessions", tags=["sessions"])


class CreateSessionBody(BaseModel):
    title: str | None = None


class RenameSessionBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)


def _serialize(o: ChatSession) -> dict:
    return {
        "id": str(o.id),
        "title": o.title,
        "created_at": o.created_at.isoformat(),
        "updated_at": o.updated_at.isoformat(),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionBody,
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> dict:
    obj = ChatSession(user_id=uid, title=body.title)
    s.add(obj)
    await s.commit()
    await s.refresh(obj)
    return _serialize(obj)


@router.get("")
async def list_sessions(
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> list[dict]:
    rows = (
        await s.execute(
            select(ChatSession)
            .where(ChatSession.user_id == uid)
            .order_by(ChatSession.updated_at.desc())
        )
    ).scalars().all()
    return [_serialize(o) for o in rows]


@router.patch("/{session_id}")
async def rename_session(
    session_id: UUID,
    body: RenameSessionBody,
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> dict:
    result = await s.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.user_id == uid)
        .values(title=body.title, updated_at=func.now())
        .returning(ChatSession)
    )
    obj = result.scalar_one_or_none()
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сессия не найдена")
    await s.commit()
    return _serialize(obj)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> None:
    result = await s.execute(
        delete(ChatSession).where(
            ChatSession.id == session_id, ChatSession.user_id == uid
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сессия не найдена")
    await s.commit()
