import logging
from datetime import date
import asyncpg, httpx

from calendar_client import list_events_range, format_events_for_telegram

logger = logging.getLogger(__name__)

async def handle_cron_morning(pool: asyncpg.Pool, job: asyncpg.Record):
    tenant_id = job["tenant_id"]
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token, telegram_chat_id, language FROM tenants WHERE id=$1",
            tenant_id,
        )
        if not tenant or not tenant["telegram_chat_id"]:
            return
        already = await conn.fetchval(
            "SELECT 1 FROM briefings WHERE tenant_id=$1 AND briefing_type='morning' AND created_at::date=$2",
            tenant_id, date.today(),
        )
        if already:
            return
    token   = tenant["telegram_bot_token"]
    chat_id = tenant["telegram_chat_id"]
    lang    = tenant["language"] or "fr"

    weather = await _get_weather(lang)

    # Fetch calendar events if Google Calendar is connected
    import uuid
    events = await list_events_range(uuid.UUID(str(tenant_id)), pool, "today")
    if lang == "en":
        if events:
            agenda_section = f"📅 Today's meetings:\n{format_events_for_telegram(events)}"
        elif events is not None:
            agenda_section = "📅 No meetings today"
        else:
            agenda_section = "📅 Calendar: connect Google Calendar at microfaust.vercel.app"
        values_question = (
            "💎 Which of your values do you want to lead with today,\n"
            "and what's one concrete intention?"
        )
        greeting = "Good morning!"
    else:
        if events:
            agenda_section = f"📅 Réunions du jour :\n{format_events_for_telegram(events)}"
        elif events is not None:
            agenda_section = "📅 Pas de réunion aujourd'hui"
        else:
            agenda_section = "📅 Agenda : connectez Google Calendar sur microfaust.vercel.app"
        values_question = (
            "💎 Quelle valeur veux-tu incarner aujourd'hui,\n"
            "et quelle est ton intention concrète ?"
        )
        greeting = "Bonjour !"

    text = (
        f"{greeting}\n\n"
        f"{agenda_section}\n\n"
        f"{weather}\n\n"
        f"{values_question}"
    )
    await _send(token, chat_id, text)

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        await conn.execute(
            "INSERT INTO briefings (tenant_id,briefing_type,content) VALUES ($1,'morning',$2)",
            tenant_id, text,
        )

async def _get_weather(lang: str = "fr") -> str:
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
                return ("Rain likely today — bring an umbrella" if lang == "en"
                        else "Pluie possible aujourd'hui — prenez un parapluie")
            return "No rain forecast" if lang == "en" else "Pas de pluie prévue"
    except Exception:
        return "Weather unavailable" if lang == "en" else "Météo indisponible"

async def _send(token, chat_id, text):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("Morning briefing send failed: %s", e)
