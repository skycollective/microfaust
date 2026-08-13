import logging
from datetime import date, timedelta
import asyncpg, httpx

from calendar_client import list_events_range, format_events_for_telegram

logger = logging.getLogger(__name__)

def _week_start(d: date) -> date:
    """Return the Monday of the week containing d."""
    return d - timedelta(days=d.weekday())


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

        # Fetch this week's outcomes
        ws = _week_start(date.today())
        outcomes = await conn.fetch(
            "SELECT rank, description, project_name, status FROM weekly_outcomes "
            "WHERE tenant_id=$1 AND week_start=$2 ORDER BY rank",
            tenant_id, ws,
        )

        # Upcoming deadlines (todos with content containing a date or deadline mention)
        # Simple approach: surface todos that are overdue or due soon
        urgent_todos = await conn.fetch(
            "SELECT content FROM thoughts WHERE tenant_id=$1 AND 'todo'=ANY(coalesce(tags,'{}')) "
            "AND (content ILIKE '%today%' OR content ILIKE '%tomorrow%' OR content ILIKE '%urgent%' "
            "     OR content ILIKE '%deadline%' OR content ILIKE '%sept%' OR content ILIKE '%august%' "
            "     OR content ILIKE '%demain%' OR content ILIKE '%urgent%' OR content ILIKE '%échéance%') "
            "ORDER BY created_at ASC LIMIT 3",
            tenant_id,
        )

    token   = tenant["telegram_bot_token"]
    chat_id = tenant["telegram_chat_id"]
    lang    = tenant["language"] or "fr"

    # Calendar events
    import uuid
    events = await list_events_range(uuid.UUID(str(tenant_id)), pool, "today")

    if lang == "en":
        if events:
            agenda_section = f"📅 Today:\n{format_events_for_telegram(events)}"
        elif events is not None:
            agenda_section = "📅 No meetings today"
        else:
            agenda_section = "📅 Connect Google Calendar at microfaust.vercel.app"

        if outcomes:
            status_emoji = {"done": "🟢", "in_progress": "🟡", "carried_forward": "🔵", "pending": "⬜"}
            outcome_lines = "\n".join(
                f"{status_emoji.get(o['status'], '⬜')} {o['rank']}. {o['description']}"
                + (f"  [{o['project_name']}]" if o['project_name'] else "")
                for o in outcomes
            )
            weekly_section = f"🎯 This week's 3:\n{outcome_lines}"
        else:
            weekly_section = "🎯 No weekly outcomes set yet — reply 'set weekly outcomes' to add them"

        deadline_section = ""
        if urgent_todos:
            deadline_lines = "\n".join(f"⚠️ {r['content']}" for r in urgent_todos)
            deadline_section = f"\n\n{deadline_lines}"

        focus_question = "What is your main focus today?"
        greeting = "Good morning!"

    else:
        if events:
            agenda_section = f"📅 Aujourd'hui :\n{format_events_for_telegram(events)}"
        elif events is not None:
            agenda_section = "📅 Pas de réunion aujourd'hui"
        else:
            agenda_section = "📅 Connectez Google Calendar sur microfaust.vercel.app"

        if outcomes:
            status_emoji = {"done": "🟢", "in_progress": "🟡", "carried_forward": "🔵", "pending": "⬜"}
            outcome_lines = "\n".join(
                f"{status_emoji.get(o['status'], '⬜')} {o['rank']}. {o['description']}"
                + (f"  [{o['project_name']}]" if o['project_name'] else "")
                for o in outcomes
            )
            weekly_section = f"🎯 3 de cette semaine :\n{outcome_lines}"
        else:
            weekly_section = "🎯 Pas encore d'objectifs pour la semaine — répondez 'définir objectifs' pour en ajouter"

        deadline_section = ""
        if urgent_todos:
            deadline_lines = "\n".join(f"⚠️ {r['content']}" for r in urgent_todos)
            deadline_section = f"\n\n{deadline_lines}"

        focus_question = "Quel est ton intention principale aujourd'hui ?"
        greeting = "Bonjour !"

    text = (
        f"{greeting}\n\n"
        f"{agenda_section}\n\n"
        f"{weekly_section}"
        f"{deadline_section}\n\n"
        f"{focus_question}"
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
