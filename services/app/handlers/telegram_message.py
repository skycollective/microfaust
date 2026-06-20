"""
Telegram message handler.
P-03: /forget hard-deletes rows (GDPR Art. 17)
P-04: bot token never logged

Intent routing: Claude LLM classifies every message — no regex for normal flow.
Only GDPR-critical /forget commands are pattern-matched directly for safety.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone

import asyncpg
import httpx

from calendar_client import list_events_range, create_event, format_events_for_telegram
import uuid as _uuid

logger = logging.getLogger(__name__)

# Only GDPR commands are hard-coded (P-03 safety — never route through LLM)
FORGET_ALL     = re.compile(r"^/forget\s+all$", re.IGNORECASE)
FORGET_TOPIC   = re.compile(r"^/forget\s+(.+)$", re.IGNORECASE)
CONFIRM_DELETE = re.compile(r"^confirm delete$", re.IGNORECASE)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

_INTENT_SYSTEM = (
    "You are an intent classifier for a personal assistant Telegram bot called MICROFAUST.\n\n"
    "The user sends a message. You must:\n"
    "1. Classify the intent\n"
    "2. Write a natural reply in LANG_LABEL\n\n"
    'Return ONLY valid JSON: {"intent": "<intent>", "reply": "<reply>", "data": {}}\n\n'
    "Available intents:\n"
    "- capture_thought: user is saving a note, idea, reflection, or context\n"
    "- capture_todo: user wants to add a task or reminder to their todo list.\n"
    "  TRIGGERS — any of: 'add todo', 'add to todo', 'add to my list', 'ajoute', 'ajouter', 'à faire', 'à ma liste',\n"
    "  'remind me', 'rappelle-moi', 'don't forget', 'n'oublie pas', or a message that is a clear actionable task.\n"
    "  Set data.content = the clean task only, stripped of any 'add todo / add to todo / ajoute' prefix.\n"
    "  Example: 'Add deposit Patrick check to todo' → data.content = 'Deposit Patrick check'\n"
    "  Example: 'Ajoute appeler le médecin à ma liste' → data.content = 'Appeler le médecin'\n"
    "- add_habit: user wants to track a recurring habit\n"
    "- complete_habit: user says they finished a habit (done meditating, finished run, etc.)\n"
    "- show_agenda: user wants to see calendar or meetings — set data.timeframe to 'today', 'tomorrow', or 'week'\n"
    "- show_thoughts: user wants to see their saved notes or ideas — set data.filter='todo' if user asks for todo/task list specifically\n"
    "- create_event: user wants to add a calendar event or meeting\n"
    "- invoke_council: user wants advice on a decision or multiple perspectives — keywords 'conseil' or 'comité' strongly indicate this\n"
    "- language_switch_en: user wants to switch to English\n"
    "- language_switch_fr: user wants to switch to French\n"
    "- evening_checkin: user is responding to the evening review — message contains gratitude, realisation, or reflection on the day\n"
    "- greeting: simple greeting with no actionable content\n"
    "- unknown: none of the above fits\n\n"
    "Rules:\n"
    "- For create_event: include title, start, end in ISO 8601 format in 'data' if mentioned\n"
    "- For show_agenda: always set data.timeframe — default 'today', use 'tomorrow' or 'week' if user says so\n"
    "- For capture_thought: confirm you saved it, briefly echo what you understood\n"
    "- For capture_todo: confirm the clean task was added; echo only the task name, not the full sentence\n"
    "- For invoke_council: classify IMMEDIATELY — never classify council context as capture_thought\n"
    "- For evening_checkin: reply should be empty string '' — the bot generates its own closing\n"
    "- For greetings: respond warmly and briefly\n"
    "- For unknown: acknowledge naturally, ask if there's something specific they need\n"
    "- Never mention 'intent' or 'classification' in your reply\n"
    "- Keep replies short — user is on their phone\n"
    "- Respond in LANG_LABEL"
)


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

    # Show typing indicator immediately so user knows we received the message
    await _typing(token, chat_id)

    # P-03: GDPR /forget handled directly — never through LLM
    if FORGET_ALL.match(text):
        await _forget_all_confirm(token, chat_id, lang)
        return
    if m := FORGET_TOPIC.match(text):
        await _forget_topic(pool, tenant_id, m.group(1), token, chat_id, lang)
        return
    if CONFIRM_DELETE.match(text):
        await _execute_forget_all(pool, tenant_id, token, chat_id, lang)
        return

    # All other messages: Claude routes intent
    result = await _route_with_claude(text, lang)
    intent = result.get("intent", "unknown")
    reply  = result.get("reply", _t(lang, "C'est note.", "Got it."))
    data   = result.get("data", {})

    if intent == "language_switch_en":
        await _set_language(pool, tenant_id, "en", token, chat_id)
    elif intent == "language_switch_fr":
        await _set_language(pool, tenant_id, "fr", token, chat_id)
    elif intent == "capture_todo":
        content = data.get("content") or text
        await _capture_thought(pool, tenant_id, content, token, chat_id, reply, is_todo=True)
    elif intent == "capture_thought":
        await _capture_thought(pool, tenant_id, text, token, chat_id, reply, is_todo=False)
    elif intent == "add_habit":
        await _add_habit_from_text(pool, tenant_id, text, token, chat_id, reply)
    elif intent == "complete_habit":
        await _log_habit_completion(pool, tenant_id, token, chat_id, reply)
    elif intent == "show_agenda":
        await _show_agenda(tenant_id, pool, token, chat_id, lang, data.get("timeframe", "today"))
    elif intent == "show_thoughts":
        await _show_thoughts(pool, tenant_id, token, chat_id, lang, todo_only=data.get("filter") == "todo")
    elif intent == "create_event":
        await _handle_calendar_create(tenant_id, pool, data, token, chat_id, lang)
    elif intent == "evening_checkin":
        await _evening_checkin_response(pool, tenant_id, text, token, chat_id, lang)
    elif intent == "invoke_council":
        await _invoke_council(text, token, chat_id, lang, pool=pool, tenant_id=tenant_id)
        await _mark_responded(pool, tenant_id)
    else:
        await _send(token, chat_id, reply)
        await _mark_responded(pool, tenant_id)


# ── Claude intent router ──────────────────────────────────────────────────────

async def _route_with_claude(text: str, lang: str) -> dict:
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set — falling back to unknown intent")
        return {"intent": "unknown", "reply": _t(lang, "C'est note.", "Got it.")}

    lang_label = "French" if lang == "fr" else "English"
    system = _INTENT_SYSTEM.replace("LANG_LABEL", lang_label)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 300,
                    "system": system,
                    "messages": [{"role": "user", "content": text}],
                },
            )
        if r.status_code != 200:
            logger.error("Anthropic API error %s", r.status_code)
            return {"intent": "unknown", "reply": _t(lang, "C'est note.", "Got it.")}

        content = r.json()["content"][0]["text"].strip()
        # Strip code fences
        if content.startswith("```"):
            content = re.sub(r"^```\w*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
        content = content.strip()
        # Extract first JSON object if surrounded by extra text
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            content = m.group(0)
        parsed = json.loads(content)
        # Normalise keys — Claude sometimes returns '"intent"' with embedded quotes
        normalised = {k.strip('"'): v for k, v in parsed.items()}
        if "intent" not in normalised:
            logger.error("Claude returned JSON without intent key: %s", parsed)
            return {"intent": "unknown", "reply": _t(lang, "C'est note.", "Got it.")}
        return normalised
    except Exception as e:
        logger.error("Claude intent routing failed: %s", e)
        return {"intent": "unknown", "reply": _t(lang, "C'est note.", "Got it.")}


# ── Intent handlers ───────────────────────────────────────────────────────────

async def _capture_thought(pool, tenant_id, text: str, token: str, chat_id: str, reply: str,
                           is_todo: bool = False):
    tags = ["todo"] if is_todo else []
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            await conn.execute(
                "INSERT INTO thoughts (tenant_id, content, tags) VALUES ($1,$2,$3)",
                tenant_id, text, tags,
            )
    except Exception as e:
        logger.error("capture_thought insert failed: %s", e)
    await _send(token, chat_id, reply)
    await _mark_responded(pool, tenant_id)


async def _add_habit_from_text(pool, tenant_id, text: str, token: str, chat_id: str, reply: str):
    name = re.sub(
        r"^(add habit|new habit|track|ajoute[rz]? (l.habitude|habitude)?|nouvelle habitude)\s*:?\s*",
        "", text, flags=re.IGNORECASE,
    ).strip() or text
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            await conn.execute(
                "INSERT INTO habits (tenant_id, name) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                tenant_id, name,
            )
    except Exception as e:
        logger.error("add_habit insert failed: %s", e)
    await _send(token, chat_id, reply)


async def _log_habit_completion(pool, tenant_id, token: str, chat_id: str, reply: str):
    today = datetime.now(timezone.utc).date()
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            habit = await conn.fetchrow(
                "SELECT id FROM habits WHERE tenant_id=$1 ORDER BY created_at DESC LIMIT 1",
                tenant_id,
            )
            if habit:
                await conn.execute(
                    """INSERT INTO habit_log (tenant_id, habit_id, completed_on)
                       VALUES ($1,$2,$3) ON CONFLICT DO NOTHING""",
                    tenant_id, habit["id"], today,
                )
    except Exception as e:
        logger.error("complete_habit log failed: %s", e)
    await _send(token, chat_id, reply)


_ETHICS_GUARDIAN = (
    "Responsable de l'Éthique",
    "I am the guardian of the user's declared values. For every decision I must: "
    "(1) explicitly check whether it is aligned or in conflict with their stated core values, "
    "(2) flag ethical blind spots or consequences for people involved, "
    "(3) identify whether external pressure, artificial urgency, or rationalisation is masking a values compromise, "
    "(4) conclude with a clear verdict — ALIGNED, PARTIAL CONFLICT, or DIRECT CONFLICT — with one explanatory sentence. "
    "I do not judge the decision itself: I reveal the gap between what the user is doing and what they declared they want to be. "
    "Respond in the same language as the rest of the council."
)

_DEFAULT_COUNCIL = [
    ("Dharma Guardian",    "Is this aligned with who you're becoming? Look for: integrity, values, long-term fulfilment. Is this a distraction disguised as opportunity?"),
    ("Market Capitalist",  "Will a stranger pay for this? Ignore passion — focus on revenue, demand, margins, distribution. Evaluate probabilities, not possibilities."),
    ("Frugal Innovator",   "What is the smallest experiment that validates this? What can be tested within 14 days at near-zero cost?"),
    ("Systems Architect",  "Does this create an asset or another job? Look for flywheels, network effects, compounding. Will this matter in 5 years?"),
    ("Shadow Hunter",      "What fear or desire is secretly driving this? Look for avoidance, validation-seeking, need for certainty. If the fear disappeared, what would change?"),
    ("Future Self (50)",   "Looking back 20 years, which path would matter most? What will future you thank present you for?"),
    ("Devil's Advocate",   "Make the strongest case AGAINST this decision. What is the kill shot? The single most dangerous flaw — drive it all the way in."),
    ("Distribution Master","How will customers actually arrive? What distribution advantage exists BEFORE building begins? Given history of underinvesting in distribution, this voice carries extra weight."),
]

_KNOWN_FAILURE_MODES = (
    "Known failure modes to watch for: builds before validating, underinvests in distribution and sales, "
    "diversifies too early, too many parallel projects, seeks certainty before acting, "
    "meaning before monetisation, community before business model, evaluates possibilities instead of probabilities."
)


async def _invoke_council(text: str, token: str, chat_id: str, lang: str,
                          pool: asyncpg.Pool | None = None, tenant_id=None):
    if not ANTHROPIC_API_KEY:
        await _send(token, chat_id, _t(lang,
            "Le comité n'est pas disponible pour l'instant.",
            "The council is unavailable right now."))
        return

    # Fetch custom council members from skills table if available
    advisors = []
    if pool and tenant_id:
        try:
            async with pool.acquire() as conn:
                await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
                rows = await conn.fetch(
                    "SELECT name, system_prompt FROM skills WHERE tenant_id=$1 AND active=true ORDER BY created_at",
                    tenant_id,
                )
                advisors = [(r["name"], r["system_prompt"]) for r in rows]
        except Exception as e:
            logger.error("Council fetch from skills failed: %s", e)

    if not advisors:
        advisors = _DEFAULT_COUNCIL

    # Ethics Guardian always participates in every council
    full_council = [_ETHICS_GUARDIAN] + [a for a in advisors if a[0] != _ETHICS_GUARDIAN[0]]

    lang_label = "French" if lang == "fr" else "English"
    advisor_list = "\n".join(f"- {name}: {prompt}" for name, prompt in full_council)

    council_system = (
        f"You are a personal decision council. {_KNOWN_FAILURE_MODES}\n\n"
        f"Council members and their lens:\n{advisor_list}\n\n"
        "Rules:\n"
        "- Each advisor speaks from their lens only — no hedging, no balance\n"
        f"- '{_ETHICS_GUARDIAN[0]}' ALWAYS speaks first and in full — never skip or summarise them\n"
        "- Then pick the 2-3 most relevant other advisors for this specific question\n"
        "- Format: [Advisor Name]: [1-2 sentence verdict. Direct. No hedging.]\n"
        "- End with one line: DECISION: [single recommendation, no 'it depends']\n"
        f"- No markdown. No asterisks. Respond in {lang_label}.\n"
        "- Optimising for: freedom by design, financial resilience, compounding assets.\n"
        "- NEVER ask for clarification. Make reasonable assumptions and give the verdict immediately.\n"
        "- If context is thin, state your assumption in one line then give the verdict."
    )

    try:
        await _typing(token, chat_id)  # refresh indicator before slow Claude call
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 500,
                    "system": council_system,
                    "messages": [{"role": "user", "content": text}],
                },
            )
        if r.status_code == 200:
            await _send(token, chat_id, r.json()["content"][0]["text"].strip())
            return
    except Exception as e:
        logger.error("Council invocation failed: %s", e)
    await _send(token, chat_id, _t(lang,
        "Le comité n'est pas disponible pour l'instant.",
        "The council is unavailable right now."))


# ── GDPR handlers ─────────────────────────────────────────────────────────────

async def _forget_all_confirm(token, chat_id, lang="fr"):
    await _send(token, chat_id, _t(lang,
        "Cette action supprime toutes vos données définitivement.\n"
        "Répondez CONFIRM DELETE pour continuer.",
        "This will permanently delete all your data.\n"
        "Reply CONFIRM DELETE to proceed.",
    ))


async def _execute_forget_all(pool, tenant_id, token, chat_id, lang="fr"):
    # P-03: hard delete only
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        await conn.execute("DELETE FROM thoughts WHERE tenant_id=$1", tenant_id)
        await conn.execute("DELETE FROM habits WHERE tenant_id=$1", tenant_id)
        await conn.execute("DELETE FROM briefings WHERE tenant_id=$1", tenant_id)
    await _send(token, chat_id, _t(lang,
        "Toutes vos données ont été supprimées.",
        "All your data has been permanently deleted.",
    ))


async def _forget_topic(pool, tenant_id, topic, token, chat_id, lang="fr"):
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        result = await conn.execute(
            "DELETE FROM thoughts WHERE tenant_id=$1 AND content ILIKE $2",
            tenant_id, f"%{topic}%",
        )
    count = int(result.split()[-1]) if result else 0
    await _send(token, chat_id, _t(lang,
        f"{count} souvenir(s) sur '{topic}' supprimé(s).",
        f"{count} memory item(s) about '{topic}' deleted.",
    ))


# ── Evening check-in ──────────────────────────────────────────────────────────

_EVENING_REFLECTION_SYSTEM = (
    "You are a wise evening companion. The user has just shared their evening reflection.\n\n"
    "Write ONE closing message in the user's language. Total length: 120-220 characters.\n\n"
    "Structure:\n"
    "1. ACKNOWLEDGEMENT: one short sentence reflecting the emotional tone or theme of the day. Do not repeat the journal entries.\n"
    "2. TRANSITION: choose one of: 'A thought for the evening:', 'As night falls:', 'Before you rest:', "
    "'Tonight\\'s image:', 'A small wisdom:'\n"
    "3. WISDOM: one metaphor, image, or observation. Inspired by nature, seasons, rivers, stars, gardens, "
    "craftsmanship, poetry, myths, or contemplative traditions. Should subtly resonate with the user's realisation.\n\n"
    "Constraints:\n"
    "- Do not give advice\n"
    "- Do not ask questions\n"
    "- Do not analyze\n"
    "- Do not sound like a coach, therapist, productivity app, or fortune cookie\n"
    "- Do not quote famous people\n"
    "- Avoid clichés\n"
    "- Leave space for contemplation\n"
    "- Sound simple, timeless, and quietly poetic\n\n"
    "Example:\n"
    "You found gratitude in learning, clarity, and rest today.\n"
    "A thought for the evening:\n"
    "A seed does not grow faster by being planted in seven gardens.\n\n"
    "Return only the final message."
)


async def _evening_checkin_response(pool, tenant_id, text: str, token: str, chat_id: str, lang: str):
    # Save the reflection as a thought tagged evening_checkin
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            await conn.execute(
                "INSERT INTO thoughts (tenant_id, content, tags) VALUES ($1,$2,$3)",
                tenant_id, text, ["evening_checkin"],
            )
    except Exception as e:
        logger.error("evening_checkin save failed: %s", e)

    # Generate poetic closing with Claude
    closing = None
    if ANTHROPIC_API_KEY:
        try:
            await _typing(token, chat_id)  # refresh before slow Claude call
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 200,
                        "system": _EVENING_REFLECTION_SYSTEM,
                        "messages": [{"role": "user", "content": text}],
                    },
                )
            if r.status_code == 200:
                closing = r.json()["content"][0]["text"].strip()
        except Exception as e:
            logger.error("Evening reflection generation failed: %s", e)

    if closing:
        await _send(token, chat_id, closing)
    await _mark_responded(pool, tenant_id)


# ── Calendar ──────────────────────────────────────────────────────────────────

async def _show_agenda(tenant_id, pool, token: str, chat_id: str, lang: str = "fr",
                       timeframe: str = "today"):
    events = await list_events_range(_uuid.UUID(str(tenant_id)), pool, timeframe)
    headers = {
        "today":    _t(lang, "Vos réunions aujourd'hui :", "Your meetings today:"),
        "tomorrow": _t(lang, "Vos réunions demain :", "Your meetings tomorrow:"),
        "week":     _t(lang, "Vos réunions cette semaine :", "Your meetings this week:"),
    }
    header = headers.get(timeframe, headers["today"])
    await _send(token, chat_id, header + "\n\n" + format_events_for_telegram(events))


async def _show_thoughts(pool, tenant_id, token: str, chat_id: str, lang: str = "fr",
                         todo_only: bool = False):
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            if todo_only:
                rows = await conn.fetch(
                    "SELECT content FROM thoughts WHERE tenant_id=$1 AND 'todo'=ANY(tags) ORDER BY created_at DESC LIMIT 20",
                    tenant_id,
                )
            else:
                rows = await conn.fetch(
                    "SELECT content FROM thoughts WHERE tenant_id=$1 "
                    "AND NOT ('todo'=ANY(coalesce(tags,'{}'))) "
                    "AND NOT ('evening_checkin'=ANY(coalesce(tags,'{}'))) "
                    "ORDER BY created_at DESC LIMIT 10",
                    tenant_id,
                )
    except Exception as e:
        logger.error("show_thoughts failed: %s", e)
        rows = []
    if not rows:
        if todo_only:
            await _send(token, chat_id, _t(lang, "Aucune tâche en cours.", "No tasks yet."))
        else:
            await _send(token, chat_id, _t(lang, "Aucune note sauvegardée.", "No saved notes yet."))
        return
    lines = [f"- {r['content']}" for r in rows]
    header = _t(lang, "Vos taches :" if todo_only else "Vos notes :", "Your tasks:" if todo_only else "Your notes:")
    await _send(token, chat_id, header + "\n\n" + "\n".join(lines))


async def _handle_calendar_create(tenant_id, pool, data: dict,
                                   token: str, chat_id: str, lang: str = "fr"):
    title = data.get("title") or data.get("summary")
    if title and data.get("start"):
        result = await create_event(
            _uuid.UUID(str(tenant_id)), pool,
            summary=title,
            start=data["start"],
            end=data.get("end", data["start"]),
            description=data.get("description", ""),
        )
        if result:
            await _send(token, chat_id, _t(lang,
                f"Événement créé : {title}",
                f"Event created: {title}",
            ))
            return
    await _send(token, chat_id, _t(lang,
        "Pour créer un événement :\nEx: Réunion client demain à 14h pendant 1h",
        "To create an event:\nEx: Client meeting tomorrow at 2pm for 1 hour",
    ))


# ── Language ──────────────────────────────────────────────────────────────────

async def _set_language(pool, tenant_id, lang: str, token: str, chat_id: str):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE tenants SET language=$1 WHERE id=$2", lang, tenant_id)
    if lang == "en":
        await _send(token, chat_id, "Switched to English. I'll reply in English from now on.")
    else:
        await _send(token, chat_id, "Passé en français. Je répondrai en français désormais.")


# ── Utilities ─────────────────────────────────────────────────────────────────

def _t(lang: str, fr: str, en: str) -> str:
    return en if lang == "en" else fr


async def _typing(token: str, chat_id: str):
    """Send typing indicator — visible for ~5 seconds."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(
                f"https://api.telegram.org/bot{token}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
            )
    except Exception:
        pass  # never block on typing indicator failure


async def _mark_responded(pool, tenant_id):
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE briefings SET user_responded=true
               WHERE tenant_id=$1 AND created_at > NOW()-INTERVAL '4 hours'
                 AND user_responded=false""",
            tenant_id,
        )


async def _send(token: str, chat_id: str, text: str):
    # P-04: never log token
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("Telegram send failed chat=%s: %s", chat_id, e)
