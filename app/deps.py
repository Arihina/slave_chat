from __future__ import annotations

from uuid import UUID

from fastapi import Header, HTTPException


async def user_id(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> UUID:
    if x_user_id is None:
        raise HTTPException(
            401, "Не передан идентификатор пользователя (X-User-Id)")

    try:
        return UUID(x_user_id)
    except ValueError:
        raise HTTPException(
            401, "Некорректный идентификатор пользователя (ожидается UUID)")
