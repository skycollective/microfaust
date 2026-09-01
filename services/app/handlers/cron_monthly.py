"""1st of month: macro goals review."""
import logging
from datetime import date
import asyncpg, httpx

logger = logging.getLogger(__name__)


async def handle_cron_monthly(pool: asyncpg.Pool, job: asyncpg.Record):
    tenant_id = job["tenant_id"]
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token, telegram_chat_id, language FROM tenants WHERE id=$1", tenant_id
        )
        if not tenant or not tenant["telegram_chat_id"]:
            return
        already = await conn.fetchval(
            "SELECT 1 FROM briefings WHERE tenant_id=$1 AND briefing_type='monthly_review' "
            "AND date_trunc('month', created_at) = date_trunc('month', NOW())",
            tenant_id,
        )
        if already:
            return

        goals = await conn.fetch(
            "SELECT description, project_name, deadline, current_state FROM macro_goals "
            "WHERE tenant_id=$1 AND status='active' ORDER BY deadline NULLS LAST",
            tenant_id,
        )

    lang = tenant["language"] or "fr"

    if goals:
        lines = []
        for g in goals:
            line = f"• {g['description']}"
            if g["project_name"]:
                line += f" [{g['project_name']}]"
            if g["deadline"]:
                months_left = (g["deadline"].year - date.today().year) * 12 + \
                              (g["deadline"].month - date.today().month)
                line += f" — {months_left}m" if lang == "en" else f" — {months_left} mois"
            lines.append(line)

        goal_block = "\n".join(lines)
        if lang == "en":
            text = (
                f"📆 Monthly goals review — {date.today().strftime('%B %Y')}\n\n"
                f"Your macro goals:\n{goal_block}\n\n"
                f"What advanced this month? What needs a reset?"
            )
        else:
            text = (
                f"📆 Bilan mensuel — {date.today().strftime('%B %Y')}\n\n"
                f"Tes objectifs macro :\n{goal_block}\n\n"
                f"Qu'est-ce qui a avancé ce mois-ci ? Qu'est-ce qui doit être recalibré ?"
            )
    else:
        if lang == "en":
            text = (
                f"📆 Monthly review — {date.today().strftime('%B %Y')}\n\n"
                f"No macro goals defined yet.\n"
                f"Reply 'add goal [description] by [date]' to set one."
            )
        else:
            text = (
                f"📆 Bilan mensuel — {date.today().strftime('%B %Y')}\n\n"
                f"Aucun objectif macro défini.\n"
                f"Réponds 'ajouter objectif [description] pour [date]' pour en définir un."
            )

    await _send(tenant["telegram_bot_token"], tenant["telegram_chat_id"], text)

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        await conn.execute(
            "INSERT INTO briefings (tenant_id, briefing_type, content) VALUES ($1,'monthly_review',$2)",
            tenant_id, text,
        )


async def _send(token, chat_id, text):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("Monthly review send failed: %s", e)
