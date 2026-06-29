from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db import dispose_engine, init_engine
from app.ollama import OllamaClient
from app.routers import chat, feedback, messages, sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_engine()
    app.state.ollama = OllamaClient(
        base_url=settings.ollama_base_url,
        timeout=settings.ollama_timeout,
    )
    try:
        yield
    finally:
        await app.state.ollama.aclose()
        await dispose_engine()


app = FastAPI(title="Chat Agent", lifespan=lifespan)

app.include_router(sessions.router)
app.include_router(messages.router)
app.include_router(chat.router)
app.include_router(feedback.router)


@app.get("/healthz", tags=["health"])
async def healthz() -> dict:
    return {"status": "ok", "model": settings.ollama_model}


def run() -> None:
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
        log_level="info",
    )


if __name__ == "__main__":
    run()
