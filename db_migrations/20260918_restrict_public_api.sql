-- This application's browser reads docs/api/history.json. Only the trusted
-- PostgreSQL backend needs database access; no client Data API policies exist.
-- Run as postgres. Re-running this migration is safe.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $$
DECLARE
    item record;
BEGIN
    IF current_user <> 'postgres' THEN
        RAISE EXCEPTION 'Run this migration as the postgres backend owner';
    END IF;
    FOR item IN
        SELECT c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', item.relname);
    END LOOP;
END $$;

-- Views can bypass table RLS, so remove their client grants as well.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, anon, authenticated, service_role;

-- Also block access to future objects, including objects created by managed
-- roles whose default grants cannot be changed by postgres. Existing explicit
-- postgres schema grants remain in place.
REVOKE ALL ON SCHEMA public FROM PUBLIC, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM PUBLIC, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON FUNCTIONS FROM anon, authenticated, service_role;
-- PostgreSQL's global PUBLIC function EXECUTE default is contained by the
-- schema boundary above; defaults in other schemas are deliberately unchanged.

-- Fail and roll back the entire migration if the security boundary is absent.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
          AND NOT c.relrowsecurity
    ) OR has_schema_privilege('anon', 'public', 'USAGE')
      OR has_schema_privilege('authenticated', 'public', 'USAGE')
      OR has_schema_privilege('service_role', 'public', 'USAGE') THEN
        RAISE EXCEPTION 'Public API access is not fully restricted';
    END IF;
    IF NOT has_schema_privilege('postgres', 'public', 'USAGE') OR EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) AS needed(privilege)
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
          AND NOT has_table_privilege('postgres', c.oid, needed.privilege)
    ) THEN
        RAISE EXCEPTION 'Backend access must remain available';
    END IF;
END $$;
COMMIT;
