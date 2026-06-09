-- 004_platform.sql
-- Skill runs (audit + learning loop), briefings log, platform insights

CREATE TABLE IF NOT EXISTS briefings (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  briefing_type   TEXT NOT NULL CHECK (briefing_type IN (
                    'morning', 'pre_meeting', 'checkin', 'evening',
                    'habit_reminder', 'weekly_review', 'custom')),
  content         TEXT NOT NULL,
  delivered_via   TEXT DEFAULT 'telegram' CHECK (delivered_via IN ('telegram')),
  user_responded  BOOLEAN DEFAULT false,
  created_at      TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE briefings ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON briefings
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE INDEX IF NOT EXISTS idx_briefings_tenant_date ON briefings(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_briefings_type_date ON briefings(tenant_id, briefing_type, created_at DESC);

-- Add FK from checkins to briefings now that briefings table exists
ALTER TABLE checkins
  ADD CONSTRAINT fk_checkins_briefing
  FOREIGN KEY (briefing_id) REFERENCES briefings(id) ON DELETE SET NULL;

-- ----------------------------------------
-- Skill runs — audit log + learning loop input
-- ----------------------------------------
CREATE TABLE IF NOT EXISTS skill_runs (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  skill_id     UUID REFERENCES skills(id) ON DELETE SET NULL,
  skill_slug   TEXT NOT NULL,
  input        TEXT,
  output       TEXT,
  tokens_used  INT DEFAULT 0,
  success      BOOLEAN DEFAULT true,
  error        TEXT,
  duration_ms  INT,
  created_at   TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE skill_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON skill_runs
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE INDEX IF NOT EXISTS idx_skill_runs_tenant_date ON skill_runs(tenant_id, created_at DESC);

-- ----------------------------------------
-- Platform insights — admin only, no tenant_id
-- ----------------------------------------
CREATE TABLE IF NOT EXISTS platform_insights (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  insight_type TEXT NOT NULL,
  content      JSONB NOT NULL,
  raw_stats    JSONB DEFAULT '{}',
  week         DATE NOT NULL,             -- Monday of the analysis week
  created_at   TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE platform_insights ENABLE ROW LEVEL SECURITY;
CREATE POLICY admin_only ON platform_insights
  USING (current_setting('app.role', true) = 'admin');

CREATE INDEX IF NOT EXISTS idx_platform_insights_week ON platform_insights(week DESC);
