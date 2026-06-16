"""
Google Calendar client — direct OAuth2, no third-party SDK.
Tokens stored per-tenant in the tenants table.
"""
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_TOKEN_URL     = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
GOOGLE_SCOPES        = " ".join([
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
])


def build_oauth_url(tenant_id: str, redirect_uri: str) -> str:
    """Build the Google OAuth2 authorization URL."""
    import urllib.parse
    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         GOOGLE_SCOPES,
        "access_type":   "offline",
        "prompt":        "consent",   # always returns refresh_token
        "state":         str(tenant_id),
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


async def exchange_code(code: str, redirect_uri: str) -> dict:
    """Exchange authorization code for access + refresh tokens."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  redirect_uri,
            "grant_type":    "authorization_code",
        })
        r.raise_for_status()
        return r.json()


async def _get_access_token(pool: asyncpg.Pool, tenant_id: uuid.UUID) -> str | None:
    """Return a valid access token, refreshing if needed."""
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
        row = await conn.fetchrow(
            "SELECT google_access_token, google_refresh_token, google_token_expiry FROM tenants WHERE id=$1",
            tenant_id,
        )
    if not row or not row["google_refresh_token"]:
        return None

    # Still valid with 5-min buffer
    if row["google_token_expiry"] and row["google_token_expiry"] > datetime.now(timezone.utc) + timedelta(minutes=5):
        return row["google_access_token"]

    # Refresh
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(GOOGLE_TOKEN_URL, data={
                "refresh_token": row["google_refresh_token"],
                "client_id":     GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "grant_type":    "refresh_token",
            })
            r.raise_for_status()
            data = r.json()

        new_token  = data["access_token"]
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600))
        async with pool.acquire() as conn:
            await conn.execute("SELECT set_config('app.tenant_id',$1,true)", str(tenant_id))
            await conn.execute(
                "UPDATE tenants SET google_access_token=$1, google_token_expiry=$2 WHERE id=$3",
                new_token, expires_at, tenant_id,
            )
        return new_token
    except Exception as e:
        logger.error("Google token refresh failed tenant=%s: %s", tenant_id, e)
        return None


async def list_events_range(tenant_id: uuid.UUID, pool: asyncpg.Pool,
                            timeframe: str = "today") -> list[dict]:
    """Fetch Google Calendar events for a tenant. timeframe: today | tomorrow | week"""
    token = await _get_access_token(pool, tenant_id)
    if not token:
        return []

    from zoneinfo import ZoneInfo
    paris = ZoneInfo("Europe/Paris")
    now = datetime.now(paris)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if timeframe == "tomorrow":
        from datetime import timedelta
        start = today + timedelta(days=1)
        end   = start.replace(hour=23, minute=59, second=59)
    elif timeframe == "week":
        from datetime import timedelta
        start = today
        end   = today + timedelta(days=7)
    else:  # today
        start = today
        end   = today.replace(hour=23, minute=59, second=59)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{GOOGLE_CALENDAR_BASE}/calendars/primary/events",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "timeMin":      start.isoformat(),
                    "timeMax":      end.isoformat(),
                    "singleEvents": "true",
                    "orderBy":      "startTime",
                    "maxResults":   "20",
                },
            )
            r.raise_for_status()
            return r.json().get("items", [])
    except Exception as e:
        logger.error("Google Calendar list_events failed tenant=%s: %s", tenant_id, e)
        return []


async def list_today_events(tenant_id: uuid.UUID, pool: asyncpg.Pool) -> list[dict]:
    return await list_events_range(tenant_id, pool, "today")


async def create_event(tenant_id: uuid.UUID, pool: asyncpg.Pool,
                       summary: str, start: str, end: str,
                       description: str = "") -> dict | None:
    """Create a Google Calendar event for a tenant."""
    token = await _get_access_token(pool, tenant_id)
    if not token:
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{GOOGLE_CALENDAR_BASE}/calendars/primary/events",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "summary":     summary,
                    "description": description,
                    "start": {"dateTime": start, "timeZone": "Europe/Paris"},
                    "end":   {"dateTime": end,   "timeZone": "Europe/Paris"},
                },
            )
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.error("Google Calendar create_event failed tenant=%s: %s", tenant_id, e)
        return None


def format_events_for_telegram(events: list[dict]) -> str:
    """Format event list as plain text for Telegram."""
    if not events:
        return "Aucune reunion aujourd'hui."
    lines = []
    for e in events:
        start    = e.get("start", {})
        time_str = start.get("dateTime", start.get("date", ""))
        if "T" in time_str:
            try:
                dt       = datetime.fromisoformat(time_str)
                time_str = dt.strftime("%H:%M")
            except Exception:
                pass
        lines.append(f"  {time_str} - {e.get('summary', 'Sans titre')}")
    return "\n".join(lines)
