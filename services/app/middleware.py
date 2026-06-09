"""
P-02: Tenant context middleware
Extracts tenant_id from Supabase JWT and sets app.tenant_id on every request.
"""
import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)

SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")


async def set_tenant_context(conn, tenant_id: str) -> None:
    """Call this at the start of every DB operation in a route handler."""
    await conn.execute(
        "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
    )


async def set_admin_context(conn) -> None:
    await conn.execute("SELECT set_config('app.role', 'admin', true)")


class TenantContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.tenant_id = self._extract_tenant_id(request)
        return await call_next(request)

    def _extract_tenant_id(self, request: Request) -> str | None:
        # Webhook path carries tenant UUID — extracted by route handler
        if request.url.path.startswith("/webhook/"):
            return None
        if request.url.path.startswith("/health"):
            return None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return _verify_supabase_jwt(auth[7:])
        return None


def _verify_supabase_jwt(token: str) -> str | None:
    """
    Supabase JWTs are signed with SUPABASE_JWT_SECRET.
    The 'sub' claim is the Supabase user UUID — used as tenant lookup key.
    """
    from jose import jwt, JWTError
    try:
        payload = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"],
                             options={"verify_aud": False})
        return payload.get("sub")  # Supabase user UUID
    except JWTError:
        return None
