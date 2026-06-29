from __future__ import annotations

from uuid import UUID

from fastapi import Header, HTTPException, status


async def user_id(x_user_id: str = Header(..., alias="X-User-Id")) -> UUID:
    try:
        return UUID(x_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Невалидный X-User-Id (ожидается UUID)",
        )
