"""
Worker — drain loop extracted as a module so main.py stays clean.
Three wake paths (P-01):
  1. LISTEN/NOTIFY — instant on new job insert
  2. Startup drain  — clears backlog after restart
  3. 60-second timer — catches lost NOTIFYs
"""
import asyncio
import logging

import asyncpg

from handlers.telegram_message import handle_telegram_message
from handlers.telegram_start import handle_telegram_start
from handlers.cron_morning import handle_cron_morning
from handlers.cron_evening import handle_cron_evening
from handlers.cron_weekly import handle_cron_weekly_review
from handlers.cron_sunday_pm import handle_cron_sunday_pm

logger = logging.getLogger(__name__)

HANDLERS = {
    "telegram_message":   handle_telegram_message,
    "telegram_start":     handle_telegram_start,
    "cron_morning":       handle_cron_morning,
    "cron_evening":       handle_cron_evening,
    "cron_weekly_review": handle_cron_weekly_review,
    "cron_sunday_pm":     handle_cron_sunday_pm,
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
