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
LANG_EN      = re.compile(r"(switch to english|speak english|en anglais|change.*english|english please)", re.IGNORECASE)
LANG_FR      = re.compile(r"(switch to french|parle(z)? (en )?fran[cç]ais|en fran[cç]ais|french please)", re.IGNORECASE)
CALENDAR_VIEW = re.compile(
    r"(mon agenda|mes reunions?|quelles? reunions?|qu.est.ce que j.ai|qu.ai.je|programme du jour|what.*meeting|my (schedule|calendar|meetings?))",
    re.IGNORECASE,
)
CALENDAR_CREATE = re.compile(
    r"(cree[rz]?|ajoute[rz]?|planifie[rz]?|schedule|add.*meeting|reunions? avec|rendez.?vous avec)",
    re.IGNORECASE,
)

def _t(lang: str, fr: str, en: str) -> str:
    """Return French or English string based on tenant language."""
    return en if lang == 'en' else fr


async def handle_telegram_message(pool: asyncpg.Pool, job: asyncpg.Record):
    payload = json.loads(job["payload"])
    tenant_id = job["tenant_id"]
    chat_id   = payload["chat_id"]
    text      = payload.get("text", "").strip()

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token, composio_entity_id, language FROM tenants WHERE id=$1",
            tenant_id,
        )
    if not tenant:
        return

    token     = tenant["telegram_bot_token"]
    entity_id = tenant["composio_entity_id"]
    lang      = tenant["language"] or "fr"

    if LANG_EN.search(text):
        await _set_language(pool, tenant_id, "en", token, chat_id)
        return
    elif LANG_FR.search(text):
        await _set_language(pool, tenant_id, "fr", token, chat_id)
        return
    elif FORGET_ALL.match(text):
        await _forget_all_confirm(pool, tenant_id, token, chat_id, lang)
    elif m := FORGET_TOPIC.match(text):
        await _forget_topic(pool, tenant_id, m.group(1), token, chat_id, lang)
    elif m := HABIT_ADD.match(text):
        await _add_habit(pool, tenant_id, m.group(1), token, chat_id, lang)
    elif DECIDE_CMD.match(text):
        await _send(token, chat_id, _t(lang,
            "Conseil en cours... (bientot disponible)",
            "Council deliberating... (coming soon)"))
    elif AGENDA_CMD.match(text) or CALENDAR_VIEW.search(text):
        await _show_agenda(entity_id, token, chat_id, lang)
    elif CALENDAR_CREATE.search(text):
        await _handle_calendar_create(entity_id, text, token, chat_id, lang)
    else:
        await _send(token, chat_id, _t(lang, "C'est note.", "Got it."))
        await _mark_responded(pool, tenant_id)


async def _set_language(pool, tenant_id, lang: str, token: str, chat_id: str):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE tenants SET language=$1 WHERE id=$2", lang, tenant_id)
    if lang == "en":
        await _send(token, chat_id, "Switched to English. I'll reply in English from now on.")
    else:
        await _send(token, chat_id, "Passe en francais. Je repondrai en francais desormais.")


async def _forget_all_confirm(pool, tenant_id, token, chat_id, lang="fr"):
    await _send(token, chat_id, _t(lang,
        "Cette action supprime toutes vos donnees definitivamente.\n"
        "Repondez CONFIRMER SUPPRESSION pour continuer.",
        "This will permanently delete all your data.\n"
        "Reply CONFIRM DELETE to proceed.",
    ))
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO job_queue (tenant_id, job_type, payload) VALUES ($1,'await_forget_confirm','{}')",
            tenant_id,
        )


async def _forget_topic(pool, tenant_id, topic, token, chat_id, lang="fr"):
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        result = await conn.execute(
            "DELETE FROM thoughts WHERE tenant_id=$1 AND content ILIKE $2",
            tenant_id, f"%{topic}%",
        )
    count = int(result.split()[-1]) if result else 0
    await _send(token, chat_id, _t(lang,
        f"{count} souvenir(s) sur '{topic}' supprime(s).",
        f"{count} memory item(s) about '{topic}' deleted.",
    ))


async def _add_habit(pool, tenant_id, name, token, chat_id, lang="fr"):
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        await conn.execute(
            "INSERT INTO habits (tenant_id, name) VALUES ($1,$2)", tenant_id, name.strip()
        )
    await _send(token, chat_id, _t(lang,
        f"Habitude ajoutee : {name.strip()}",
        f"Habit added: {name.strip()}",
    ))


async def _mark_responded(pool, tenant_id):
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE briefings SET user_responded=true
               WHERE tenant_id=$1 AND created_at > NOW()-INTERVAL '4 hours'
                 AND user_responded=false""",
            tenant_id,
        )


async def _show_agenda(entity_id: str | None, token: str, chat_id: str, lang: str = "fr"):
    if not entity_id:
        await _send(token, chat_id, _t(lang,
            "Google Calendar pas encore connecte.\nConnectez-le sur microfaust.vercel.app dans la section Agenda.",
            "Google Calendar not connected yet.\nConnect it at microfaust.vercel.app in the Agenda section.",
        ))
        return
    events = await list_today_events(entity_id)
    header = _t(lang, "Vos reunions aujourd'hui :", "Your meetings today:")
    await _send(token, chat_id, header + "\n\n" + format_events_for_telegram(events))


async def _handle_calendar_create(entity_id: str | None, text: str,
                                   token: str, chat_id: str, lang: str = "fr"):
    if not entity_id:
        await _send(token, chat_id, _t(lang,
            "Google Calendar pas encore connecte.\nConnectez-le sur microfaust.vercel.app dans la section Agenda.",
            "Google Calendar not connected yet.\nConnect it at microfaust.vercel.app in the Agenda section.",
        ))
        return
    await _send(token, chat_id, _t(lang,
        "Pour creer un evenement :\nReunion: [titre] le [date] de [heure] a [heure]\nEx: Demo client le 20 juin de 14h a 15h",
        "To create an event:\nMeeting: [title] on [date] from [start] to [end]\nEx: Client demo on June 20 from 2pm to 3pm",
    ))


async def _send(token: str, chat_id: str, text: str):
    # P-04: never log token
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("Telegram send failed chat=%s: %s", chat_id, e)
