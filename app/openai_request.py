from __future__ import annotations

from typing import Iterable

from fastapi import HTTPException

CHAT_TEXT_TYPES = frozenset({"text"})

RESPONSES_TEXT_TYPES = frozenset({"input_text", "output_text"})

INSTRUCTION_ROLES = ("system", "developer")
DIALOG_ROLES = ("user", "assistant")


def extract_text(content, text_types: Iterable[str]) -> str:
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return ""

    types = frozenset(text_types)
    return "\n".join(
        str(p.get("text", ""))
        for p in content
        if isinstance(p, dict) and p.get("type") in types
    )


def collect_instructions(items: list, text_types: Iterable[str]) -> str | None:
    texts = [
        extract_text(m.get("content"), text_types)
        for m in items
        if isinstance(m, dict) and m.get("role") in INSTRUCTION_ROLES
    ]
    joined = "\n".join(t for t in texts if t.strip())
    return joined or None


def sampling_options(
    temperature=None, top_p=None, max_tokens=None, num_ctx=None
) -> dict:
    options: dict = {}

    if num_ctx is not None:
        options["num_ctx"] = num_ctx

    if temperature is not None:
        options["temperature"] = _number("temperature", temperature)
    if top_p is not None:
        options["top_p"] = _number("top_p", top_p)
    if max_tokens is not None:
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
            raise HTTPException(
                400, "Ограничение на длину ответа должно быть целым числом >= 1")
        options["num_predict"] = max_tokens

    return options


def _number(name: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(400, f"{name} должен быть числом")
    return float(value)
