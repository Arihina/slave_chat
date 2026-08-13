from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException

from .tokens import TokenCounter

log = logging.getLogger(__name__)

# Служебные токены chat-шаблона на одно сообщение:
# <|im_start|>role\n ... <|im_end|>\n — обычно 3-5 токенов.
MESSAGE_OVERHEAD = 4
# Заголовок ответа ассистента, который шаблон добавит в конце промпта.
GENERATION_PRIMER = 3


@dataclass(frozen=True)
class WindowStats:
    prompt_tokens: int      # оценка промпта, который реально уедет в модель
    history_budget: int     # сколько токенов было отведено под историю
    kept: int               # сколько сообщений истории влезло
    dropped: int            # сколько отброшено
    reserved_output: int
    num_ctx: int
    counter: str

    def as_log(self) -> str:
        return (
            f"ctx={self.num_ctx} prompt≈{self.prompt_tokens} "
            f"reserve={self.reserved_output} history={self.kept}/"
            f"{self.kept + self.dropped} budget={self.history_budget} "
            f"counter={self.counter}"
        )


def message_tokens(role: str, content: str, counter: TokenCounter) -> int:
    return counter.count(content) + counter.count(role) + MESSAGE_OVERHEAD


def build_window(
    *,
    system: str,
    history: list[dict],
    question: str,
    counter: TokenCounter,
    num_ctx: int,
    reserve_output: int,
    safety: int = 64,
) -> tuple[list[dict], WindowStats]:
    """Собирает messages под ollama, влезая в num_ctx с запасом на генерацию.

    Инварианты:
      * system-промпт и текущий вопрос не выкидываются никогда;
      * история берётся с конца (самое свежее), непрерывным куском;
      * висячее assistant-сообщение в начале окна отбрасывается;
      * если даже system + вопрос + запас не влезают — это 400, а не молчаливая
        обрезка на стороне ollama.

    Элементы history — {"role", "content"} и опционально "tokens"
    (посчитанное заранее и сохранённое в БД значение).
    """
    fixed = (
        message_tokens("system", system, counter)
        + message_tokens("user", question, counter)
        + GENERATION_PRIMER
    )

    budget = num_ctx - reserve_output - safety - fixed

    if budget < 0:
        raise HTTPException(
            413,
            "Сообщение не помещается в контекст модели: нужно "
            f"{fixed + reserve_output + safety} токенов при лимите {num_ctx}. "
            "Сократите вопрос или увеличьте context_window.",
        )

    kept: list[dict] = []
    used = 0

    for item in reversed(history):
        role = item["role"]
        content = item["content"]
        cost = item.get("tokens")
        if cost is None:
            cost = message_tokens(role, content, counter)

        if used + cost > budget:
            break

        used += cost
        kept.append({"role": role, "content": content})

    kept.reverse()


    while kept and kept[0]["role"] == "assistant":
        used -= message_tokens(
            kept[0]["role"], kept[0]["content"], counter)
        kept.pop(0)

    stats = WindowStats(
        prompt_tokens=fixed + used,
        history_budget=budget,
        kept=len(kept),
        dropped=len(history) - len(kept),
        reserved_output=reserve_output,
        num_ctx=num_ctx,
        counter=getattr(counter, "name", "unknown"),
    )

    messages = [
        {"role": "system", "content": system},
        *kept,
        {"role": "user", "content": question},
    ]
    return messages, stats
