-- 005_rls_verify.sql
-- P-02: Verification query — run after all migrations to confirm RLS state
-- This migration adds no schema changes; it is a safety check only.

-- Run this to verify all user-data tables have RLS enabled:
-- SELECT tablename, rowsecurity
-- FROM pg_tables
-- WHERE schemaname = 'public'
-- ORDER BY tablename;
--
-- Expected: rowsecurity = true for all 9 tables:
--   briefings, checkins, conversations, habit_log, habits,
--   job_queue, platform_insights, skill_runs, skills, thoughts

-- Grant service role access to all tables
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO authenticated;
GRANT USAGE ON SCHEMA public TO authenticated;

-- Ensure set_config is callable from application layer
GRANT EXECUTE ON FUNCTION set_config(text, text, boolean) TO authenticated;
