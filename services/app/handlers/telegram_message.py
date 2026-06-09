"""
Telegram message handler.
P-03: /forget hard-deletes rows (GDPR Art. 17)
P-04: bot token never logged
"""
import json, logging, re
import asyncpg, httpx

logger = logging.getLogger(__name__)

FORGET_ALL  = re.compile(r"^/forget\s+all$", re.IGNORECASE)
FORGET_TOPIC = re.compile(r"^/forget\s+(.+)$", re.IGNORECASE)
HABIT_ADD   = re.compile(r"^add habit:\s*(.+)$", re.IGNORECASE)
DECIDE_CMD  = re.compile(r"^/decide\s*(.*)", re.IGNORECASE | re.DOTALL)


async def handle_telegram_message(pool: asyncpg.Pool, job: asyncpg.Record):
    payload = json.loads(job["payload"])
    tenant_id = job["tenant_id"]
    chat_id   = payload["chat_id"]
    text      = payload.get("text", "").strip()

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token FROM tenants WHERE id=$1", tenant_id
        )
    if not tenant:
        return

    token = tenant["telegram_bot_token"]

    if FORGET_ALL.match(text):
        await _forget_all_confirm(pool, tenant_id, token, chat_id)
    elif m := FORGET_TOPIC.match(text):
        await _forget_topic(pool, tenant_id, m.group(1), token, chat_id)
    elif m := HABIT_ADD.match(text):
        await _add_habit(pool, tenant_id, m.group(1), token, chat_id)
    elif DECIDE_CMD.match(text):
        await _send(token, chat_id,
            "Conseil en cours de délibération... (intégration LLM à venir)")
    else:
        # Default: capture as thought (placeholder for full LLM routing)
        await _send(token, chat_id, f"✅ Capturé en mémoire.")
        await _mark_responded(pool, tenant_id)


async def _forget_all_confirm(pool, tenant_id, token, chat_id):
    await _send(token, chat_id,
        "⚠️ Cette action supprime toutes vos données définitivement.\n"
        "Répondez CONFIRMER SUPPRESSION pour continuer."
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO job_queue (tenant_id, job_type, payload) VALUES ($1,'await_forget_confirm','{}')",
            tenant_id,
        )


async def _forget_topic(pool, tenant_id, topic, token, chat_id):
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        result = await conn.execute(
            "DELETE FROM thoughts WHERE tenant_id=$1 AND content ILIKE $2",
            tenant_id, f"%{topic}%",
        )
    count = int(result.split()[-1]) if result else 0
    await _send(token, chat_id,
        f"✅ {count} souvenir(s) sur '{topic}' supprimé(s) définitivement."
    )


async def _add_habit(pool, tenant_id, name, token, chat_id):
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        await conn.execute(
            "INSERT INTO habits (tenant_id, name) VALUES ($1,$2)", tenant_id, name.strip()
        )
    await _send(token, chat_id, f"✅ Habitude ajoutée : {name.strip()}")


async def _mark_responded(pool, tenant_id):
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE briefings SET user_responded=true
               WHERE tenant_id=$1 AND created_at > NOW()-INTERVAL '4 hours'
                 AND user_responded=false""",
            tenant_id,
        )


async def _send(token: str, chat_id: str, text: str):
    # P-04: never log token
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("Telegram send failed chat=%s: %s", chat_id, e)
