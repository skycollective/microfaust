"""
Composio Google Calendar client — multi-tenant.
Uses Composio Python SDK which handles API versioning internally.
"""
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY", "")

# App name for Google Calendar — varies by composio-core version
_GCAL_APP_NAMES = ["GOOGLECALENDAR", "GOOGLE_CALENDAR", "googlecalendar", "google_calendar"]


def _get_gcal_app():
    """Return the App enum value for Google Calendar, trying known names across versions."""
    from composio import App
    for name in _GCAL_APP_NAMES:
        try:
            return getattr(App, name)
        except AttributeError:
            continue
    # Last resort: pass string directly (works in some versions)
    return "GOOGLECALENDAR"


async def get_oauth_url(entity_id: str, redirect_url: str) -> str:
    """Return Composio OAuth initiation URL for Google Calendar.
    Calls Composio REST API directly — avoids SDK constructor 410 issue.
    """
    if not COMPOSIO_API_KEY:
        raise ValueError("COMPOSIO_API_KEY not set")

    async with httpx.AsyncClient(timeout=15) as client:
        # Composio v1 connected accounts endpoint
        r = await client.post(
            "https://backend.composio.dev/api/v1/connectedAccounts",
            headers={
                "x-api-key": COMPOSIO_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "appName": "googlecalendar",
                "entityId": entity_id,
                "redirectUri": redirect_url,
            },
        )
        logger.info("Composio connectedAccounts status=%s body=%s", r.status_code, r.text[:300])
        if r.status_code not in (200, 201):
            raise ValueError(f"Composio API error {r.status_code}: {r.text[:200]}")
        data = r.json()
        url = data.get("redirectUrl") or data.get("redirect_url") or data.get("connectionUrl")
        if not url:
            raise ValueError(f"No redirect URL in Composio response: {data}")
        return url


async def list_today_events(entity_id: str) -> list[dict]:
    """Fetch today's Google Calendar events for a tenant."""
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    day_end   = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    try:
        from composio import ComposioToolSet, Action
        import asyncio

        def _fetch():
            toolset = ComposioToolSet(api_key=COMPOSIO_API_KEY, entity_id=entity_id)
            result = toolset.execute_action(
                action=Action.GOOGLECALENDAR_LIST_EVENTS,
                params={
                    "calendarId": "primary",
                    "timeMin": day_start,
                    "timeMax": day_end,
                    "singleEvents": True,
                    "orderBy": "startTime",
                },
            )
            return result.get("response", {}).get("items", [])

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _fetch)
    except Exception as e:
        logger.error("Composio list_today_events failed entity=%s: %s", entity_id, e)
        return []


async def create_event(entity_id: str, summary: str, start: str, end: str,
                       description: str = "") -> dict | None:
    """Create a Google Calendar event for a tenant."""
    try:
        from composio import ComposioToolSet, Action
        import asyncio

        def _create():
            toolset = ComposioToolSet(api_key=COMPOSIO_API_KEY, entity_id=entity_id)
            result = toolset.execute_action(
                action=Action.GOOGLECALENDAR_CREATE_EVENT,
                params={
                    "calendarId": "primary",
                    "summary": summary,
                    "description": description,
                    "start": {"dateTime": start, "timeZone": "Europe/Paris"},
                    "end":   {"dateTime": end,   "timeZone": "Europe/Paris"},
                },
            )
            return result.get("response", {})

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _create)
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
