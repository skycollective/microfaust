"""P-01: /health/queue — stale job monitor + alert."""
import logging
import os

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health")

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")


def _require_admin(request: Request):
    if request.headers.get("X-Admin-Secret") != ADMIN_SECRET:
        raise HTTPException(status_code=403)


@router.get("/jwt-debug")
async def jwt_debug(request: Request):
    """Temporary: diagnose JWT verification without auth requirement."""
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token:
        return {"error": "no token"}
    import os
    from jose import jwt as jose_jwt, JWTError
    secret = os.environ.get("SUPABASE_JWT_SECRET", "")
    result = {"has_secret": bool(secret), "token_length": len(token)}
    # Try HS256
    try:
        payload = jose_jwt.decode(token, secret, algorithms=["HS256"],
                                  options={"verify_aud": False})
        result["hs256"] = "ok"
        result["sub"] = payload.get("sub")
        return result
    except JWTError as e:
        result["hs256_error"] = str(e)
    # Try without verification to inspect header
    try:
        header = jose_jwt.get_unverified_header(token)
        result["token_alg"] = header.get("alg")
        result["token_kid"] = header.get("kid")
    except Exception as e:
        result["header_error"] = str(e)
    return result


@router.get("/composio-debug")
async def composio_debug(request: Request):
    """Diagnose Composio SDK — no auth needed, remove before production."""
    import os
    result = {"api_key_set": bool(os.environ.get("COMPOSIO_API_KEY"))}
    try:
        import composio
        result["composio_version"] = getattr(composio, "__version__", "unknown")
    except ImportError as e:
        result["import_error"] = str(e)
        return result
    try:
        from composio import App
        gcal_names = ["GOOGLECALENDAR", "GOOGLE_CALENDAR", "googlecalendar"]
        for name in gcal_names:
            if hasattr(App, name):
                result["app_enum"] = name
                break
        else:
            result["app_enum"] = "NOT FOUND"
            result["available_apps"] = [a for a in dir(App) if "GOOGLE" in a.upper()]
    except Exception as e:
        result["app_error"] = str(e)
    try:
        from composio import ComposioToolSet
        toolset = ComposioToolSet(api_key=os.environ.get("COMPOSIO_API_KEY", ""))
        result["toolset_ok"] = True
    except Exception as e:
        result["toolset_error"] = str(e)

    # Test the REST API directly (what get_oauth_url now uses)
    try:
        import httpx as _httpx
        r = await _httpx.AsyncClient(timeout=10).get(
            "https://backend.composio.dev/api/v1/apps/googlecalendar",
            headers={"x-api-key": os.environ.get("COMPOSIO_API_KEY", "")},
        )
        result["rest_api_status"] = r.status_code
        if r.status_code == 200:
            result["rest_api_ok"] = True
        else:
            result["rest_api_body"] = r.text[:200]
    except Exception as e:
        result["rest_api_error"] = str(e)
    return result


@router.get("/queue")
async def queue_health(request: Request, _=Depends(_require_admin)):
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        stale = await conn.fetchval(
            """SELECT COUNT(*) FROM job_queue
               WHERE status='pending' AND scheduled_at < NOW()-INTERVAL '5 minutes'"""
        )
        dead = await conn.fetchval(
            "SELECT COUNT(*) FROM job_queue WHERE status='dead'"
        )
    if stale > 0:
        await _alert(f"{stale} stale job(s) pending > 5 min")
    return {"stale_jobs": stale, "dead_jobs": dead,
            "status": "ok" if stale == 0 else "degraded"}


async def _alert(msg: str):
    if not ALERT_WEBHOOK_URL:
        logger.warning("ALERT (no webhook): %s", msg)
        return
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(ALERT_WEBHOOK_URL, json={"text": f"⚠️ MICROFAUST: {msg}"})
    except Exception as e:
        logger.error("Alert send failed: %s", e)
