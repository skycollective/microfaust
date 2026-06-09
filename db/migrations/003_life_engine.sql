-- 003_life_engine.sql
-- Habit tracking, check-ins, briefing log — ported from THOT life_engine schema

CREATE TABLE IF NOT EXISTS habits (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  description TEXT,
  frequency   TEXT DEFAULT 'daily'
              CHECK (frequency IN ('daily', 'weekdays', 'weekends', 'weekly', 'custom')),
  time_of_day TEXT DEFAULT 'morning'
              CHECK (time_of_day IN ('morning', 'midday', 'evening', 'anytime')),
  active      BOOLEAN DEFAULT true,
  created_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE habits ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON habits
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE TABLE IF NOT EXISTS habit_log (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  habit_id     UUID NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
  date         DATE NOT NULL DEFAULT CURRENT_DATE,
  completed    BOOLEAN DEFAULT true,
  note         TEXT,
  created_at   TIMESTAMPTZ DEFAULT now(),
  UNIQUE(habit_id, date)
);

ALTER TABLE habit_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON habit_log
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE TABLE IF NOT EXISTS checkins (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  briefing_id   UUID,                     -- FK to briefings.id (added in 004)
  habit_id      UUID REFERENCES habits(id) ON DELETE SET NULL,
  checkin_type  TEXT NOT NULL CHECK (checkin_type IN ('mood', 'energy', 'health', 'custom')),
  value         TEXT NOT NULL,
  notes         TEXT,
  responded     BOOLEAN DEFAULT false,
  response_text TEXT,
  created_at    TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE checkins ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON checkins
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE INDEX IF NOT EXISTS idx_habits_tenant ON habits(tenant_id);
CREATE INDEX IF NOT EXISTS idx_habit_log_tenant_date ON habit_log(tenant_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_checkins_tenant_date ON checkins(tenant_id, created_at DESC);

CREATE TRIGGER habits_updated BEFORE UPDATE ON habits FOR EACH ROW EXECUTE FUNCTION update_updated_at();
