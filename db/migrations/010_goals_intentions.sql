-- 010: Macro goals + daily intentions

CREATE TABLE IF NOT EXISTS macro_goals (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    description  TEXT NOT NULL,
    project_name TEXT,                      -- loose link to a project
    deadline     DATE,
    current_state TEXT,                     -- snapshot of where they are now
    status       TEXT NOT NULL DEFAULT 'active',  -- active | achieved | abandoned
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE macro_goals ENABLE ROW LEVEL SECURITY;
CREATE POLICY macro_goals_tenant ON macro_goals
    USING (tenant_id = (current_setting('app.tenant_id'))::uuid);

CREATE TABLE IF NOT EXISTS daily_intentions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    intention   TEXT NOT NULL,
    held        BOOLEAN,                    -- set during evening check-in
    reflection  TEXT,                       -- evening note on the intention
    date        DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, date)
);
ALTER TABLE daily_intentions ENABLE ROW LEVEL SECURITY;
CREATE POLICY daily_intentions_tenant ON daily_intentions
    USING (tenant_id = (current_setting('app.tenant_id'))::uuid);

-- Add done flag to thoughts so mark_done works without hard-delete
ALTER TABLE thoughts ADD COLUMN IF NOT EXISTS done BOOLEAN NOT NULL DEFAULT false;
