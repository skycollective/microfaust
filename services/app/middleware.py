"""
P-02: Tenant context middleware
Extracts tenant_id from Supabase JWT on every request.
Supports both legacy HS256 and new ECC (ES256) Supabase JWT signing keys.
"""
import logging
import os

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)

SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")
SUPABASE_URL        = os.environ.get("SUPABASE_URL", "")

# JWKS cache — fetched once at startup
_jwks_cache: dict | None = None


async def _get_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache is not None:
        return _jwks_cache
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json")
            r.raise_for_status()
            _jwks_cache = r.json()
            return _jwks_cache
    except Exception as e:
        logger.error("Failed to fetch JWKS: %s", e)
        return {"keys": []}


async def set_tenant_context(conn, tenant_id: str) -> None:
    """Call this at the start of every DB operation in a route handler."""
    await conn.execute(
        "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
    )


async def set_admin_context(conn) -> None:
    await conn.execute("SELECT set_config('app.role', 'admin', true)")


class TenantContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.tenant_id = await self._extract_tenant_id(request)
        return await call_next(request)

    async def _extract_tenant_id(self, request: Request) -> str | None:
        if request.url.path.startswith("/webhook/"):
            return None
        if request.url.path.startswith("/health"):
            return None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return await _verify_supabase_jwt(auth[7:])
        return None


async def _verify_supabase_jwt(token: str) -> str | None:
    from jose import jwt, JWTError, jwk
    from jose.utils import base64url_decode
    import json as _json

    # Try HS256 with legacy secret first (fast path)
    if SUPABASE_JWT_SECRET:
        try:
            payload = jwt.decode(
                token, SUPABASE_JWT_SECRET, algorithms=["HS256"],
                options={"verify_aud": False},
            )
            return payload.get("sub")
        except JWTError:
            pass  # fall through to JWKS

    # Try ECC (ES256) via JWKS
    try:
        jwks = await _get_jwks()
        for key_data in jwks.get("keys", []):
            try:
                public_key = jwk.construct(key_data)
                payload = jwt.decode(
                    token, public_key,
                    algorithms=["ES256", "RS256"],
                    options={"verify_aud": False},
                )
                return payload.get("sub")
            except JWTError:
                continue
    except Exception as e:
        logger.error("JWKS JWT verification error: %s", e)

    return None
