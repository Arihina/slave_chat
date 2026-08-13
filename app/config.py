from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
