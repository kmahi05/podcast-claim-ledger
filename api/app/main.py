import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import db, storage, vectorstore
from app.auth.router import router as auth_router
from app.config import get_settings
from app.errors import install_handlers
from app.ratelimit import limiter
from app.workspaces.router import router as workspaces_router

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    db.init_engines(s.APP_DATABASE_URL, s.WORKER_DATABASE_URL)
    client = vectorstore.open_client(s.CHROMA_PATH)
    # raises (refusing to start) if a collection was built with another embedding model
    app.state.chroma = client
    app.state.collections = vectorstore.ensure_collections(client, s.GEMINI_EMBEDDING_MODEL)
    try:
        yield
    finally:
        await db.dispose_engines()


health_router = APIRouter(tags=["health"])


@health_router.get("/health")
async def health(request: Request) -> JSONResponse:
    checks = {"db": "ok", "chroma": "ok"}
    try:
        async with db.worker_session() as conn:
            await conn.execute(text("select 1"))
    except Exception:
        log.exception("health: database check failed")
        checks["db"] = "error"
    try:
        await asyncio.to_thread(request.app.state.chroma.heartbeat)
    except Exception:
        log.exception("health: chroma check failed")
        checks["chroma"] = "error"
    ok = all(v == "ok" for v in checks.values())
    body = {
        "status": "ok" if ok else "degraded",
        **checks,
        "whisper_model": get_settings().WHISPER_MODEL,
    }
    return JSONResponse(body, status_code=200 if ok else 503)


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Podcast Claim Ledger API", lifespan=lifespan)
    app.state.limiter = limiter
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[s.WEB_ORIGIN],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )
    install_handlers(app)
    for router in (health_router, auth_router, workspaces_router, storage.router):
        app.include_router(router, prefix="/api/v1")
    return app


app = create_app()
