-- 002_job_queue.sql
-- Job queue — replaces Redis entirely (ADR-002)
-- SKIP LOCKED for multi-worker safety
-- idempotency_key prevents duplicate jobs on restart (P-01)

CREATE TABLE IF NOT EXISTS job_queue (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID REFERENCES tenants(id) ON DELETE CASCADE,   -- NULL = platform job
  job_type          TEXT NOT NULL,
  payload           JSONB DEFAULT '{}',
  status            TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'done', 'failed', 'dead')),
  attempts          INT DEFAULT 0,
  max_attempts      INT DEFAULT 3,
  scheduled_at      TIMESTAMPTZ DEFAULT now(),
  started_at        TIMESTAMPTZ,
  completed_at      TIMESTAMPTZ,
  error             TEXT,
  idempotency_key   TEXT UNIQUE,          -- format: {tenant_id}:{job_type}:{YYYY-MM-DD}
  created_at        TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE job_queue ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON job_queue
  USING (
    tenant_id IS NULL
    OR tenant_id = current_setting('app.tenant_id', true)::uuid
  );

CREATE INDEX IF NOT EXISTS idx_job_queue_pending ON job_queue(scheduled_at)
  WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_job_queue_tenant ON job_queue(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_job_queue_stale ON job_queue(scheduled_at)
  WHERE status = 'pending';

-- LISTEN/NOTIFY trigger — fires on every INSERT (ADR-002)
CREATE OR REPLACE FUNCTION notify_new_job()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('new_job', NEW.id::text);
  RETURN NEW;
END;
$$;

CREATE TRIGGER job_queue_notify
  AFTER INSERT ON job_queue
  FOR EACH ROW EXECUTE FUNCTION notify_new_job();

-- Dead-letter: move jobs exceeding max_attempts
CREATE OR REPLACE FUNCTION mark_dead_jobs()
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  UPDATE job_queue
  SET status = 'dead'
  WHERE status = 'failed'
    AND attempts >= max_attempts;
END;
$$;
