-- Applied by owner after migration, with separately generated login passwords.
GRANT USAGE ON SCHEMA fcs TO fcs_gateway, fcs_worker;
GRANT SELECT ON ALL TABLES IN SCHEMA fcs TO fcs_gateway, fcs_worker;
GRANT INSERT ON fcs.plans, fcs.operations, fcs.operation_steps, fcs.sync_jobs, fcs.assignments, fcs.audit TO fcs_gateway;
GRANT UPDATE ON fcs.assignments TO fcs_gateway;
GRANT INSERT,UPDATE,DELETE ON fcs.resources, fcs.occurrences, fcs.coverage TO fcs_worker;
GRANT INSERT ON fcs.audit TO fcs_worker;
GRANT UPDATE ON fcs.operations, fcs.operation_steps, fcs.sync_jobs TO fcs_worker;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA fcs TO fcs_gateway, fcs_worker;
-- Migration 2 (guardian rules): SELECT comes from the line above once it exists; rules stay owner-written.
-- Skipped on schema 1 so db-roles keeps working before migration 2 is applied.
DO $$ BEGIN
 IF to_regclass('fcs.projection_links') IS NOT NULL THEN
  GRANT INSERT,UPDATE ON fcs.projection_links TO fcs_worker;
 END IF;
END $$;
-- Migration 4 (calendar_schedule, ANYM-552): the gateway marks its previews and binds a plan to the operation that
-- consumed it; a plan's payload, scope and expiry stay insert-only. Skipped before migration 4.
DO $$ BEGIN
 IF to_regclass('fcs.schedule_selections') IS NOT NULL THEN
  GRANT UPDATE (token_hash, tool, request, operation_id) ON fcs.plans TO fcs_gateway;
  GRANT INSERT ON fcs.schedule_selections TO fcs_gateway;
 END IF;
END $$;
-- Neither process can issue/revoke keys, change grants, memberships, providers or write opt-in.
-- Provider secrets and raw encrypted source payload keys are outside PostgreSQL, worker-only.
