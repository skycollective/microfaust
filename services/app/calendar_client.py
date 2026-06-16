"""
Composio Google Calendar client — multi-tenant.
Each tenant has their own entity_id (Composio's per-user OAuth handle).
"""
import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY", "")
COMPOSIO_BASE    = "https://backend.composio.dev/api/v1"


async def get_oauth_url(entity_id: str, redirect_url: str) -> str:
    """Return Composio OAuth initiation URL for Google Calendar."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{COMPOSIO_BASE}/connectedAccounts",
            headers={
                "x-api-key": COMPOSIO_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "appName": "googlecalendar",
                "entityId": entity_id,
                "redirectUri": redirect_url,
                "authMode": "OAUTH2",
                "integrationId": None,
            },
        )
        logger.error("Composio OAuth response: %s %s", r.status_code, r.text)
        r.raise_for_status()
        data = r.json()
        return data.get("redirectUrl") or data.get("connectionUrl") or data["url"]


async def list_today_events(entity_id: str) -> list[dict]:
    """Fetch today's Google Calendar events for a tenant."""
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    day_end   = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{COMPOSIO_BASE}/actions/execute",
                headers={"x-api-key": COMPOSIO_API_KEY},
                json={
                    "entityId": entity_id,
                    "actionName": "GOOGLECALENDAR_LIST_EVENTS",
                    "input": {
                        "calendarId": "primary",
                        "timeMin": day_start,
                        "timeMax": day_end,
                        "singleEvents": True,
                        "orderBy": "startTime",
                    },
                },
            )
            r.raise_for_status()
            data = r.json()
            return data.get("response", {}).get("items", [])
    except Exception as e:
        logger.error("Composio list_today_events failed entity=%s: %s", entity_id, e)
        return []


async def create_event(entity_id: str, summary: str, start: str, end: str,
                       description: str = "") -> dict | None:
    """Create a Google Calendar event for a tenant."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{COMPOSIO_BASE}/actions/execute",
                headers={"x-api-key": COMPOSIO_API_KEY},
                json={
                    "entityId": entity_id,
                    "actionName": "GOOGLECALENDAR_CREATE_EVENT",
                    "input": {
                        "calendarId": "primary",
                        "summary": summary,
                        "description": description,
                        "start": {"dateTime": start, "timeZone": "Europe/Paris"},
                        "end":   {"dateTime": end,   "timeZone": "Europe/Paris"},
                    },
                },
            )
            r.raise_for_status()
            return r.json().get("response", {})
    except Exception as e:
        logger.error("Composio create_event failed entity=%s: %s", entity_id, e)
        return None


def format_events_for_telegram(events: list[dict]) -> str:
    """Format event list as plain text for Telegram."""
    if not events:
        return "Aucune reunion aujourd'hui."
    lines = []
    for e in events:
        start = e.get("start", {})
        time_str = start.get("dateTime", start.get("date", ""))
        if "T" in time_str:
            try:
                dt = datetime.fromisoformat(time_str)
                time_str = dt.strftime("%H:%M")
            except Exception:
                pass
        summary = e.get("summary", "Sans titre")
        lines.append(f"  {time_str} - {summary}")
    return "\n".join(lines)
