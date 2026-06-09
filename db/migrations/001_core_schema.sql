-- 001_core_schema.sql
-- Core tables: tenants, thoughts, skills
-- Run order: first

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ----------------------------------------
-- Tenants
-- ----------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email               TEXT UNIQUE,
  telegram_bot_token  TEXT,               -- stored encrypted at app layer, never logged
  telegram_chat_id    TEXT,               -- captured on /start (P-05)
  timezone            TEXT NOT NULL DEFAULT 'Europe/Paris',
  composio_entity_id  TEXT UNIQUE,        -- Composio per-user entity
  active              BOOLEAN DEFAULT true,
  created_at          TIMESTAMPTZ DEFAULT now(),
  updated_at          TIMESTAMPTZ DEFAULT now()
);

-- ----------------------------------------
-- Thoughts (OB1 core — extended with tenant_id)
-- ----------------------------------------
CREATE TABLE IF NOT EXISTS thoughts (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  content     TEXT NOT NULL,
  embedding   vector(1536),
  source      TEXT DEFAULT 'mcp',
  tags        TEXT[] DEFAULT '{}',
  metadata    JSONB DEFAULT '{}',
  created_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE thoughts ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON thoughts
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE INDEX IF NOT EXISTS idx_thoughts_tenant ON thoughts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_thoughts_embedding ON thoughts USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ----------------------------------------
-- Skills (tenant_id NULL = global)
-- ----------------------------------------
CREATE TABLE IF NOT EXISTS skills (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID REFERENCES tenants(id) ON DELETE CASCADE,  -- NULL = global
  slug            TEXT NOT NULL,
  name            TEXT NOT NULL,
  system_prompt   TEXT NOT NULL,
  trigger_type    TEXT NOT NULL CHECK (trigger_type IN ('keyword', 'cron', 'webhook', 'manual')),
  trigger_value   TEXT,                   -- regex for keyword, cron expression for cron
  required_tools  TEXT[] DEFAULT '{}',    -- Composio tool names
  active          BOOLEAN DEFAULT true,
  created_at      TIMESTAMPTZ DEFAULT now(),
  UNIQUE(tenant_id, slug)
);

ALTER TABLE skills ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_or_global ON skills
  USING (
    tenant_id IS NULL
    OR tenant_id = current_setting('app.tenant_id', true)::uuid
  );

CREATE INDEX IF NOT EXISTS idx_skills_tenant ON skills(tenant_id);
CREATE INDEX IF NOT EXISTS idx_skills_global ON skills(tenant_id) WHERE tenant_id IS NULL;

-- ----------------------------------------
-- Conversations (Telegram thread history per skill)
-- ----------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  skill_slug  TEXT NOT NULL,
  role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  content     TEXT NOT NULL,
  tokens      INT DEFAULT 0,
  created_at  TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON conversations
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE INDEX IF NOT EXISTS idx_conversations_tenant_skill ON conversations(tenant_id, skill_slug, created_at DESC);

-- ----------------------------------------
-- Vector search function
-- ----------------------------------------
CREATE OR REPLACE FUNCTION match_thoughts(
  p_tenant_id       UUID,
  query_embedding   vector(1536),
  match_threshold   FLOAT DEFAULT 0.5,
  match_count       INT DEFAULT 10
)
RETURNS TABLE (
  id          UUID,
  content     TEXT,
  metadata    JSONB,
  similarity  FLOAT,
  created_at  TIMESTAMPTZ
) LANGUAGE plpgsql AS $$
BEGIN
  RETURN QUERY
  SELECT t.id, t.content, t.metadata,
         1 - (t.embedding <=> query_embedding) AS similarity,
         t.created_at
  FROM thoughts t
  WHERE t.tenant_id = p_tenant_id
    AND 1 - (t.embedding <=> query_embedding) > match_threshold
  ORDER BY t.embedding <=> query_embedding
  LIMIT match_count;
END;
$$;

-- Auto-update timestamps
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

CREATE TRIGGER tenants_updated BEFORE UPDATE ON tenants FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER thoughts_updated BEFORE UPDATE ON thoughts FOR EACH ROW EXECUTE FUNCTION update_updated_at();
