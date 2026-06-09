-- 007_pg_cron.sql
-- Replaces the entire scheduler/ Python service
-- pg_cron runs every 5 minutes and inserts jobs for tenants
-- whose local time falls in the briefing window.
-- Idempotency key prevents duplicates even if cron fires twice.

CREATE EXTENSION IF NOT EXISTS pg_cron;

-- ----------------------------------------
-- Core scheduling function
-- ----------------------------------------
CREATE OR REPLACE FUNCTION trigger_scheduled_jobs()
RETURNS void LANGUAGE plpgsql AS $$
BEGIN

  -- MORNING BRIEFINGS — local time 07:05–07:09
  INSERT INTO job_queue (tenant_id, job_type, idempotency_key, scheduled_at)
  SELECT
    id,
    'cron_morning',
    id::text || ':morning:' || (NOW() AT TIME ZONE timezone)::date,
    NOW()
  FROM tenants
  WHERE active = true
    AND telegram_chat_id IS NOT NULL
    AND to_char(NOW() AT TIME ZONE timezone, 'HH24:MI')
        BETWEEN '07:05' AND '07:09'
  ON CONFLICT (idempotency_key) DO NOTHING;

  -- EVENING BRIEFINGS — local time 18:00–18:04
  INSERT INTO job_queue (tenant_id, job_type, idempotency_key, scheduled_at)
  SELECT
    id,
    'cron_evening',
    id::text || ':evening:' || (NOW() AT TIME ZONE timezone)::date,
    NOW()
  FROM tenants
  WHERE active = true
    AND telegram_chat_id IS NOT NULL
    AND to_char(NOW() AT TIME ZONE timezone, 'HH24:MI')
        BETWEEN '18:00' AND '18:04'
  ON CONFLICT (idempotency_key) DO NOTHING;

  -- WEEKLY REVIEW — Sunday local time 08:00–08:04
  INSERT INTO job_queue (tenant_id, job_type, idempotency_key, scheduled_at)
  SELECT
    id,
    'cron_weekly_review',
    id::text || ':weekly:' || date_trunc('week', (NOW() AT TIME ZONE timezone)::date),
    NOW()
  FROM tenants
  WHERE active = true
    AND telegram_chat_id IS NOT NULL
    AND to_char(NOW() AT TIME ZONE timezone, 'D')    = '1'  -- Sunday
    AND to_char(NOW() AT TIME ZONE timezone, 'HH24:MI')
        BETWEEN '08:00' AND '08:04'
  ON CONFLICT (idempotency_key) DO NOTHING;

END;
$$;

-- ----------------------------------------
-- Register cron job — every 5 minutes
-- ----------------------------------------
-- Remove existing if re-running this migration
SELECT cron.unschedule('microfaust-scheduler')
WHERE EXISTS (
  SELECT 1 FROM cron.job WHERE jobname = 'microfaust-scheduler'
);

SELECT cron.schedule(
  'microfaust-scheduler',
  '*/5 * * * *',
  $$SELECT trigger_scheduled_jobs()$$
);

-- ----------------------------------------
-- Dead-letter cleanup — daily at 03:00 UTC
-- ----------------------------------------
SELECT cron.unschedule('microfaust-cleanup')
WHERE EXISTS (
  SELECT 1 FROM cron.job WHERE jobname = 'microfaust-cleanup'
);

SELECT cron.schedule(
  'microfaust-cleanup',
  '0 3 * * *',
  $$
    -- Archive done jobs older than 30 days
    DELETE FROM job_queue
    WHERE status IN ('done', 'dead')
      AND created_at < NOW() - INTERVAL '30 days';
  $$
);
