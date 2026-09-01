"""Thursday morning: remind user of their weekly intention."""
import logging
from datetime import date, timedelta
import asyncpg, httpx

logger = logging.getLogger(__name__)

def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def handle_cron_thursday(pool: asyncpg.Pool, job: asyncpg.Record):
    tenant_id = job["tenant_id"]
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token, telegram_chat_id, language FROM tenants WHERE id=$1", tenant_id
        )
        if not tenant or not tenant["telegram_chat_id"]:
            return
        already = await conn.fetchval(
            "SELECT 1 FROM briefings WHERE tenant_id=$1 AND briefing_type='thursday_nudge' AND created_at::date=$2",
            tenant_id, date.today(),
        )
        if already:
            return

        # Fetch this week's top outcome as the "weekly intention"
        ws = _week_start(date.today())
        top_outcome = await conn.fetchrow(
            "SELECT description, project_name FROM weekly_outcomes "
            "WHERE tenant_id=$1 AND week_start=$2 AND rank=1",
            tenant_id, ws,
        )

    lang = tenant["language"] or "fr"

    if top_outcome:
        outcome = top_outcome["description"]
        proj = f" [{top_outcome['project_name']}]" if top_outcome["project_name"] else ""
        if lang == "en":
            text = (
                f"🔔 Mid-week check-in\n\n"
                f"Your #1 intention this week:{proj}\n{outcome}\n\n"
                f"Still on track?"
            )
        else:
            text = (
                f"🔔 Point de mi-semaine\n\n"
                f"Ton intention principale cette semaine :{proj}\n{outcome}\n\n"
                f"Tu es toujours sur la bonne voie ?"
            )
    else:
        if lang == "en":
            text = "🔔 Mid-week check-in — no weekly outcomes set. Reply '1. [outcome]' to set them."
        else:
            text = "🔔 Point de mi-semaine — aucun objectif défini. Réponds '1. [objectif]' pour en définir."

    await _send(tenant["telegram_bot_token"], tenant["telegram_chat_id"], text)

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        await conn.execute(
            "INSERT INTO briefings (tenant_id, briefing_type, content) VALUES ($1,'thursday_nudge',$2)",
            tenant_id, text,
        )


async def _send(token, chat_id, text):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("Thursday nudge send failed: %s", e)
