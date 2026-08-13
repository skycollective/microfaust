-- 009: Projects (portfolio) + weekly outcomes + project linking on thoughts

-- Projects table: simple, one outcome per project
CREATE TABLE IF NOT EXISTS projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    outcome     TEXT,
    horizon     TEXT,
    status      TEXT NOT NULL DEFAULT 'active',  -- active | paused | done
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
CREATE POLICY projects_tenant ON projects
    USING (tenant_id = (current_setting('app.tenant_id'))::uuid);

-- Weekly outcomes: 3 per week, ranked
CREATE TABLE IF NOT EXISTS weekly_outcomes (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    week_start   DATE NOT NULL,        -- Monday of the week (ISO)
    rank         INT  NOT NULL CHECK (rank IN (1,2,3)),
    description  TEXT NOT NULL,
    project_name TEXT,                 -- loose FK — just the name for now
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | in_progress | done | carried_forward
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, week_start, rank)
);
ALTER TABLE weekly_outcomes ENABLE ROW LEVEL SECURITY;
CREATE POLICY weekly_outcomes_tenant ON weekly_outcomes
    USING (tenant_id = (current_setting('app.tenant_id'))::uuid);

-- Add project_name to thoughts so tasks can belong to a project
ALTER TABLE thoughts ADD COLUMN IF NOT EXISTS project_name TEXT;

-- Add Sunday PM planning job to pg_cron (17:00 Paris on Sundays)
-- Requires pg_cron extension already enabled (see 007_pg_cron.sql)
-- Run this block manually if pg_cron is available:
-- SELECT cron.schedule(
--     'sunday-planning',
--     '0 15 * * 0',   -- 15:00 UTC = 17:00 Paris
--     $$INSERT INTO job_queue (tenant_id, job_type, payload)
--       SELECT id, 'cron_sunday_pm', '{}'::jsonb
--       FROM tenants WHERE telegram_chat_id IS NOT NULL$$
-- );
