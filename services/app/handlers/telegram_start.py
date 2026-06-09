import json, logging
import asyncpg, httpx

logger = logging.getLogger(__name__)

async def handle_telegram_start(pool: asyncpg.Pool, job: asyncpg.Record):
    payload = json.loads(job["payload"])
    chat_id = payload["chat_id"]
    async with pool.acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token FROM tenants WHERE id=$1", job["tenant_id"]
        )
    if not tenant:
        return
    await _send(tenant["telegram_bot_token"], chat_id,
        "Bonjour ! Je suis votre assistant MicroFaust.\n\n"
        "Je vous enverrai chaque matin un briefing avec votre agenda, "
        "vos habitudes du jour et la météo.\n\n"
        "Commandes disponibles :\n"
        "/decide — consulter votre conseil\n"
        "/agenda — voir votre journée\n"
        "/souviens [sujet] — chercher dans votre mémoire\n"
        "/forget [sujet] — supprimer des souvenirs\n"
        "Message libre — capture en mémoire\n\n"
        "À demain matin ! 🌅"
    )

async def _send(token: str, chat_id: str, text: str):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("Telegram send failed chat=%s: %s", chat_id, e)
