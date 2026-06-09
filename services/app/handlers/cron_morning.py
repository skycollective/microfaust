import logging
from datetime import date
import asyncpg, httpx

logger = logging.getLogger(__name__)

async def handle_cron_morning(pool: asyncpg.Pool, job: asyncpg.Record):
    tenant_id = job["tenant_id"]
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token, telegram_chat_id FROM tenants WHERE id=$1", tenant_id
        )
        if not tenant or not tenant["telegram_chat_id"]:
            return
        already = await conn.fetchval(
            "SELECT 1 FROM briefings WHERE tenant_id=$1 AND briefing_type='morning' AND created_at::date=$2",
            tenant_id, date.today(),
        )
        if already:
            return
        habits = await conn.fetch(
            "SELECT name FROM habits WHERE tenant_id=$1 AND active=true AND time_of_day='morning'",
            tenant_id,
        )

    token   = tenant["telegram_bot_token"]
    chat_id = tenant["telegram_chat_id"]

    habit_lines = "\n".join(f"• {h['name']}" for h in habits) or "• Aucune habitude ce matin"
    weather = await _get_weather()

    text = (
        "☀️ Bonjour !\n\n"
        "📅 Agenda du jour : /agenda pour voir vos réunions\n\n"
        f"🏃 Habitudes du matin :\n{habit_lines}\n\n"
        f"🌤️ {weather}\n\n"
        "Bonne journée !"
    )
    await _send(token, chat_id, text)

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        await conn.execute(
            "INSERT INTO briefings (tenant_id,briefing_type,content) VALUES ($1,'morning',$2)",
            tenant_id, text,
        )

async def _get_weather() -> str:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(
                "https://api.open-meteo.com/v1/forecast"
                "?latitude=48.85&longitude=2.35"
                "&hourly=precipitation_probability&forecast_days=1&timezone=Europe/Paris"
            )
            data = r.json()
            probs = data["hourly"]["precipitation_probability"]
            if max(probs) >= 30:
                return "Pluie possible aujourd'hui — prenez un parapluie"
            return "Pas de pluie prévue"
    except Exception:
        return "Météo indisponible"

async def _send(token, chat_id, text):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("Morning briefing send failed: %s", e)
