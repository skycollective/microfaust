import logging
from datetime import date, timedelta
import asyncpg, httpx

logger = logging.getLogger(__name__)

async def handle_cron_weekly_review(pool: asyncpg.Pool, job: asyncpg.Record):
    tenant_id = job["tenant_id"]
    week_ago = date.today() - timedelta(days=7)

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token, telegram_chat_id, language FROM tenants WHERE id=$1", tenant_id
        )
        if not tenant or not tenant["telegram_chat_id"]:
            return
        already = await conn.fetchval(
            "SELECT 1 FROM briefings WHERE tenant_id=$1 AND briefing_type='weekly_review' AND created_at::date > $2",
            tenant_id, week_ago,
        )
        if already:
            return
        habits_done = await conn.fetchval(
            "SELECT COUNT(*) FROM habit_log WHERE tenant_id=$1 AND date > $2", tenant_id, week_ago
        )
        thoughts_count = await conn.fetchval(
            "SELECT COUNT(*) FROM thoughts WHERE tenant_id=$1 AND created_at::date > $2",
            tenant_id, week_ago,
        )

    lang = tenant["language"] or "fr"
    habits_line = f"✅ Habitudes complétées : {habits_done}\n" if habits_done > 0 else ""

    if lang == "en":
        thoughts_label = f"🧠 Thoughts captured : {thoughts_count}"
        question = "What are you most proud of this week?"
        title = "📊 Weekly review"
    else:
        thoughts_label = f"🧠 Pensées capturées : {thoughts_count}"
        question = "Quelle est la chose dont tu es le plus fier cette semaine ?"
        title = "📊 Bilan de la semaine"

    text = f"{title}\n\n{habits_line}{thoughts_label}\n\n{question}"
    await _send(tenant["telegram_bot_token"], tenant["telegram_chat_id"], text)

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        await conn.execute(
            "INSERT INTO briefings (tenant_id,briefing_type,content) VALUES ($1,'weekly_review',$2)",
            tenant_id, text,
        )

async def _send(token, chat_id, text):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("Weekly review send failed: %s", e)
