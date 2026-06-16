import json, logging
import asyncpg, httpx

logger = logging.getLogger(__name__)

async def handle_telegram_start(pool: asyncpg.Pool, job: asyncpg.Record):
    payload = json.loads(job["payload"])
    chat_id   = payload["chat_id"]
    tenant_id = job["tenant_id"]

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token, language FROM tenants WHERE id=$1", tenant_id
        )
        # Deduplicate — skip if already sent a start message in the last hour
        already = await conn.fetchval(
            """SELECT 1 FROM briefings
               WHERE tenant_id=$1 AND briefing_type='start'
                 AND created_at > NOW() - INTERVAL '1 hour'""",
            tenant_id,
        )

    if not tenant or already:
        return

    lang = tenant["language"] or "fr"

    if lang == "en":
        text = (
            "Hi! I'm your MicroFaust assistant.\n\n"
            "Every morning I'll send you a briefing with your meetings, habits, and weather.\n\n"
            "Commands:\n"
            "/decide — consult your personal council\n"
            "/agenda — see today's schedule\n"
            "/forget [topic] — delete memories\n"
            "Free message — I'll remember it\n\n"
            "See you tomorrow morning!"
        )
    else:
        text = (
            "Bonjour ! Je suis votre assistant MicroFaust.\n\n"
            "Chaque matin je vous enverrai un briefing avec vos reunions, habitudes et meteo.\n\n"
            "Commandes :\n"
            "/decide — consulter votre conseil\n"
            "/agenda — voir votre journee\n"
            "/forget [sujet] — supprimer des souvenirs\n"
            "Message libre — je m'en souviens\n\n"
            "A demain matin !"
        )

    await _send(tenant["telegram_bot_token"], chat_id, text)

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        await conn.execute(
            "INSERT INTO briefings (tenant_id, briefing_type, content) VALUES ($1,'start',$2)",
            tenant_id, text,
        )


async def _send(token: str, chat_id: str, text: str):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("Telegram send failed chat=%s: %s", chat_id, e)
