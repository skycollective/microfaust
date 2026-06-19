import logging
from datetime import date
import asyncpg, httpx

logger = logging.getLogger(__name__)

async def handle_cron_evening(pool: asyncpg.Pool, job: asyncpg.Record):
    tenant_id = job["tenant_id"]
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token, telegram_chat_id, language FROM tenants WHERE id=$1", tenant_id
        )
        if not tenant or not tenant["telegram_chat_id"]:
            return
        already = await conn.fetchval(
            "SELECT 1 FROM briefings WHERE tenant_id=$1 AND briefing_type='evening' AND created_at::date=$2",
            tenant_id, date.today(),
        )
        if already:
            return
        habits_done  = await conn.fetchval(
            "SELECT COUNT(*) FROM habit_log WHERE tenant_id=$1 AND date=$2", tenant_id, date.today()
        )
        habits_total = await conn.fetchval(
            "SELECT COUNT(*) FROM habits WHERE tenant_id=$1 AND active=true", tenant_id
        )
        checkin = await conn.fetchrow(
            "SELECT value FROM checkins WHERE tenant_id=$1 AND created_at::date=$2 LIMIT 1",
            tenant_id, date.today(),
        )

    token   = tenant["telegram_bot_token"]
    chat_id = tenant["telegram_chat_id"]
    lang    = tenant["language"] or "fr"
    mood    = checkin["value"] if checkin else ("not recorded" if lang == "en" else "non enregistree")

    if lang == "en":
        text = (
            f"Habits : {habits_done}/{habits_total}  |  Mood : {mood}\n\n"
            "🌙 Now is the moment to reflect on your day.\n"
            "What are you grateful for? What did you learn today?"
        )
    else:
        text = (
            f"Habitudes : {habits_done}/{habits_total}  |  Humeur : {mood}\n\n"
            "🌙 C'est le moment de prendre un instant pour revenir sur votre journee.\n"
            "De quoi etes-vous reconnaissant(e) ? Qu'avez-vous appris aujourd'hui ?"
        )
    await _send(token, chat_id, text)

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        await conn.execute(
            "INSERT INTO briefings (tenant_id,briefing_type,content) VALUES ($1,'evening',$2)",
            tenant_id, text,
        )

async def _send(token, chat_id, text):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("Evening briefing send failed: %s", e)
