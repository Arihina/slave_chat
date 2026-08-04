from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import user_id
from ..models import Conversation, Feedback, Message

router = APIRouter(prefix="/v1/platform/conversations", tags=["conversations"])


class CreateConversationBody(BaseModel):
    title: str | None = None


class RenameConversationBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)


def _serialize(o: Conversation) -> dict:
    return {
        "id": str(o.id),
        "title": o.title,
        "created_at": o.created_at.isoformat(),
        "updated_at": o.updated_at.isoformat(),
    }


def _serialize_message(m: Message, fb: Feedback | None) -> dict:
    return {
        "id": str(m.id),
        "role": m.role,
        "content": m.content,
        "created_at": m.created_at.isoformat(),
        "feedback": {"payload": fb.payload} if fb else None,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: CreateConversationBody,
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> dict:
    obj = Conversation(user_id=uid, title=body.title)
    s.add(obj)
    await s.commit()
    await s.refresh(obj)
    return _serialize(obj)


@router.get("")
async def list_conversations(
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> list[dict]:
    rows = (
        await s.execute(
            select(Conversation)
            .where(Conversation.user_id == uid)
            .order_by(Conversation.updated_at.desc())
        )
    ).scalars().all()
    return [_serialize(o) for o in rows]


@router.get("/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: UUID,
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> list[dict]:
    owns = await s.scalar(
        select(Conversation.id).where(
            Conversation.id == conversation_id, Conversation.user_id == uid
        )
    )
    if not owns:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чат не найден")

    rows = (
        await s.execute(
            select(Message, Feedback)
            .outerjoin(Feedback, Feedback.message_id == Message.id)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
        )
    ).all()

    return [_serialize_message(m, fb) for m, fb in rows]


@router.patch("/{conversation_id}")
async def rename_conversation(
    conversation_id: UUID,
    body: RenameConversationBody,
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> dict:
    result = await s.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id, Conversation.user_id == uid)
        .values(title=body.title, updated_at=func.now())
        .returning(Conversation)
    )
    obj = result.scalar_one_or_none()
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чат не найден")
    await s.commit()
    return _serialize(obj)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID,
    uid: UUID = Depends(user_id),
    s: AsyncSession = Depends(get_session),
) -> None:
    result = await s.execute(
        delete(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == uid
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чат не найден")
    await s.commit()
