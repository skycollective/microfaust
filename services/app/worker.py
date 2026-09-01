"""
Worker — drain loop extracted as a module so main.py stays clean.
Three wake paths (P-01):
  1. LISTEN/NOTIFY — instant on new job insert
  2. Startup drain  — clears backlog after restart
  3. 60-second timer — catches lost NOTIFYs
Scheduler: internal asyncio loop fires cron jobs — no pg_cron needed.
"""
import asyncio
import logging
from datetime import datetime
import pytz

import asyncpg

PARIS = pytz.timezone("Europe/Paris")

# (job_type, hour, minute, weekday)  weekday: 0-6 Mon-Sun, None=daily
_CRON_SCHEDULE = [
    ("cron_morning",       7,  5, None, None),   # daily 07:05 Paris
    ("cron_evening",      20, 30, None, None),   # daily 20:30 Paris
    ("cron_weekly_review", 9,  0,    6, None),   # Sunday 09:00 Paris
    ("cron_sunday_pm",    17,  0,    6, None),   # Sunday 17:00 Paris
    ("cron_thursday",      9,  0,    3, None),   # Thursday 09:00 Paris
    ("cron_monthly",       9,  0, None,    1),   # 1st of month 09:00 Paris
]

from handlers.telegram_message import handle_telegram_message
from handlers.telegram_start import handle_telegram_start
from handlers.cron_morning import handle_cron_morning
from handlers.cron_evening import handle_cron_evening
from handlers.cron_weekly import handle_cron_weekly_review
from handlers.cron_sunday_pm import handle_cron_sunday_pm
from handlers.cron_thursday import handle_cron_thursday
from handlers.cron_monthly import handle_cron_monthly

logger = logging.getLogger(__name__)

HANDLERS = {
    "telegram_message":   handle_telegram_message,
    "telegram_start":     handle_telegram_start,
    "cron_morning":       handle_cron_morning,
    "cron_evening":       handle_cron_evening,
    "cron_weekly_review": handle_cron_weekly_review,
    "cron_sunday_pm":     handle_cron_sunday_pm,
    "cron_thursday":      handle_cron_thursday,
    "cron_monthly":       handle_cron_monthly,
}


async def drain(pool: asyncpg.Pool) -> None:
    """Pick up and process all pending jobs. SKIP LOCKED = safe for multiple workers."""
    async with pool.acquire() as conn:
        while True:
            rows = await conn.fetch(
                """
                UPDATE job_queue
                SET status = 'processing',
                    started_at = NOW(),
                    attempts = attempts + 1
                WHERE id = (
                    SELECT id FROM job_queue
                    WHERE status = 'pending'
                      AND scheduled_at <= NOW()
                    ORDER BY scheduled_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING *
                """
            )
            if not rows:
                break
            await _process(pool, rows[0])


async def _process(pool: asyncpg.Pool, job: asyncpg.Record) -> None:
    handler = HANDLERS.get(job["job_type"])
    if not handler:
        logger.warning("No handler for job_type=%s — marking done", job["job_type"])
        await _set_status(pool, job["id"], "done")
        return
    try:
        await handler(pool, job)
        await _set_status(pool, job["id"], "done")
    except Exception as exc:
        logger.error("Job %s (%s) failed: %s", job["id"], job["job_type"], exc, exc_info=True)
        await _fail(pool, job["id"], str(exc))


async def _set_status(pool: asyncpg.Pool, job_id, status: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE job_queue SET status=$2, completed_at=NOW() WHERE id=$1",
            job_id, status,
        )


async def _fail(pool: asyncpg.Pool, job_id, error: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE job_queue
            SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'failed' END,
                error = $2,
                completed_at = NOW()
            WHERE id = $1
            """,
            job_id, error,
        )


async def start_listener(listen_conn: asyncpg.Connection, pool: asyncpg.Pool) -> None:
    await listen_conn.add_listener(
        "new_job",
        lambda *_: asyncio.ensure_future(drain(pool)),
    )
    logger.info("Worker listening on 'new_job' channel")


async def periodic_drain(pool: asyncpg.Pool) -> None:
    """60-second fallback — runs unconditionally regardless of NOTIFY (P-01)."""
    while True:
        await asyncio.sleep(60)
        try:
            await drain(pool)
        except Exception as exc:
            logger.error("Periodic drain error: %s", exc)


async def cron_scheduler(pool: asyncpg.Pool) -> None:
    """Internal scheduler — checks every minute, fires jobs at configured times."""
    _fired: set[str] = set()  # tracks which jobs fired this minute

    while True:
        await asyncio.sleep(30)
        now = datetime.now(PARIS)
        minute_key = now.strftime("%Y-%m-%d %H:%M")

        for job_type, hour, minute, weekday, monthday in _CRON_SCHEDULE:
            if now.hour != hour or now.minute != minute:
                continue
            if weekday is not None and now.weekday() != weekday:
                continue
            if monthday is not None and now.day != monthday:
                continue
            fire_key = f"{job_type}:{minute_key}"
            if fire_key in _fired:
                continue
            _fired.add(fire_key)
            # Keep _fired small — only keep today's keys
            today_prefix = now.strftime("%Y-%m-%d")
            _fired = {k for k in _fired if k.endswith(today_prefix) or today_prefix in k}
            try:
                async with pool.acquire() as conn:
                    tenants = await conn.fetch(
                        "SELECT id FROM tenants WHERE telegram_chat_id IS NOT NULL AND active=true"
                    )
                    for t in tenants:
                        idem_key = f"{t['id']}:{job_type}:{minute_key}"
                        await conn.execute(
                            """INSERT INTO job_queue (tenant_id, job_type, payload, idempotency_key)
                               VALUES ($1,$2,'{}',$3)
                               ON CONFLICT (idempotency_key) DO NOTHING""",
                            t["id"], job_type, idem_key,
                        )
                logger.info("Cron fired: %s for %d tenant(s)", job_type, len(tenants))
            except Exception as exc:
                logger.error("Cron scheduler error (%s): %s", job_type, exc)
