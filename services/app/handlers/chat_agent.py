"""
Conversational agent path — the "smart" reply loop, now with tools.

Slice 1 gave general chat its own path: conversation history + real persona +
full user context + Sonnet (instead of the Haiku intent-classifier stub).

Slice 2 (this file) adds a TOOL LOOP so the agent can *act*, not just talk:
  - add_task       → add a todo to the user's list
  - search_memory  → look up something the user saved earlier
  - get_agenda     → read the user's calendar (today / tomorrow / week)

The agent runs a multi-step loop: model → tool_use → run tool → feed result
back → model → ... → final reply. Command intents (capture/show/agenda/habit/
council/forget) still route through telegram_message.py unchanged; this is only
the general-conversation path.
"""
import logging
import os
import uuid as _uuid

import asyncpg
import httpx

from calendar_client import list_events_range, format_events_for_telegram

logger = logging.getLogger(__name__)

# Reasoning-path model (supports tools). Haiku stays the router in
# telegram_message.py; this is the "converse + act well" model.
CHAT_MODEL = "claude-sonnet-4-6"
HISTORY_TURNS = 10          # prior chat turns replayed for memory
NOTES_LIMIT = 40            # recent thoughts pulled into context
MAX_TOOL_TURNS = 5          # safety cap on the agent loop
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── tool schemas exposed to the model ──────────────────────────────────────────

TOOLS = [
    {
        "name": "add_task",
        "description": "Add a task or todo to the user's list. Use whenever the user "
                       "asks to remember to do something or add something to their todos.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "The task text only, stripped of prefixes like "
                                        "'add todo' or 'remind me'."}
            },
            "required": ["task"],
        },
    },
    {
        "name": "search_memory",
        "description": "Search the user's saved notes and todos for a keyword or phrase. "
                       "Use to recall something the user saved earlier.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword or phrase to search for."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_agenda",
        "description": "Read the user's calendar events for a timeframe.",
        "input_schema": {
            "type": "object",
            "properties": {
                "timeframe": {"type": "string", "enum": ["today", "tomorrow", "week"]}
            },
            "required": ["timeframe"],
        },
    },
]


async def chat_reply(pool: asyncpg.Pool, tenant_id, chat_id: str, token: str,
                     lang: str, text: str) -> None:
    """Generate a context-aware, stateful, tool-capable reply and persist it."""
    if not ANTHROPIC_API_KEY:
        await _send(token, chat_id, _t(lang, "C'est noté.", "Got it."))
        return

    history = await _load_history(pool, tenant_id)
    system = await _build_system(pool, tenant_id, lang)
    messages = history + [{"role": "user", "content": text}]

    reply = await _agent_loop(pool, tenant_id, chat_id, token, lang, system, messages)

    await _send(token, chat_id, reply)
    # Persist as plain text turns so history stays clean (no dangling tool blocks)
    await _save_turn(pool, tenant_id, "user", text)
    await _save_turn(pool, tenant_id, "assistant", reply)


# ── agent loop ─────────────────────────────────────────────────────────────────

async def _agent_loop(pool, tenant_id, chat_id: str, token: str, lang: str,
                      system: str, messages: list) -> str:
    convo = list(messages)
    for _ in range(MAX_TOOL_TURNS):
        data = await _call_api(system, convo)
        if not data:
            return _t(lang, "Désolé, je n'ai pas pu répondre là. Réessaie ?",
                            "Sorry, I couldn't answer just now. Try again?")
        blocks = data.get("content", [])
        if data.get("stop_reason") == "tool_use":
            await _typing(token, chat_id)  # keep indicator alive during tool work
            convo.append({"role": "assistant", "content": blocks})
            results = []
            for b in blocks:
                if b.get("type") == "tool_use":
                    out = await _run_tool(pool, tenant_id, b.get("name", ""), b.get("input", {}))
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": b["id"],
                        "content": out,
                    })
            convo.append({"role": "user", "content": results})
            continue
        # normal completion → return the text
        return _extract_text(blocks) or _t(lang, "C'est noté.", "Got it.")
    return _t(lang, "Je me suis un peu perdu — peux-tu reformuler ?",
                    "I got a bit tangled — can you rephrase?")


async def _call_api(system: str, messages: list) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CHAT_MODEL,
                    "max_tokens": 1024,
                    "system": system,
                    "tools": TOOLS,
                    "messages": messages,
                },
            )
        if r.status_code != 200:
            logger.error("chat_agent API error %s: %s", r.status_code, r.text[:300])
            return None
        return r.json()
    except Exception as e:
        logger.error("chat_agent API call failed: %s", e)
        return None


def _extract_text(blocks: list) -> str:
    return "\n".join(b["text"] for b in blocks if b.get("type") == "text").strip()


# ── tools ──────────────────────────────────────────────────────────────────────

async def _run_tool(pool, tenant_id, name: str, args: dict) -> str:
    try:
        if name == "add_task":
            return await _tool_add_task(pool, tenant_id, args.get("task", ""))
        if name == "search_memory":
            return await _tool_search_memory(pool, tenant_id, args.get("query", ""))
        if name == "get_agenda":
            return await _tool_get_agenda(pool, tenant_id, args.get("timeframe", "today"))
        return f"Unknown tool: {name}"
    except Exception as e:
        logger.error("tool %s failed: %s", name, e)
        return f"Tool {name} failed."


async def _tool_add_task(pool, tenant_id, task: str) -> str:
    task = (task or "").strip()
    if not task:
        return "No task text was provided."
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        await conn.execute(
            "INSERT INTO thoughts (tenant_id, content, tags) VALUES ($1,$2,$3)",
            tenant_id, task, ["todo"],
        )
    return f"Task added: {task}"


async def _tool_search_memory(pool, tenant_id, query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "No search query was provided."
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        rows = await conn.fetch(
            """SELECT content FROM thoughts
               WHERE tenant_id=$1 AND content ILIKE $2
               ORDER BY created_at DESC LIMIT 10""",
            tenant_id, f"%{query}%",
        )
    if not rows:
        return f"No saved items match '{query}'."
    return "\n".join(f"- {r['content']}" for r in rows)


async def _tool_get_agenda(pool, tenant_id, timeframe: str) -> str:
    if timeframe not in ("today", "tomorrow", "week"):
        timeframe = "today"
    try:
        events = await list_events_range(_uuid.UUID(str(tenant_id)), pool, timeframe)
        return format_events_for_telegram(events) or f"No events for {timeframe}."
    except Exception as e:
        logger.error("get_agenda tool failed: %s", e)
        return "Couldn't fetch the calendar right now."


# ── context assembly ───────────────────────────────────────────────────────────

async def _load_history(pool: asyncpg.Pool, tenant_id) -> list[dict]:
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            rows = await conn.fetch(
                """SELECT role, content FROM conversations
                   WHERE tenant_id=$1 AND skill_slug='chat'
                   ORDER BY created_at DESC LIMIT $2""",
                tenant_id, HISTORY_TURNS,
            )
    except Exception as e:
        logger.error("chat_agent history load failed: %s", e)
        return []
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def _build_system(pool: asyncpg.Pool, tenant_id, lang: str) -> str:
    values_lines, todo_lines, note_lines = [], [], []
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            vrows = await conn.fetch(
                """SELECT name, description FROM user_values
                   WHERE tenant_id=$1 AND active=true
                   ORDER BY rank NULLS LAST LIMIT 20""",
                tenant_id,
            )
            trows = await conn.fetch(
                """SELECT content, tags FROM thoughts
                   WHERE tenant_id=$1 ORDER BY created_at DESC LIMIT $2""",
                tenant_id, NOTES_LIMIT,
            )
        values_lines = [f"- {v['name']}: {v['description']}" for v in vrows if v["name"]]
        for t in trows:
            tags = t["tags"] or []
            (todo_lines if "todo" in tags else note_lines).append(f"- {t['content']}")
    except Exception as e:
        logger.error("chat_agent context load failed: %s", e)

    lang_label = "French" if lang == "fr" else "English"

    parts = [
        "You are the user's persistent personal assistant on Telegram. "
        "You are warm, direct, and genuinely useful — talk like a sharp friend, "
        "not a customer-service script. No filler, no \"I'd be happy to help\". "
        "You have real, persistent memory of this person; use the context below to "
        "be specific and personal rather than generic.",
        "",
        "Style rules (Telegram):",
        f"- Reply in {lang_label}, matching the user's language. Never mix languages.",
        "- No markdown, no asterisks (they render literally on Telegram).",
        "- Short by default — they're on a phone. Expand only when the question earns it.",
        "- If you don't know something, say so in one line. Never invent facts.",
        "",
        "You can take actions with tools: add_task (add a todo), search_memory "
        "(look up something the user saved), get_agenda (check their calendar). "
        "When the user asks you to do one of these, call the tool and then confirm "
        "naturally in one line. Don't announce that you're using a tool.",
    ]
    if values_lines:
        parts += ["", "The user's declared core values (honour these):", *values_lines]
    if todo_lines:
        parts += ["", "The user's current open todos:", *todo_lines]
    if note_lines:
        parts += ["", "Recent notes the user has saved:", *note_lines]
    return "\n".join(parts)


async def _save_turn(pool: asyncpg.Pool, tenant_id, role: str, content: str) -> None:
    try:
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            await conn.execute(
                """INSERT INTO conversations (id, tenant_id, skill_slug, role, content, tokens)
                   VALUES (gen_random_uuid(), $1, 'chat', $2, $3, 0)""",
                tenant_id, role, content,
            )
    except Exception as e:
        logger.error("chat_agent save_turn failed: %s", e)


# ── telegram helpers (local, to avoid a circular import) ───────────────────────

def _t(lang: str, fr: str, en: str) -> str:
    return en if lang == "en" else fr


async def _typing(token: str, chat_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(
                f"https://api.telegram.org/bot{token}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
            )
    except Exception:
        pass


async def _send(token: str, chat_id: str, text: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat_id, "text": text})
    except Exception as e:
        logger.error("chat_agent telegram send failed chat=%s: %s", chat_id, e)
