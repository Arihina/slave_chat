from __future__ import annotations

import json
from typing import AsyncIterator

import httpx


class OllamaError(Exception):
    """Ошибки связи с Ollama или нестандартный ответ."""


class OllamaClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream_chat(
        self, messages: list[dict], model: str
    ) -> AsyncIterator[dict]:
        """
        Ollama /api/chat в режиме stream=true отдаёт NDJSON.
        Каждая строка — JSON-объект с полем `message.content` (дельта)
        и финальным флагом `done: true` с метриками.
        """
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": 0.6
            },
        }
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise OllamaError(
                        f"ollama вернула {resp.status_code}: "
                        f"{body.decode(errors='replace')}"
                    )
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except httpx.HTTPError as e:
            raise OllamaError(f"ошибка связи с ollama: {e}") from e
