import logging
from datetime import date, timedelta
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

        # Consecutive evening reflection streak
        checkin_dates = await conn.fetch(
            "SELECT DISTINCT created_at::date AS d FROM thoughts "
            "WHERE tenant_id=$1 AND 'evening_checkin'=ANY(coalesce(tags,'{}')) "
            "ORDER BY d DESC LIMIT 60",
            tenant_id,
        )

    streak = 0
    today = date.today()
    for i, row in enumerate(checkin_dates):
        if row["d"] == today - timedelta(days=i):
            streak += 1
        else:
            break

    token   = tenant["telegram_bot_token"]
    chat_id = tenant["telegram_chat_id"]
    lang    = tenant["language"] or "fr"

    if streak > 0:
        streak_line = (
            f"✍️ {streak} jour{'s' if streak > 1 else ''} de réflexion consécutif{'s' if streak > 1 else ''}"
            if lang != "en" else
            f"✍️ {streak} day{'s' if streak > 1 else ''} writing streak"
        )
    else:
        streak_line = ""

    if lang == "en":
        text = (
            (streak_line + "\n\n" if streak_line else "") +
            "🌙 Now is the moment to reflect on your day.\n"
            "What are you grateful for? What did you learn today?"
        )
    else:
        text = (
            (streak_line + "\n\n" if streak_line else "") +
            "🌙 C'est le moment de prendre un instant pour revenir sur votre journée.\n"
            "De quoi êtes-vous reconnaissant(e) ? Qu'avez-vous appris aujourd'hui ?"
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
