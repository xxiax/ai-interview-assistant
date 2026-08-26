"""FastAPI 应用入口。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# 必须先加载 .env，再导入会读取安全配置的应用模块。
_env_file = os.environ.get("AI_ENV_FILE") or str(
    Path(__file__).resolve().parents[1] / ".env"
)
load_dotenv(dotenv_path=_env_file, override=False)

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse,
    Response,
)
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import asr, cost_control, db
from .routes_configs import router as configs_router
from .routes_review import router as review_router
from .routes_sessions import router as sessions_router
from .routes_settings import router as settings_router
from .security import validate_security_config
from .ws import prepare_realtime, shutdown_realtime, websocket_endpoint


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str]:
    return [
        item.strip() for item in os.environ.get(name, "").split(",") if item.strip()
    ]


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_security_config()
    asr.validate_funasr_config()
    asr.validate_media_probe_available()
    cost_control.reset_runtime_state()
    prepare_realtime()
    conn = db.get_db()
    try:
        db.init_db(conn)
    finally:
        conn.close()
    try:
        yield
    finally:
        await shutdown_realtime()
        cost_control.reset_runtime_state()


def create_app() -> FastAPI:
    # 生产默认关闭交互式文档；本地调试用 AI_DOCS_ENABLED=true 打开
    docs_enabled = _env_bool("AI_DOCS_ENABLED")
    application = FastAPI(
        title="AI 面试助手",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    allowed_origins = _env_list("AI_ALLOWED_ORIGINS")
    if allowed_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )

    allowed_hosts = _env_list("AI_ALLOWED_HOSTS")
    if allowed_hosts:
        application.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    if _env_bool("AI_REQUIRE_HTTPS"):
        application.add_middleware(HTTPSRedirectMiddleware)

    def redact_secrets(value):
        if isinstance(value, dict):
            return {
                key: "***"
                if key.lower()
                in {
                    "api_key",
                    "token",
                    "authorization",
                    "password",
                    "secret",
                    "access_token",
                    "input",
                }
                else redact_secrets(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [redact_secrets(item) for item in value]
        return value

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        errors = redact_secrets(jsonable_encoder(exc.errors()))
        return JSONResponse(status_code=422, content={"detail": errors})

    @application.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Cache-Control"] = "no-store"
        if request.url.path in {"/docs", "/redoc"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            )
        if _env_bool("AI_REQUIRE_HTTPS"):
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    application.include_router(sessions_router)
    application.include_router(configs_router)
    application.include_router(review_router)
    application.include_router(settings_router)

    @application.websocket("/ws/{session_id}")
    async def ws_endpoint(ws: WebSocket, session_id: str):
        await websocket_endpoint(ws, session_id)

    @application.get("/health")
    async def health():
        return {"status": "ok"}

    @application.get("/health/live")
    async def liveness():
        return {"status": "ok"}

    @application.get("/health/ready")
    async def readiness():
        conn = db.get_db()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        if os.environ.get("AI_ASR_ENGINE", "funasr").strip().lower() == "funasr":
            try:
                await asr.check_funasr_ready()
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail="FunASR 尚未就绪") from exc
        return {"status": "ready"}

    return application


app = create_app()
