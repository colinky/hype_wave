-- Apply after restoring public with --no-owner --no-acl, as avnadmin.
-- Provision the hype_sync LOGIN password separately; never put it in this file.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
DO $$
DECLARE item record;
BEGIN
    IF current_user <> 'avnadmin' THEN
        RAISE EXCEPTION 'Run as the Aiven schema owner avnadmin';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='hype_sync'
                   AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
                   AND NOT rolbypassrls AND NOT rolinherit) THEN
        RAISE EXCEPTION 'Provision the restricted hype_sync role first';
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO hype_sync', current_database());
    EXECUTE format('REVOKE CREATE ON DATABASE %I FROM PUBLIC, hype_sync', current_database());
END $$;
REVOKE ALL ON SCHEMA public FROM PUBLIC, hype_sync;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, hype_sync;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, hype_sync;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, hype_sync;
GRANT USAGE ON SCHEMA public TO hype_sync;
DO $$
DECLARE item record;
BEGIN
    FOR item IN SELECT c.relname, c.relowner FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
    LOOP
        IF item.relowner <> 'avnadmin'::regrole THEN
            RAISE EXCEPTION 'Unexpected table owner';
        END IF;
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', item.relname);
        EXECUTE format('DROP POLICY IF EXISTS hype_sync_access ON public.%I', item.relname);
        IF item.relname = 'schema_migrations' THEN
            EXECUTE format('GRANT SELECT ON public.%I TO hype_sync', item.relname);
            EXECUTE format('CREATE POLICY hype_sync_access ON public.%I FOR SELECT TO hype_sync USING (true)', item.relname);
        ELSE
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO hype_sync', item.relname);
            EXECUTE format('CREATE POLICY hype_sync_access ON public.%I FOR ALL TO hype_sync USING (true) WITH CHECK (true)', item.relname);
        END IF;
    END LOOP;
END $$;
GRANT SELECT ON public.frontend_history_source TO hype_sync;
ALTER DEFAULT PRIVILEGES FOR ROLE avnadmin IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE avnadmin IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE avnadmin REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER ROLE hype_sync SET search_path TO public;
ALTER ROLE hype_sync SET statement_timeout TO '180s';
ALTER ROLE hype_sync SET idle_in_transaction_session_timeout TO '180s';
COMMIT;
