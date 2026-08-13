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
from app.tokens import make_counter
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

    app.state.counter = make_counter(settings.tokenizer_repo)
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


_VALIDATION_STATUS = 400

_LOC_PREFIXES = {"body", "query", "path", "header", "cookie"}


def _param_from_loc(loc) -> str | None:
    """('body', 'messages', 0, 'role') -> 'messages.0.role'"""
    parts = [str(p) for p in loc if p not in _LOC_PREFIXES]
    return ".".join(parts) or None


def _error_body(status_code: int, message: str, param: str | None = None) -> dict:
    return {"error": {
        "message": message,
        "type": _ERROR_TYPES.get(status_code, "server_error"),
        "param": param,
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
    param = _param_from_loc(first.get("loc", ()))

    return JSONResponse(
        status_code=_VALIDATION_STATUS,
        content=_error_body(_VALIDATION_STATUS, message, param),
    )


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
