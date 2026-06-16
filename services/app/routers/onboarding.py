"""
Onboarding — endpoints:
  POST /auth/sync           Called after Supabase login — creates tenant row if needed
  POST /onboarding/bot      Registers Telegram bot token + webhook
  POST /onboarding/calendar Starts Composio Google Calendar OAuth flow
  GET  /onboarding/calendar/callback  Composio redirects here after user auth
"""
import logging
import os
import uuid

import asyncpg
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from calendar_client import build_oauth_url, exchange_code

logger = logging.getLogger(__name__)
router = APIRouter()

BASE_URL = os.environ.get("BASE_URL", "")


# ── Auth sync ────────────────────────────────────────────────────────────────

@router.post("/auth/sync")
async def auth_sync(request: Request):
    """
    Called by the frontend immediately after Supabase login.
    Creates a tenants row if this is a new user.
    Returns tenant metadata needed by the frontend.
    """
    supabase_user_id = request.state.tenant_id
    if not supabase_user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Extract email from Supabase JWT claims
    auth = request.headers.get("Authorization", "")[7:]
    email = _extract_email_from_jwt(auth)

    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, telegram_chat_id, composio_entity_id FROM tenants WHERE id = $1",
            uuid.UUID(supabase_user_id),
        )
        if not existing:
            await conn.execute(
                "INSERT INTO tenants (id, email) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                uuid.UUID(supabase_user_id), email,
            )
            telegram_chat_id   = None
            composio_entity_id = None
        else:
            telegram_chat_id   = existing["telegram_chat_id"]
            composio_entity_id = existing["composio_entity_id"]

        council_count = await conn.fetchval(
            "SELECT COUNT(*) FROM skills WHERE tenant_id=$1 AND active=true",
            uuid.UUID(supabase_user_id),
        )

    return {
        "tenant_id":           supabase_user_id,
        "onboarding_complete": telegram_chat_id is not None,
        "calendar_connected":  composio_entity_id is not None,
        "council_configured":  (council_count or 0) > 0,
    }


# ── Bot registration ─────────────────────────────────────────────────────────

class BotRequest(BaseModel):
    telegram_bot_token: str
    timezone: str = "Europe/Paris"


@router.post("/onboarding/bot")
async def register_bot(body: BotRequest, request: Request):
    tenant_id = request.state.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    bot_info = await _validate_bot_token(body.telegram_bot_token)
    if not bot_info:
        raise HTTPException(status_code=400, detail="Invalid Telegram bot token")

    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        # P-04: token stored, never logged
        await conn.execute(
            """UPDATE tenants
               SET telegram_bot_token=$2, timezone=$3
               WHERE id=$1""",
            uuid.UUID(tenant_id), body.telegram_bot_token, body.timezone,
        )

    webhook_url = f"{BASE_URL}/webhook/{tenant_id}"
    await _register_webhook(body.telegram_bot_token, webhook_url)

    return {
        "webhook_url": webhook_url,
        "bot_username": bot_info.get("username"),
        "next": "Send /start to your bot on Telegram",
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_email_from_jwt(token: str) -> str | None:
    import os
    from jose import jwt, JWTError
    try:
        payload = jwt.decode(
            token,
            os.environ.get("SUPABASE_JWT_SECRET", ""),
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        return payload.get("email")
    except JWTError:
        return None


async def _validate_bot_token(token: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            if r.status_code == 200:
                return r.json().get("result", {})
    except Exception:
        pass
    return None


async def _register_webhook(token: str, url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/setWebhook",
                json={"url": url, "drop_pending_updates": True},
            )
            return r.status_code == 200 and r.json().get("ok")
    except Exception:
        return False


# ── Calendar onboarding — Google OAuth direct ────────────────────────────────

FRONTEND_URL   = os.environ.get("FRONTEND_URL", "https://microfaust.vercel.app")
CALENDAR_CB    = f"{BASE_URL}/onboarding/calendar/callback"


@router.post("/onboarding/calendar")
async def start_calendar_oauth(request: Request):
    """Return Google OAuth URL. Frontend opens it in a new tab."""
    tenant_id = request.state.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    from calendar_client import GOOGLE_CLIENT_ID
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID not configured")
    oauth_url = build_oauth_url(tenant_id=tenant_id, redirect_uri=CALENDAR_CB)
    return {"oauth_url": oauth_url}


@router.get("/onboarding/calendar/callback")
async def calendar_oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Google redirects here after user authorises Calendar access."""
    if error or not code or not state:
        return RedirectResponse(url=f"{FRONTEND_URL}/app.html?calendar=error")

    try:
        tokens = await exchange_code(code=code, redirect_uri=CALENDAR_CB)
    except Exception as e:
        logger.error("Google token exchange failed: %s", e)
        return RedirectResponse(url=f"{FRONTEND_URL}/app.html?calendar=error")

    refresh_token = tokens.get("refresh_token")
    access_token  = tokens.get("access_token")
    expires_in    = tokens.get("expires_in", 3600)

    if not refresh_token:
        logger.error("Google did not return refresh_token — ensure prompt=consent")
        return RedirectResponse(url=f"{FRONTEND_URL}/app.html?calendar=error")

    from datetime import datetime, timedelta, timezone
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE tenants
               SET google_refresh_token=$1,
                   google_access_token=$2,
                   google_token_expiry=$3,
                   composio_entity_id=$4
               WHERE id=$5""",
            refresh_token, access_token, expires_at,
            state,          # reuse composio_entity_id field so calendar_connected stays true
            uuid.UUID(state),
        )
    return RedirectResponse(url=f"{FRONTEND_URL}/app.html?calendar=connected")
