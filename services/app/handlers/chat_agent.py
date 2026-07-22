"""
Conversational agent path — the "smart" reply loop.

Problem this fixes: normal chat previously reused the Haiku *intent classifier's*
reply field — stateless, no user context, tiny model. This module gives general
conversation its own path:

  #1 conversation history   → last N turns replayed from `conversations`
  #2 real persona           → a proper system prompt, not a 200-char stub
  #3 full context           → the user's actual notes / todos / values injected
      (no RAG — the corpus is small enough to load wholesale)
  + model upgrade           → Sonnet instead of Haiku for the reasoning path

Command intents (capture/show/agenda/habit/council) still route through
telegram_message.py unchanged. Only greeting / unknown / general chat land here.
"""
import logging
import os

import asyncpg
import httpx

logger = logging.getLogger(__name__)

# Reasoning-path model. Haiku stays the router in telegram_message.py; this is the
# "converse well" model. Bump to a larger model here if you want more depth (cost ↑).
CHAT_MODEL = "claude-sonnet-4-6"
HISTORY_TURNS = 10          # how many prior chat turns to replay
NOTES_LIMIT = 40            # recent thoughts pulled into context
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


async def chat_reply(pool: asyncpg.Pool, tenant_id, chat_id: str, token: str,
                     lang: str, text: str) -> None:
    """Generate a context-aware, stateful reply and persist the exchange."""
    if not ANTHROPIC_API_KEY:
        await _send(token, chat_id, _t(lang, "C'est noté.", "Got it."))
        return

    history = await _load_history(pool, tenant_id)
    system = await _build_system(pool, tenant_id, lang)

    messages = history + [{"role": "user", "content": text}]

    reply = None
    try:
        await _typing(token, chat_id)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CHAT_MODEL,
                    "max_tokens": 700,
                    "system": system,
                    "messages": messages,
                },
            )
        if r.status_code == 200:
            reply = r.json()["content"][0]["text"].strip()
        else:
            logger.error("chat_agent Anthropic error %s", r.status_code)
    except Exception as e:
        logger.error("chat_agent call failed: %s", e)

    if not reply:
        reply = _t(lang, "Désolé, je n'ai pas pu répondre là. Réessaie ?",
                         "Sorry, I couldn't answer just now. Try again?")

    await _send(token, chat_id, reply)
    # Persist both turns so the next message has memory of this one
    await _save_turn(pool, tenant_id, "user", text)
    await _save_turn(pool, tenant_id, "assistant", reply)


# ── context assembly ──────────────────────────────────────────────────────────

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
    # rows are newest-first; conversation needs oldest-first
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
            if "todo" in tags:
                todo_lines.append(f"- {t['content']}")
            else:
                note_lines.append(f"- {t['content']}")
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


# ── telegram helpers (kept local to avoid a circular import) ───────────────────

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
