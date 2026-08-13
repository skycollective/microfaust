"""Sunday PM: prompt the user to set this week's 3 outcomes."""
import logging
from datetime import date, timedelta
import asyncpg, httpx

logger = logging.getLogger(__name__)

def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def handle_cron_sunday_pm(pool: asyncpg.Pool, job: asyncpg.Record):
    tenant_id = job["tenant_id"]
    this_monday = _week_start(date.today() + timedelta(days=1))  # next week's Monday

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token, telegram_chat_id, language FROM tenants WHERE id=$1", tenant_id
        )
        if not tenant or not tenant["telegram_chat_id"]:
            return
        already = await conn.fetchval(
            "SELECT 1 FROM briefings WHERE tenant_id=$1 AND briefing_type='sunday_pm' AND created_at::date=$2",
            tenant_id, date.today(),
        )
        if already:
            return

        # Show current active projects as context
        projects = await conn.fetch(
            "SELECT name, outcome FROM projects WHERE tenant_id=$1 AND status='active' ORDER BY created_at",
            tenant_id,
        )

    lang = tenant["language"] or "fr"

    if projects:
        proj_lines = "\n".join(
            f"• {p['name']}" + (f" → {p['outcome']}" if p['outcome'] else "")
            for p in projects
        )
        if lang == "en":
            context = f"Your active projects:\n{proj_lines}\n\n"
        else:
            context = f"Tes projets actifs :\n{proj_lines}\n\n"
    else:
        context = ""

    if lang == "en":
        text = (
            f"🗓 Next week's planning\n\n"
            f"{context}"
            f"What are your 3 most important outcomes for this week?\n\n"
            f"Reply with:\n"
            f"1. [outcome] — [project]\n"
            f"2. [outcome] — [project]\n"
            f"3. [outcome] — [project]"
        )
    else:
        text = (
            f"🗓 Planification de la semaine\n\n"
            f"{context}"
            f"Quels sont tes 3 objectifs les plus importants pour cette semaine ?\n\n"
            f"Réponds avec :\n"
            f"1. [objectif] — [projet]\n"
            f"2. [objectif] — [projet]\n"
            f"3. [objectif] — [projet]"
        )

    await _send(tenant["telegram_bot_token"], tenant["telegram_chat_id"], text)

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        await conn.execute(
            "INSERT INTO briefings (tenant_id,briefing_type,content) VALUES ($1,'sunday_pm',$2)",
            tenant_id, text,
        )


async def _send(token, chat_id, text):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("Sunday PM send failed: %s", e)
