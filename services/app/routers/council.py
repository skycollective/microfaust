"""
Council (skills) CRUD — GET/POST/PUT/DELETE /council
Maps to the skills table (tenant_id = user = personal expert).
"""
import logging
import uuid

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from middleware import set_tenant_context

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/council", tags=["council"])


class ExpertIn(BaseModel):
    name: str
    role: str | None = None
    system_prompt: str
    signature: str | None = None
    active: bool = True


def _get_tenant(request: Request) -> str:
    tid = request.state.tenant_id
    if not tid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return tid


@router.get("")
async def list_experts(request: Request):
    tid = _get_tenant(request)
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        await set_tenant_context(conn, tid)
        rows = await conn.fetch(
            """SELECT id, name, slug,
                      metadata->>'role' AS role,
                      metadata->>'signature' AS signature,
                      active
               FROM skills
               WHERE tenant_id = $1
               ORDER BY created_at""",
            uuid.UUID(tid),
        )
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def add_expert(body: ExpertIn, request: Request):
    tid = _get_tenant(request)
    pool: asyncpg.Pool = request.app.state.pool
    import json, re
    slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")
    meta = json.dumps({"role": body.role, "signature": body.signature})
    async with pool.acquire() as conn:
        await set_tenant_context(conn, tid)
        row = await conn.fetchrow(
            """INSERT INTO skills
                 (tenant_id, slug, name, system_prompt, trigger_type, metadata, active)
               VALUES ($1,$2,$3,$4,'manual',$5,$6)
               RETURNING id, name, slug, active""",
            uuid.UUID(tid), slug, body.name,
            body.system_prompt, meta, body.active,
        )
    return dict(row)


@router.put("/{expert_id}")
async def update_expert(expert_id: str, body: ExpertIn, request: Request):
    tid = _get_tenant(request)
    pool: asyncpg.Pool = request.app.state.pool
    import json
    meta = json.dumps({"role": body.role, "signature": body.signature})
    async with pool.acquire() as conn:
        await set_tenant_context(conn, tid)
        row = await conn.fetchrow(
            """UPDATE skills
               SET name=$3, system_prompt=$4, metadata=$5, active=$6
               WHERE id=$1 AND tenant_id=$2
               RETURNING id, name, slug, active""",
            uuid.UUID(expert_id), uuid.UUID(tid),
            body.name, body.system_prompt, meta, body.active,
        )
    if not row:
        raise HTTPException(status_code=404)
    return dict(row)


@router.delete("/{expert_id}", status_code=204)
async def remove_expert(expert_id: str, request: Request):
    tid = _get_tenant(request)
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        await set_tenant_context(conn, tid)
        await conn.execute(
            "UPDATE skills SET active=false WHERE id=$1 AND tenant_id=$2",
            uuid.UUID(expert_id), uuid.UUID(tid),
        )
