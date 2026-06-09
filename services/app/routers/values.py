"""
Values CRUD — GET/POST/PUT/DELETE /values
Used by the app UI (Mon conseil d'experts — values panel)
"""
import logging
import uuid

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from middleware import set_tenant_context

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/values", tags=["values"])


class ValueIn(BaseModel):
    name: str
    description: str | None = None
    rank: int = 0


def _get_tenant(request: Request) -> str:
    tid = request.state.tenant_id
    if not tid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return tid


@router.get("")
async def list_values(request: Request):
    tid = _get_tenant(request)
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        await set_tenant_context(conn, tid)
        rows = await conn.fetch(
            """SELECT id, name, description, rank
               FROM user_values
               WHERE tenant_id = $1 AND active = true
               ORDER BY rank, created_at""",
            uuid.UUID(tid),
        )
    return [dict(r) for r in rows]


@router.post("", status_code=201)
async def create_value(body: ValueIn, request: Request):
    tid = _get_tenant(request)
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        await set_tenant_context(conn, tid)
        row = await conn.fetchrow(
            """INSERT INTO user_values (tenant_id, name, description, rank)
               VALUES ($1, $2, $3, $4)
               RETURNING id, name, description, rank""",
            uuid.UUID(tid), body.name, body.description, body.rank,
        )
    return dict(row)


@router.put("/{value_id}")
async def update_value(value_id: str, body: ValueIn, request: Request):
    tid = _get_tenant(request)
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        await set_tenant_context(conn, tid)
        row = await conn.fetchrow(
            """UPDATE user_values SET name=$3, description=$4, rank=$5
               WHERE id=$1 AND tenant_id=$2
               RETURNING id, name, description, rank""",
            uuid.UUID(value_id), uuid.UUID(tid),
            body.name, body.description, body.rank,
        )
    if not row:
        raise HTTPException(status_code=404)
    return dict(row)


@router.delete("/{value_id}", status_code=204)
async def delete_value(value_id: str, request: Request):
    tid = _get_tenant(request)
    pool: asyncpg.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        await set_tenant_context(conn, tid)
        await conn.execute(
            "UPDATE user_values SET active=false WHERE id=$1 AND tenant_id=$2",
            uuid.UUID(value_id), uuid.UUID(tid),
        )
