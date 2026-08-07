from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.db import dispose_engine, init_engine
from app.ollama import OllamaClient
from app.routers import chat, conversations, feedback, responses

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


app = FastAPI(title="Chat Agent", version="2.0.0", lifespan=lifespan)

app.include_router(conversations.router)
app.include_router(chat.router)
app.include_router(responses.router)
app.include_router(feedback.router)


@app.get("/healthz", tags=["health"])
async def healthz() -> dict:
    return {"status": "ok", "model": settings.ollama_model}


_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    404: "not_found_error",
    413: "invalid_request_error",
    415: "invalid_request_error",
    422: "invalid_request_error",
}


def _error_body(status_code: int, message: str) -> dict:
    return {"error": {
        "message": message,
        "type": _ERROR_TYPES.get(status_code, "server_error"),
        "param": None,
        "code": None,
    }}


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.status_code, str(exc.detail)),
    )


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    message = first.get("msg", "Некорректный запрос")
    return JSONResponse(status_code=422, content=_error_body(422, message))


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
