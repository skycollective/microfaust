import logging
from datetime import date, timedelta
import asyncpg, httpx

logger = logging.getLogger(__name__)

def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def handle_cron_weekly_review(pool: asyncpg.Pool, job: asyncpg.Record):
    """Sunday AM: review last week's 3 outcomes + ask what happened."""
    tenant_id = job["tenant_id"]
    last_monday = _week_start(date.today()) - timedelta(weeks=1)

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token, telegram_chat_id, language FROM tenants WHERE id=$1", tenant_id
        )
        if not tenant or not tenant["telegram_chat_id"]:
            return
        already = await conn.fetchval(
            "SELECT 1 FROM briefings WHERE tenant_id=$1 AND briefing_type='weekly_review' AND created_at::date > $2",
            tenant_id, last_monday,
        )
        if already:
            return

        outcomes = await conn.fetch(
            "SELECT rank, description, project_name, status FROM weekly_outcomes "
            "WHERE tenant_id=$1 AND week_start=$2 ORDER BY rank",
            tenant_id, last_monday,
        )

    lang = tenant["language"] or "fr"
    status_emoji = {"done": "🟢", "in_progress": "🟡", "carried_forward": "🔵", "pending": "🔴"}

    if outcomes:
        outcome_lines = "\n".join(
            f"{status_emoji.get(o['status'], '🔴')} {o['rank']}. {o['description']}"
            + (f"  [{o['project_name']}]" if o['project_name'] else "")
            for o in outcomes
        )
        if lang == "en":
            text = (
                f"📊 Last week's commitments\n\n"
                f"{outcome_lines}\n\n"
                f"What happened? What got in the way?"
            )
        else:
            text = (
                f"📊 Engagements de la semaine passée\n\n"
                f"{outcome_lines}\n\n"
                f"Qu'est-ce qui s'est passé ? Qu'est-ce qui a bloqué ?"
            )
    else:
        if lang == "en":
            text = (
                "📊 Weekly review\n\n"
                "No weekly outcomes were set last week.\n\n"
                "This evening I'll ask you to set 3 outcomes for this week."
            )
        else:
            text = (
                "📊 Bilan de la semaine\n\n"
                "Aucun objectif n'avait été défini la semaine dernière.\n\n"
                "Ce soir je t'inviterai à définir 3 objectifs pour cette semaine."
            )

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
