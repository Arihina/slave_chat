from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    DB_HOST: str
    DB_PORT: int
    DB_USER: str
    DB_PASSWORD: str
    DB_NAME: str

    ollama_model: str
    ollama_base_url: str = "http://localhost:11434"
    ollama_timeout: float = 180

    context_window: int = 8192
    reserve_output_tokens: int = 1024
    context_safety_tokens: int = 64
    history_max_messages: int = 200
    tokenizer_repo: str | None = None

    system_prompt: str = "Ты - полезный универсальный ассистент. Отвечай по-русски, кратко и по делу, без вступлений и извинений."

    HOST: str = "127.0.0.1"
    PORT: int = 8002

    # Внутренняя аутентификация master -> агент
    internal_auth_mode: Literal["off", "token", "assertion"] = "token"
    master_internal_token: str | None = None
    master_assertion_public_key: str | None = None
    public_paths: list[str] = ["/health"]
    agent_id: str = "chat"
    env: str = "dev"
    allow_public_bind: bool = False

    @field_validator("public_paths", mode="before")
    @classmethod
    def _split_public_paths(cls, v):
        """Поддержать и `PUBLIC_PATHS=/health`, и `PUBLIC_PATHS=["/health"]`."""
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [p for p in (x.strip() for x in v.split(",")) if p]
        return v

    @property
    def is_prod(self) -> bool:
        return self.env.strip().lower() == "prod"

    def validate_internal_auth(self) -> None:
        """Fail-closed проверка конфигурации. Вызывается из lifespan до старта.

        Агент обязан отказываться стартовать, а не работать «как раньше»,
        когда режим требует секрета, но секрета нет.
        """
        if self.internal_auth_mode == "token" and not self.master_internal_token:
            raise RuntimeError(
                "INTERNAL_AUTH_MODE=token, но MASTER_INTERNAL_TOKEN пуст. "
                "Задайте секрет агента (выдаёт master) или переведите режим в 'off'."
            )
        if self.internal_auth_mode == "assertion" and not self.master_assertion_public_key:
            raise RuntimeError(
                "INTERNAL_AUTH_MODE=assertion, но MASTER_ASSERTION_PUBLIC_KEY пуст. "
                "Задайте публичный ключ master."
            )
        if self.internal_auth_mode == "off":
            if self.is_prod:
                raise RuntimeError(
                    "INTERNAL_AUTH_MODE=off запрещён в ENV=prod."
                )
            log.warning(
                "INTERNAL_AUTH_MODE=off: внутренняя аутентификация отключена. "
                "Это допустимо только для локальной разработки и тестов."
            )

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
