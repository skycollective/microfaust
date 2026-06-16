"""
Telegram message handler.
P-03: /forget hard-deletes rows (GDPR Art. 17)
P-04: bot token never logged
"""
import json, logging, re
import asyncpg, httpx

from calendar_client import list_today_events, create_event, format_events_for_telegram

logger = logging.getLogger(__name__)

FORGET_ALL   = re.compile(r"^/forget\s+all$", re.IGNORECASE)
FORGET_TOPIC = re.compile(r"^/forget\s+(.+)$", re.IGNORECASE)
HABIT_ADD    = re.compile(r"^add habit:\s*(.+)$", re.IGNORECASE)
DECIDE_CMD   = re.compile(r"^/decide\s*(.*)", re.IGNORECASE | re.DOTALL)
AGENDA_CMD   = re.compile(r"^/agenda$", re.IGNORECASE)
# Natural language patterns for calendar intent
CALENDAR_VIEW = re.compile(
    r"(mon agenda|mes reunions?|quelles? reunions?|qu.est.ce que j.ai|qu.ai.je|programme du jour|what.*meeting|my (schedule|calendar|meetings?))",
    re.IGNORECASE,
)
CALENDAR_CREATE = re.compile(
    r"(cree[rz]?|ajoute[rz]?|planifie[rz]?|schedule|add.*meeting|reunions? avec|rendez.?vous avec)",
    re.IGNORECASE,
)


async def handle_telegram_message(pool: asyncpg.Pool, job: asyncpg.Record):
    payload = json.loads(job["payload"])
    tenant_id = job["tenant_id"]
    chat_id   = payload["chat_id"]
    text      = payload.get("text", "").strip()

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token, composio_entity_id FROM tenants WHERE id=$1", tenant_id
        )
    if not tenant:
        return

    token     = tenant["telegram_bot_token"]
    entity_id = tenant["composio_entity_id"]

    if FORGET_ALL.match(text):
        await _forget_all_confirm(pool, tenant_id, token, chat_id)
    elif m := FORGET_TOPIC.match(text):
        await _forget_topic(pool, tenant_id, m.group(1), token, chat_id)
    elif m := HABIT_ADD.match(text):
        await _add_habit(pool, tenant_id, m.group(1), token, chat_id)
    elif DECIDE_CMD.match(text):
        await _send(token, chat_id,
            "Conseil en cours de deliberation... (integration LLM a venir)")
    elif AGENDA_CMD.match(text) or CALENDAR_VIEW.search(text):
        await _show_agenda(entity_id, token, chat_id)
    elif CALENDAR_CREATE.search(text):
        await _handle_calendar_create(entity_id, text, token, chat_id)
    else:
        await _send(token, chat_id, "Capture en memoire.")
        await _mark_responded(pool, tenant_id)


async def _forget_all_confirm(pool, tenant_id, token, chat_id):
    await _send(token, chat_id,
        "⚠️ Cette action supprime toutes vos données définitivement.\n"
        "Répondez CONFIRMER SUPPRESSION pour continuer."
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO job_queue (tenant_id, job_type, payload) VALUES ($1,'await_forget_confirm','{}')",
            tenant_id,
        )


async def _forget_topic(pool, tenant_id, topic, token, chat_id):
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        result = await conn.execute(
            "DELETE FROM thoughts WHERE tenant_id=$1 AND content ILIKE $2",
            tenant_id, f"%{topic}%",
        )
    count = int(result.split()[-1]) if result else 0
    await _send(token, chat_id,
        f"✅ {count} souvenir(s) sur '{topic}' supprimé(s) définitivement."
    )


async def _add_habit(pool, tenant_id, name, token, chat_id):
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        await conn.execute(
            "INSERT INTO habits (tenant_id, name) VALUES ($1,$2)", tenant_id, name.strip()
        )
    await _send(token, chat_id, f"✅ Habitude ajoutée : {name.strip()}")


async def _mark_responded(pool, tenant_id):
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE briefings SET user_responded=true
               WHERE tenant_id=$1 AND created_at > NOW()-INTERVAL '4 hours'
                 AND user_responded=false""",
            tenant_id,
        )


async def _show_agenda(entity_id: str | None, token: str, chat_id: str):
    if not entity_id:
        await _send(token, chat_id,
            "Google Calendar pas encore connecte.\n"
            "Connectez-le sur microfaust.vercel.app dans la section Agenda."
        )
        return
    events = await list_today_events(entity_id)
    text = "Vos reunions aujourd'hui :\n\n" + format_events_for_telegram(events)
    await _send(token, chat_id, text)


async def _handle_calendar_create(entity_id: str | None, text: str,
                                   token: str, chat_id: str):
    if not entity_id:
        await _send(token, chat_id,
            "Google Calendar pas encore connecte.\n"
            "Connectez-le sur microfaust.vercel.app dans la section Agenda."
        )
        return
    # Ask Claude to parse the intent — for now send a clear prompt back
    await _send(token, chat_id,
        "Pour creer un evenement, utilisez ce format :\n"
        "Reunions: [titre] le [date] de [heure debut] a [heure fin]\n\n"
        "Exemple : Reunions: Demo client le 20 juin de 14h a 15h"
    )


async def _send(token: str, chat_id: str, text: str):
    # P-04: never log token
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("Telegram send failed chat=%s: %s", chat_id, e)
