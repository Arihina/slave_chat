from __future__ import annotations

import json


def completion_object(
    completion_id: str, created: int, model: str, conversation_id_str: str | None,
    content: str, usage: dict,
) -> dict:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "system_fingerprint": None,
        "conversation_id": conversation_id_str,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                "refusal": None,
                "annotations": [],
            },
            "logprobs": None,
            "finish_reason": "stop",
        }],
        "usage": usage,
    }


def chat_usage(prompt_tokens: int | None, completion_tokens: int | None) -> dict:
    pt = prompt_tokens or 0
    ct = completion_tokens or 0
    return {
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": pt + ct,
    }


def chunk(
    completion_id: str, created: int, model: str, conversation_id_str: str | None,
    delta: dict | None, finish_reason: str | None = None, usage: dict | None = None,
) -> bytes:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "system_fingerprint": None,
        "conversation_id": conversation_id_str,
        "choices": [] if delta is None else [{
            "index": 0,
            "delta": delta,
            "logprobs": None,
            "finish_reason": finish_reason,
        }],
    }
    if usage is not None:
        payload["usage"] = usage

    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


DONE = b"data: [DONE]\n\n"


def responses_usage(prompt_tokens: int | None, completion_tokens: int | None) -> dict:
    """input_tokens_details / output_tokens_details обязательны в объекте
    usage — без них SDK не разбирает ответ."""
    pt = prompt_tokens or 0
    ct = completion_tokens or 0
    return {
        "input_tokens": pt,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        "output_tokens": ct,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": pt + ct,
    }


def message_item(item_id: str, text: str, status: str) -> dict:
    return {
        "id": item_id,
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": text,
                "annotations": [], "logprobs": []},
        ],
    }


def text_part(text: str) -> dict:
    return {"type": "output_text", "text": text, "annotations": [], "logprobs": []}


def response_object(
    response_id: str, created: int, model: str, conversation_id_str: str | None,
    status: str, output: list[dict], usage: dict | None = None,
    req: dict | None = None, error: dict | None = None,
) -> dict:
    req = req or {}

    obj = {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "model": model,
        "output": output,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "error": error,
        "incomplete_details": None,
        "instructions": req.get("instructions"),
        "metadata": req.get("metadata") or {},
        "temperature": req.get("temperature"),
        "top_p": req.get("top_p"),
        "max_output_tokens": req.get("max_output_tokens"),
        "previous_response_id": req.get("previous_response_id"),
        "store": req.get("store", True),
        "truncation": "disabled",
        "text": {"format": {"type": "text"}},
        # Расширение платформы, не часть спецификации.
        "conversation_id": conversation_id_str,
    }
    if usage is not None:
        obj["usage"] = usage

    return obj


def sse_event(seq: int, event_type: str, **fields) -> str:
    payload = {"type": event_type, "sequence_number": seq, **fields}
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
