"""
Telegram webhook receiver — POST /webhook/{tenant_uuid}
Returns HTTP 200 within 5s (Telegram requirement).
Actual processing is async via job_queue.
"""
import json
import logging
import re
import uuid

import asyncpg
from fastapi import APIRouter, Request, Response

logger = logging.getLogger(__name__)
router = APIRouter()

START_RE = re.compile(r"^/start", re.IGNORECASE)


@router.post("/webhook/{tenant_uuid}")
async def receive_webhook(tenant_uuid: str, request: Request):
    try:
        tenant_id = uuid.UUID(tenant_uuid)
    except ValueError:
        return Response(status_code=404)

    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400)

    pool: asyncpg.Pool = request.app.state.pool

    async with pool.acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, telegram_chat_id FROM tenants WHERE id=$1 AND active=true",
            tenant_id,
        )
    if not tenant:
        return Response(status_code=404)

    message = body.get("message") or body.get("edited_message")
    if not message:
        return Response(status_code=200)

    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "")
    message_id = message.get("message_id")

    if START_RE.match(text):
        # P-05: capture chat_id immediately
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE tenants SET telegram_chat_id=$1 WHERE id=$2",
                chat_id, tenant_id,
            )
        await _enqueue(pool, tenant_id, "telegram_start", {
            "chat_id": chat_id, "message_id": message_id,
        })
    else:
        await _enqueue(pool, tenant_id, "telegram_message", {
            "chat_id": chat_id, "message_id": message_id,
            "text": text, "raw": body,
        })

    return Response(status_code=200)


async def _enqueue(pool, tenant_id, job_type: str, payload: dict):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO job_queue (tenant_id, job_type, payload) VALUES ($1,$2,$3)",
            tenant_id, job_type, json.dumps(payload),
        )
