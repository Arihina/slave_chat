from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import user_id
from ..models import ChatSession, Message

router = APIRouter(tags=["messages"])


@router.get("/sessions/{session_id}/messages")
async def get_messages(
    session_id: UUID,
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> list[dict]:
    owns = await s.scalar(
        select(ChatSession.id).where(
            ChatSession.id == session_id, ChatSession.user_id == uid
        )
    )
    if not owns:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сессия не найдена")

    rows = (
        await s.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at)
        )
    ).scalars().all()

    return [
        {
            "id": str(m.id),
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        for m in rows
    ]
