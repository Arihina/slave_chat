from __future__ import annotations

import json
from typing import Any


def sse(data: Any) -> bytes:
    """Сериализует событие в формат Server-Sent Events."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8")
