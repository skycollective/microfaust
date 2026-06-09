# MICROFAUST — Multi-Tenant OB1 Platform

## What this is

MICROFAUST is the multi-tenant SaaS evolution of OB1 Open Brain. It hosts multiple users, each getting their own Telegram-based AI assistant with persistent memory, habit tracking, calendar awareness, and self-improving skills.

Architecture: Railway Postgres + pgvector · FastAPI (API service) · Async Worker · APScheduler · Composio (OAuth/tools) · Telegram Bot API webhooks

ADR source: `../THOT/Downloads/OB1_to_MultiTenant_ADR.docx`
Pre-launch issues: `../THOT/Downloads/PreLaunch_Priority_Issues.docx`

## Project path

`C:\Users\abhinav.agarwal\OneDrive - talan.com\Bureau\Claude Project\PROJECTS\MICROFAUST\`

## Directory map

```
MICROFAUST/
├── CLAUDE.md
├── .env.example
├── railway.toml
├── db/
│   └── migrations/          # Numbered SQL — always via migration files, never ORM auto-migrate
├── scripts/
│   └── lint_migrations.py   # CI: fails build if CREATE TABLE has no RLS (P-02)
└── services/
    ├── api/                 # FastAPI — webhooks, REST, OAuth redirects
    ├── worker/              # Async job processor — Composio, Anthropic, Telegram send
    └── scheduler/           # APScheduler — inserts cron jobs into job_queue
```

## Tech stack

| Layer | Tool | Why |
|---|---|---|
| Database | Railway Postgres + pgvector | Full ownership, same vector capability as Supabase |
| Job queue | `job_queue` table + SKIP LOCKED + LISTEN/NOTIFY | No Redis — Postgres handles our throughput |
| External tools | Composio | 1000+ tools, zero OAuth code |
| LLM | Anthropic claude-sonnet-4-6 | Default; override per skill row |
| Telegram | Bot API webhook (not polling) | < 500ms latency, one URL per tenant |
| Auth | JWT → tenant_id extraction | RLS enforced at DB layer |

## Multi-tenancy rules

- Every user-data table has `tenant_id UUID NOT NULL`
- RLS is enabled on every table — enforced at DB layer, not app layer
- `app.tenant_id` session variable set on every DB connection (middleware + worker)
- Skills with `tenant_id IS NULL` = global (available to all tenants)
- `platform_insights` = admin-only (no tenant_id)

## Non-negotiable rules (inherited)

1. No ORM auto-migration — all schema changes via numbered files in `db/migrations/`
2. Never log bot tokens, API keys, or embeddings (P-04)
3. `/forget all` must hard-delete rows — not tag or soft-delete (P-03, GDPR Art. 17)
4. Every new `CREATE TABLE` must have RLS in the same migration file — CI enforces this
5. Worker drain() runs on NOTIFY *and* every 60 seconds — never rely on NOTIFY alone (P-01)

## Railway services (4)

| Service | Entry point | Role |
|---|---|---|
| `api` | `services/api/main.py` | Webhook receiver, REST API, OAuth redirects |
| `worker` | `services/worker/main.py` | Job processor — LLM calls, Telegram sends |
| `scheduler` | `services/scheduler/main.py` | Inserts cron jobs (morning, evening, etc.) |
| `postgres` | Railway managed | Primary DB, vector store, job queue |

## Pre-launch checklist (all must pass before first EU user)

- [ ] P-01: Worker drain() + 60s periodic + /health/queue + idempotency_key
- [ ] P-02: RLS on all 9 tables + migration linter in CI
- [ ] P-03: /forget Telegram command + hard delete + backup policy
- [ ] P-04: Bot token redacted from all logs
- [ ] P-05: /start command captures chat_id + sends onboarding
- [ ] P-06: Morning briefing jobs staggered (not all at :05)

## Tenant 0 — Abhinav (THOT migration)

When platform is stable, migrate THOT data:
1. Export `thoughts` from Supabase → import with `tenant_id = abhinav_uuid`
2. Import habits, checkins from `life_engine_*` tables
3. Register Abhinav's Telegram bot token in `tenants` table
4. Retire THOT Supabase project
