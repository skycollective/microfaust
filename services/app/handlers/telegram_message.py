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
from handlers.chat_agent import chat_reply
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
    "2. Write a short confirmation reply\n\n"
    'Return ONLY valid JSON: {"intent": "<intent>", "reply": "<reply>", "data": {}}\n\n'
    "Available intents:\n"
    "- capture_thought: user is saving a note, idea, reference, or inbox item (no hashtag, no YouTube URL, not a clear action).\n"
    "  Set data.content = the EXACT text the user wrote, unchanged.\n"
    "- capture_tagged: user saves something WITH a hashtag like #portfolio, #toread, #leela, #talan, etc.\n"
    "  TRIGGERS — message contains '#' followed by a word.\n"
    "  Set data.tags = list of tag words found (lowercase, without #). e.g. ['portfolio','leela']\n"
    "  Set data.content = the clean content only — strip ONLY the structural prefix ('add to #tag', 'store in #tag', 'ajoute à #tag') and the hashtags themselves. Keep everything else verbatim.\n"
    "  Example: 'Add to #portfolio ifef ai agent' → data.tags=['portfolio'], data.content='ifef ai agent'\n"
    "  Example: 'Store in #toread and #ideas https://example.com great article' → data.tags=['toread','ideas'], data.content='https://example.com great article'\n"
    "  NEVER save the question part of a message. 'Portfolio should also have #leela?' → this is a QUESTION, not a capture. Use unknown intent.\n"
    "- capture_todo: user wants to add an action/task.\n"
    "  TRIGGERS — explicit: 'add todo', 'add to todo', 'add task', 'ajoute', 'à faire', 'remind me', 'rappelle-moi'.\n"
    "  Also triggers when message is a clear project-linked action: 'Add to #leela todo pay Loli'.\n"
    "  Do NOT fire for messages that are questions or observations.\n"
    "  Set data.content = clean task text. Set data.project = project tag if mentioned (e.g. 'leela'), else empty.\n"
    "  Example: 'Add to #talan todo prepare pspo2 certification' → data.content='prepare pspo2 certification', data.project='talan'\n"
    "- capture_youtube: message contains a youtube.com or youtu.be URL.\n"
    "  Set data.content = the URL. Set data.title = any description the user added (or empty string).\n"
    "- capture_goal: user is defining a macro goal with a deadline.\n"
    "  TRIGGERS — 'add goal', 'macro goal', 'objectif', 'by [date] I want', 'for [date]', 'goal for June', or setting a long-term target.\n"
    "  Set data.description = the goal text. Set data.deadline = ISO date if mentioned. Set data.project_name = project if mentioned. Set data.current_state = current snapshot if mentioned.\n"
    "  Example: 'Goal for June 2027: 6k savings' → data.description='6k savings', data.deadline='2027-06-01', data.project_name=''\n"
    "- mark_done: user wants to mark a todo or note as done/completed.\n"
    "  TRIGGERS — 'done', 'mark done', 'marquer comme fait', 'completed', 'terminé', 'c'est fait', 'paid', 'payé' + reference to an item.\n"
    "  Set data.hint = the keyword/name of the item to mark done. Set data.project = project tag if mentioned.\n"
    "  Example: 'Mark done payment to Loli for #leela' → data.hint='payment to Loli', data.project='leela'\n"
    "- add_habit: user wants to track a recurring habit\n"
    "- show_agenda: user wants to see calendar or meetings — set data.timeframe to 'today', 'tomorrow', or 'week'\n"
    "- show_thoughts: user wants to see their saved notes (plain notes, no tag filter)\n"
    "- show_by_tag: user asks to see items with a specific hashtag or project.\n"
    "  TRIGGERS — 'show #tag', 'my #tag', 'what's in #tag', 'is [name] in portfolio', 'tasks under #tag', 'quoi sous #tag'.\n"
    "  Set data.tag = the tag word (lowercase, without #).\n"
    "- show_youtube: user asks to see their saved YouTube videos / watch list.\n"
    "- show_todos: user asks to see their task or todo list.\n"
    "  TRIGGERS — 'my todos', 'my tasks', 'task list', 'mes tâches', 'ma liste'\n"
    "- show_projects: user asks to see their active projects or portfolio.\n"
    "  TRIGGERS — 'my projects', 'show portfolio', 'mon portfolio', 'mes projets'\n"
    "- show_goals: user asks to see their macro goals.\n"
    "  TRIGGERS — 'my goals', 'macro goals', 'mes objectifs', 'objectifs macro', 'long term goals'\n"
    "- capture_project: user wants to create a project.\n"
    "  TRIGGERS — 'add project', 'new project', 'nouveau projet'.\n"
    "  Set data.name, data.outcome, data.horizon.\n"
    "- set_weekly_outcomes: user is defining their 3 outcomes for the week.\n"
    "  TRIGGERS — numbered list with items, or 'this week', 'weekly outcomes', 'objectifs de la semaine'.\n"
    "  Set data.outcomes = list of {rank, description, project_name}.\n"
    "- update_outcome_status: user reports progress on a weekly outcome.\n"
    "  Set data.description_hint = keyword. Set data.status = 'done' | 'in_progress' | 'carried_forward'.\n"
    "- what_now: user asks what to focus on right now.\n"
    "  TRIGGERS — 'what now', 'what should I do', 'quoi faire', 'que faire'\n"
    "- create_event: user wants to add a calendar event — include title, start, end ISO 8601 in data\n"
    "- invoke_council: user wants expert advice on a decision — 'conseil', 'comité', 'committee', 'advisory'\n"
    "- language_switch_en: user wants to switch to English\n"
    "- language_switch_fr: user wants to switch to French\n"
    "- evening_checkin: user is responding to the evening reflection — gratitude, learning, realisation\n"
    "- greeting: simple greeting with no actionable content\n"
    "- unknown: question, unclear intent, or conversation that doesn't fit above\n\n"
    "CAPTURE CONFIRMATION RULES (critical):\n"
    "- ALWAYS echo the exact saved content in the reply — never paraphrase or summarise\n"
    "- Format: 'Saved: [exact content]' or 'Todo: [exact task]' or '#tag: [exact content]'\n"
    "- Keep replies to 1 line — user is on their phone\n"
    "- Do NOT interpret, expand, or reword what the user said at capture time\n\n"
    "LANGUAGE RULES (critical):\n"
    "- Detect the language of the user's message and ALWAYS respond in THAT language\n"
    "- French message → French reply. English message → English reply. NO EXCEPTIONS.\n"
    "- Do not switch languages mid-reply or based on conversation history\n"
    "- Only fall back to LANG_LABEL when the message is a single emoji or truly ambiguous\n\n"
    "OTHER RULES:\n"
    "- capture_tagged takes priority over capture_todo whenever '#' is present and content is being saved\n"
    "- capture_youtube takes priority whenever a YouTube URL is present\n"
    "- Questions about the system ('is X in portfolio?', 'what categories exist?') → unknown, answer conversationally\n"
    "- For invoke_council: prepend 'Switching to advisory mode.' before the council response\n"
    "- For evening_checkin: reply = '' — bot generates its own closing\n"
    "- Never mention 'intent' or 'classification' in replies"
)


async def handle_telegram_message(pool: asyncpg.Pool, job: asyncpg.Record):
    payload = json.loads(job["payload"])
    tenant_id = job["tenant_id"]
    chat_id   = payload["chat_id"]
    text      = payload.get("text", "").strip()

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        tenant = await conn.fetchrow(
            "SELECT telegram_bot_token, telegram_chat_id, composio_entity_id, language FROM tenants WHERE id=$1",
            tenant_id,
        )
    if not tenant:
        return

    # Security: only the registered chat_id may interact with this tenant's bot
    if str(chat_id) != str(tenant["telegram_chat_id"]):
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
        await _capture_thought(pool, tenant_id, content, token, chat_id, reply, tags=["todo"])
    elif intent == "capture_tagged":
        tags = data.get("tags") or []
        content = data.get("content") or text
        await _capture_thought(pool, tenant_id, content, token, chat_id, reply, tags=tags)
    elif intent == "capture_youtube":
        content = data.get("content") or text
        title = data.get("title", "")
        saved = f"{title} {content}".strip() if title else content
        await _capture_thought(pool, tenant_id, saved, token, chat_id, reply, tags=["youtube", "watchlist"])
    elif intent == "capture_thought":
        await _capture_thought(pool, tenant_id, text, token, chat_id, reply, tags=[])
    elif intent == "add_habit":
        await _add_habit_from_text(pool, tenant_id, text, token, chat_id, reply)
    elif intent == "complete_habit":
        await _log_habit_completion(pool, tenant_id, token, chat_id, reply)
    elif intent == "show_agenda":
        await _show_agenda(tenant_id, pool, token, chat_id, lang, data.get("timeframe", "today"))
    elif intent == "show_thoughts":
        await _show_thoughts(pool, tenant_id, token, chat_id, lang)
    elif intent == "show_todos":
        await _show_by_tag(pool, tenant_id, token, chat_id, lang, "todo")
    elif intent == "show_by_tag":
        await _show_by_tag(pool, tenant_id, token, chat_id, lang, data.get("tag", ""))
    elif intent == "show_youtube":
        await _show_youtube(pool, tenant_id, token, chat_id, lang)
    elif intent == "capture_project":
        await _capture_project(pool, tenant_id, data, token, chat_id, reply, lang)
    elif intent == "show_projects":
        await _show_projects(pool, tenant_id, token, chat_id, lang)
    elif intent == "set_weekly_outcomes":
        await _set_weekly_outcomes(pool, tenant_id, data, token, chat_id, reply, lang)
    elif intent == "update_outcome_status":
        await _update_outcome_status(pool, tenant_id, data, token, chat_id, reply, lang)
    elif intent == "what_now":
        await _what_now(pool, tenant_id, token, chat_id, lang)
    elif intent == "capture_goal":
        await _capture_goal(pool, tenant_id, data, token, chat_id, reply, lang)
    elif intent == "show_goals":
        await _show_goals(pool, tenant_id, token, chat_id, lang)
    elif intent == "mark_done":
        await _mark_done(pool, tenant_id, data, token, chat_id, reply, lang)
    elif intent == "create_event":
        await _handle_calendar_create(tenant_id, pool, data, token, chat_id, lang)
    elif intent == "evening_checkin":
        await _evening_checkin_response(pool, tenant_id, text, token, chat_id, lang)
    elif intent == "invoke_council":
        await _invoke_council(text, token, chat_id, lang, pool=pool, tenant_id=tenant_id)
        await _mark_responded(pool, tenant_id)
    else:
        # greeting / unknown / general conversation → real conversational agent
        # (stateful, context-aware, Sonnet) instead of the Haiku classifier's stub reply
        await chat_reply(pool, tenant_id, chat_id, token, lang, text)
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
                           tags: list | None = None):
    if tags is None:
        tags = []
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
            council_text = r.json()["content"][0]["text"].strip()
            signal = "🔮 Mode conseil." if lang == "fr" else "🔮 Advisory mode."
            await _send(token, chat_id, signal + "\n\n" + council_text)
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
    "2. TRANSITION: one short phrase introducing the wisdom. Match the language of the user's message:\n"
    "   If French: choose from — 'Une pensée pour ce soir :', 'Avant de dormir :', 'Ce soir :', 'Une image :', 'Une petite sagesse :'\n"
    "   If English: choose from — 'A thought for the evening:', 'As night falls:', 'Before you rest:', 'Tonight\\'s image:', 'A small wisdom:'\n"
    "3. WISDOM: one metaphor, image, or observation. Inspired by nature, seasons, rivers, stars, gardens, "
    "craftsmanship, poetry, myths, or contemplative traditions. Should subtly resonate with the user's realisation.\n\n"
    "Constraints:\n"
    "- Detect the language of the user's message and use ONLY that language throughout — never mix languages\n"
    "- Do not give advice\n"
    "- Do not ask questions\n"
    "- Do not analyze\n"
    "- Do not sound like a coach, therapist, productivity app, or fortune cookie\n"
    "- Do not quote famous people\n"
    "- Avoid clichés\n"
    "- Leave space for contemplation\n"
    "- Sound simple, timeless, and quietly poetic\n\n"
    "Example (French):\n"
    "Vous avez trouvé de la gratitude dans l'apprentissage et le repos aujourd'hui.\n"
    "Une pensée pour ce soir :\n"
    "Une graine ne pousse pas plus vite parce qu'on l'a plantée dans sept jardins.\n\n"
    "Example (English):\n"
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


async def _show_thoughts(pool, tenant_id, token: str, chat_id: str, lang: str = "fr"):
    """Show plain notes only — excludes all tagged content."""
    _EXCLUDED = ["todo", "evening_checkin", "youtube", "watchlist", "portfolio",
                 "toread", "ideas", "microfaust"]
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            rows = await conn.fetch(
                "SELECT content FROM thoughts WHERE tenant_id=$1 "
                "AND (tags IS NULL OR tags = '{}' OR NOT tags && $2) "
                "ORDER BY created_at DESC LIMIT 10",
                tenant_id, _EXCLUDED,
            )
    except Exception as e:
        logger.error("show_thoughts failed: %s", e)
        rows = []
    if not rows:
        await _send(token, chat_id, _t(lang, "Aucune note sauvegardée.", "No saved notes yet."))
        return
    lines = [f"- {r['content']}" for r in rows]
    await _send(token, chat_id, _t(lang, "Vos notes :", "Your notes:") + "\n\n" + "\n".join(lines))


async def _show_by_tag(pool, tenant_id, token: str, chat_id: str, lang: str, tag: str):
    """Show thoughts filtered by a specific tag."""
    if not tag:
        await _send(token, chat_id, _t(lang, "Quel tag voulez-vous voir ?", "Which tag do you want to see?"))
        return
    tag = tag.lower().lstrip("#")
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            rows = await conn.fetch(
                "SELECT content FROM thoughts WHERE tenant_id=$1 AND $2=ANY(coalesce(tags,'{}')) "
                "ORDER BY created_at DESC LIMIT 20",
                tenant_id, tag,
            )
    except Exception as e:
        logger.error("show_by_tag failed: %s", e)
        rows = []
    if not rows:
        await _send(token, chat_id, _t(lang,
            f"Rien trouvé dans #{tag}.", f"Nothing found in #{tag}."))
        return
    lines = [f"- {r['content']}" for r in rows]
    header = _t(lang, f"#{tag} :", f"#{tag}:")
    await _send(token, chat_id, header + "\n\n" + "\n".join(lines))


async def _show_youtube(pool, tenant_id, token: str, chat_id: str, lang: str):
    """Show saved YouTube watch-later list."""
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            rows = await conn.fetch(
                "SELECT content FROM thoughts WHERE tenant_id=$1 AND 'youtube'=ANY(coalesce(tags,'{}')) "
                "ORDER BY created_at DESC LIMIT 20",
                tenant_id,
            )
    except Exception as e:
        logger.error("show_youtube failed: %s", e)
        rows = []
    if not rows:
        await _send(token, chat_id, _t(lang,
            "Aucune vidéo sauvegardée.", "No saved videos yet."))
        return
    lines = [f"- {r['content']}" for r in rows]
    header = _t(lang, "Vidéos à regarder :", "Videos to watch:")
    await _send(token, chat_id, header + "\n\n" + "\n".join(lines))


async def _capture_project(pool, tenant_id, data: dict, token: str, chat_id: str, reply: str, lang: str):
    name = data.get("name", "").strip()
    if not name:
        await _send(token, chat_id, _t(lang,
            "Quel est le nom du projet ?", "What is the project name?"))
        return
    outcome = data.get("outcome", "") or ""
    horizon = data.get("horizon", "") or ""
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            await conn.execute(
                """INSERT INTO projects (tenant_id, name, outcome, horizon)
                   VALUES ($1,$2,$3,$4)
                   ON CONFLICT DO NOTHING""",
                tenant_id, name, outcome, horizon,
            )
    except Exception as e:
        logger.error("capture_project failed: %s", e)
    await _send(token, chat_id, reply)
    await _mark_responded(pool, tenant_id)


async def _show_projects(pool, tenant_id, token: str, chat_id: str, lang: str):
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            rows = await conn.fetch(
                "SELECT name, outcome, horizon, status FROM projects "
                "WHERE tenant_id=$1 ORDER BY status, created_at",
                tenant_id,
            )
    except Exception as e:
        logger.error("show_projects failed: %s", e)
        rows = []
    if not rows:
        await _send(token, chat_id, _t(lang,
            "Aucun projet dans ton portfolio. Dis 'ajouter projet [nom]' pour commencer.",
            "No projects in your portfolio. Say 'add project [name]' to start."))
        return
    status_icon = {"active": "🟢", "paused": "⏸", "done": "✅"}
    lines = []
    for p in rows:
        icon = status_icon.get(p["status"], "•")
        line = f"{icon} {p['name']}"
        if p["outcome"]:
            line += f"\n   → {p['outcome']}"
        if p["horizon"]:
            line += f"  [{p['horizon']}]"
        lines.append(line)
    header = _t(lang, "Ton portfolio :", "Your portfolio:")
    await _send(token, chat_id, header + "\n\n" + "\n\n".join(lines))
    await _mark_responded(pool, tenant_id)


async def _set_weekly_outcomes(pool, tenant_id, data: dict, token: str, chat_id: str, reply: str, lang: str):
    from datetime import date, timedelta
    def week_start(d): return d - timedelta(days=d.weekday())
    ws = week_start(date.today())
    outcomes = data.get("outcomes", [])
    if not outcomes:
        await _send(token, chat_id, _t(lang,
            "Je n'ai pas compris les 3 objectifs. Essaie :\n1. [objectif] — [projet]\n2. ...\n3. ...",
            "I didn't catch the 3 outcomes. Try:\n1. [outcome] — [project]\n2. ...\n3. ..."))
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            for o in outcomes:
                await conn.execute(
                    """INSERT INTO weekly_outcomes (tenant_id, week_start, rank, description, project_name)
                       VALUES ($1,$2,$3,$4,$5)
                       ON CONFLICT (tenant_id, week_start, rank)
                       DO UPDATE SET description=$4, project_name=$5, status='pending'""",
                    tenant_id, ws, int(o.get("rank", 1)),
                    str(o.get("description", "")), str(o.get("project_name", "") or ""),
                )
    except Exception as e:
        logger.error("set_weekly_outcomes failed: %s", e)
    await _send(token, chat_id, reply)
    await _mark_responded(pool, tenant_id)


async def _update_outcome_status(pool, tenant_id, data: dict, token: str, chat_id: str, reply: str, lang: str):
    from datetime import date, timedelta
    def week_start(d): return d - timedelta(days=d.weekday())
    ws = week_start(date.today())
    hint = data.get("description_hint", "").lower()
    status = data.get("status", "done")
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            if hint:
                await conn.execute(
                    """UPDATE weekly_outcomes SET status=$1
                       WHERE tenant_id=$2 AND week_start=$3
                         AND description ILIKE $4""",
                    status, tenant_id, ws, f"%{hint}%",
                )
            else:
                # Update rank 1 by default
                await conn.execute(
                    "UPDATE weekly_outcomes SET status=$1 WHERE tenant_id=$2 AND week_start=$3 AND rank=1",
                    status, tenant_id, ws,
                )
    except Exception as e:
        logger.error("update_outcome_status failed: %s", e)
    await _send(token, chat_id, reply)
    await _mark_responded(pool, tenant_id)


async def _what_now(pool, tenant_id, token: str, chat_id: str, lang: str):
    from datetime import date, timedelta, datetime, timezone
    import uuid as _uuid_mod

    def week_start(d): return d - timedelta(days=d.weekday())
    ws = week_start(date.today())

    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            # Get weekly outcomes ordered by rank, skip done ones
            outcomes = await conn.fetch(
                "SELECT rank, description, project_name, status FROM weekly_outcomes "
                "WHERE tenant_id=$1 AND week_start=$2 AND status != 'done' ORDER BY rank",
                tenant_id, ws,
            )
            # Get todos linked to top outcome's project, or all todos if no outcomes
            top_project = outcomes[0]["project_name"] if outcomes else None
            if top_project:
                todos = await conn.fetch(
                    "SELECT content FROM thoughts WHERE tenant_id=$1 AND 'todo'=ANY(coalesce(tags,'{}')) "
                    "AND (project_name ILIKE $2 OR content ILIKE $2) ORDER BY created_at ASC LIMIT 5",
                    tenant_id, f"%{top_project}%",
                )
                if not todos:
                    todos = await conn.fetch(
                        "SELECT content FROM thoughts WHERE tenant_id=$1 AND 'todo'=ANY(coalesce(tags,'{}')) "
                        "ORDER BY created_at ASC LIMIT 5",
                        tenant_id,
                    )
            else:
                todos = await conn.fetch(
                    "SELECT content FROM thoughts WHERE tenant_id=$1 AND 'todo'=ANY(coalesce(tags,'{}')) "
                    "ORDER BY created_at ASC LIMIT 5",
                    tenant_id,
                )
    except Exception as e:
        logger.error("what_now fetch failed: %s", e)
        await _send(token, chat_id, _t(lang, "Impossible de récupérer tes données.", "Could not fetch your data."))
        return

    # Get today's calendar to find time until next meeting
    try:
        events = await list_events_range(_uuid_mod.UUID(str(tenant_id)), pool, "today")
    except Exception:
        events = []

    now_utc = datetime.now(timezone.utc)
    next_event = None
    minutes_free = None
    if events:
        import pytz as _pytz
        _PARIS = _pytz.timezone("Europe/Paris")
        for ev in events:
            ev_start = ev.get("start", {}).get("dateTime")
            if ev_start:
                try:
                    # Parse ISO 8601 with stdlib — no dateutil needed
                    ev_start_clean = ev_start.replace("Z", "+00:00")
                    ev_dt = datetime.fromisoformat(ev_start_clean)
                    if ev_dt.tzinfo is None:
                        ev_dt = _PARIS.localize(ev_dt)
                    diff = (ev_dt - now_utc).total_seconds() / 60
                    if diff > 0 and (minutes_free is None or diff < minutes_free):
                        minutes_free = int(diff)
                        next_event = ev.get("summary", "meeting")
                except Exception:
                    pass

    # Build the response
    if not outcomes and not todos:
        msg = _t(lang,
            "Pas d'objectifs ni de tâches cette semaine. Dis 'définir objectifs' pour commencer.",
            "No outcomes or todos this week. Say 'set weekly outcomes' to get started.")
        await _send(token, chat_id, msg)
        await _mark_responded(pool, tenant_id)
        return

    lines = []

    if minutes_free is not None:
        if lang == "en":
            lines.append(f"You have {minutes_free} min before {next_event}.\n")
        else:
            lines.append(f"Tu as {minutes_free} min avant {next_event}.\n")

    if outcomes:
        top = outcomes[0]
        proj = f" [{top['project_name']}]" if top["project_name"] else ""
        if lang == "en":
            lines.append(f"Focus on weekly outcome #{top['rank']}:{proj}\n{top['description']}")
        else:
            lines.append(f"Concentre-toi sur l'objectif #{top['rank']} :{proj}\n{top['description']}")

    if todos:
        task = todos[0]["content"]
        if lang == "en":
            lines.append(f"\nNext task: {task}")
        else:
            lines.append(f"\nProchaine tâche : {task}")

        # Show next 2 outcomes as backup context
        if len(outcomes) > 1 and (minutes_free is None or minutes_free > 30):
            next_outcomes = outcomes[1:3]
            if lang == "en":
                lines.append("\nOnce done, move to:")
            else:
                lines.append("\nEnsuite :")
            for o in next_outcomes:
                lines.append(f"  • {o['description']}")

    await _send(token, chat_id, "\n".join(lines))
    await _mark_responded(pool, tenant_id)


async def _capture_goal(pool, tenant_id, data: dict, token: str, chat_id: str, reply: str, lang: str):
    description = (data.get("description") or "").strip()
    if not description:
        await _send(token, chat_id, _t(lang,
            "De quel objectif s'agit-il ?", "What is the goal?"))
        return
    deadline = data.get("deadline") or None
    project_name = (data.get("project_name") or "").strip() or None
    current_state = (data.get("current_state") or "").strip() or None
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            await conn.execute(
                """INSERT INTO macro_goals (tenant_id, description, project_name, deadline, current_state)
                   VALUES ($1,$2,$3,$4,$5)""",
                tenant_id, description, project_name, deadline, current_state,
            )
    except Exception as e:
        logger.error("capture_goal failed: %s", e)
    await _send(token, chat_id, reply)
    await _mark_responded(pool, tenant_id)


async def _show_goals(pool, tenant_id, token: str, chat_id: str, lang: str):
    from datetime import date
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            rows = await conn.fetch(
                "SELECT description, project_name, deadline, current_state, status "
                "FROM macro_goals WHERE tenant_id=$1 AND status='active' ORDER BY deadline NULLS LAST",
                tenant_id,
            )
    except Exception as e:
        logger.error("show_goals failed: %s", e)
        rows = []
    if not rows:
        await _send(token, chat_id, _t(lang,
            "Aucun objectif macro. Dis 'objectif [description] pour [date]' pour en définir un.",
            "No macro goals. Say 'goal [description] by [date]' to set one."))
        return
    today = date.today()
    lines = []
    for g in rows:
        line = f"• {g['description']}"
        if g["project_name"]:
            line += f" [{g['project_name']}]"
        if g["deadline"]:
            months_left = (g["deadline"].year - today.year) * 12 + (g["deadline"].month - today.month)
            line += f" — {months_left}m" if lang == "en" else f" — {months_left} mois"
        if g["current_state"]:
            line += f"\n  Maintenant : {g['current_state']}" if lang == "fr" else f"\n  Now: {g['current_state']}"
        lines.append(line)
    header = _t(lang, "Objectifs macro :", "Macro goals:")
    await _send(token, chat_id, header + "\n\n" + "\n\n".join(lines))
    await _mark_responded(pool, tenant_id)


async def _mark_done(pool, tenant_id, data: dict, token: str, chat_id: str, reply: str, lang: str):
    hint = (data.get("hint") or "").strip()
    project = (data.get("project") or "").strip()
    if not hint:
        await _send(token, chat_id, _t(lang,
            "Quelle tâche marquer comme faite ?", "Which task to mark as done?"))
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            if project:
                result = await conn.execute(
                    """UPDATE thoughts SET done=true
                       WHERE tenant_id=$1 AND done=false
                         AND content ILIKE $2 AND (project_name ILIKE $3 OR $3=ANY(coalesce(tags,'{}')))""",
                    tenant_id, f"%{hint}%", project,
                )
            else:
                result = await conn.execute(
                    "UPDATE thoughts SET done=true WHERE tenant_id=$1 AND done=false AND content ILIKE $2",
                    tenant_id, f"%{hint}%",
                )
    except Exception as e:
        logger.error("mark_done failed: %s", e)
    await _send(token, chat_id, reply)
    await _mark_responded(pool, tenant_id)


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
