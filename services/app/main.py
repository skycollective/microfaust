"""
MICROFAUST — merged API + Worker service
FastAPI handles webhooks/REST; worker drain loop runs as a background task.
Single Railway service, single Dockerfile, single set of logs.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from middleware import TenantContextMiddleware
from routers import webhook, health, onboarding, values, council
from worker import drain, periodic_drain, start_listener

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── DB pool (shared by API + Worker) ──────────────────────────────
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    app.state.pool = pool

    # ── Worker: startup drain (clear jobs queued while we were down) ──
    logger.info("Startup drain — clearing backlog")
    await drain(pool)

    # ── Worker: LISTEN/NOTIFY fast path ───────────────────────────────
    listen_conn = await asyncpg.connect(DATABASE_URL)
    await start_listener(listen_conn, pool)

    # ── Worker: 60-second safety net (P-01) ───────────────────────────
    drain_task = asyncio.create_task(periodic_drain(pool))

    logger.info("MICROFAUST ready — API + Worker running")
    yield

    drain_task.cancel()
    await listen_conn.close()
    await pool.close()


app = FastAPI(title="MICROFAUST", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://microfaust.vercel.app",
        "https://microfaust.app",
        "http://localhost:3000",
        "http://localhost:5500",    # local dev with Live Server
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TenantContextMiddleware)

app.include_router(webhook.router)
app.include_router(health.router)
app.include_router(onboarding.router)
app.include_router(values.router)
app.include_router(council.router)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "microfaust"}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
