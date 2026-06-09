-- 006_values.sql
-- User values — anchors council deliberation and briefings
-- Note: 'values' is a reserved SQL keyword — table named user_values

CREATE TABLE IF NOT EXISTS user_values (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  description TEXT,
  rank        INT NOT NULL DEFAULT 0,   -- display order, 0 = top
  active      BOOLEAN DEFAULT true,
  created_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE user_values ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON user_values
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE INDEX IF NOT EXISTS idx_user_values_tenant ON user_values(tenant_id, rank);

CREATE TRIGGER user_values_updated
  BEFORE UPDATE ON user_values
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
