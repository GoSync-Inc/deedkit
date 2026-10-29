-- Applied by owner after migration, with separately generated login passwords.
-- ANYM-601: every privilege names its table or sequence; a table a migration adds gets none until this file names it
-- under that migration's guard. roles_expected.json holds the same list for `db-roles --check`.
GRANT USAGE ON SCHEMA fcs TO fcs_gateway, fcs_worker;
GRANT SELECT ON fcs.migrations, fcs.people, fcs.groups, fcs.memberships, fcs.guardians, fcs.connections, fcs.calendars,
 fcs.calendar_grants, fcs.bindings, fcs.principals, fcs.agent_calendars, fcs.resources, fcs.occurrences, fcs.coverage,
 fcs.sync_jobs, fcs.plans, fcs.operations, fcs.operation_steps, fcs.assignments, fcs.audit TO fcs_gateway, fcs_worker;
GRANT INSERT ON fcs.plans, fcs.operations, fcs.operation_steps, fcs.sync_jobs, fcs.assignments, fcs.audit TO fcs_gateway;
GRANT UPDATE ON fcs.assignments TO fcs_gateway;
GRANT INSERT,UPDATE,DELETE ON fcs.resources, fcs.occurrences, fcs.coverage TO fcs_worker;
GRANT INSERT ON fcs.audit TO fcs_worker;
GRANT UPDATE ON fcs.operations, fcs.operation_steps, fcs.sync_jobs TO fcs_worker;
GRANT USAGE ON SEQUENCE fcs.audit_id_seq TO fcs_gateway, fcs_worker;
-- Migration 2 (guardian rules): both roles read rules and links; rules stay owner-written.
-- Skipped on schema 1 so db-roles keeps working before migration 2 is applied.
DO $$ BEGIN
 IF to_regclass('fcs.projection_links') IS NOT NULL THEN
  GRANT SELECT ON fcs.guardian_rules, fcs.projection_links TO fcs_gateway, fcs_worker;
  GRANT INSERT,UPDATE ON fcs.projection_links TO fcs_worker;
 END IF;
END $$;
-- Migration 3 (ANYM-495): principal_operations is the operator CLI's only; no service role gets it. A db-roles run
-- of the former `ALL TABLES` line granted it SELECT (release 2026-10-28); this rerun revokes that.
DO $$ BEGIN
 IF to_regclass('fcs.principal_operations') IS NOT NULL THEN
  REVOKE ALL ON fcs.principal_operations FROM fcs_gateway, fcs_worker;
 END IF;
END $$;
-- Migration 4 (calendar_schedule, ANYM-552): the gateway marks its previews and binds a plan to the operation that
-- consumed it; a plan's payload, scope and expiry stay insert-only. Skipped before migration 4.
DO $$ BEGIN
 IF to_regclass('fcs.schedule_selections') IS NOT NULL THEN
  GRANT SELECT ON fcs.schedule_selections TO fcs_gateway, fcs_worker;
  GRANT UPDATE (token_hash, tool, request, operation_id) ON fcs.plans TO fcs_gateway;
  GRANT INSERT ON fcs.schedule_selections TO fcs_gateway;
 END IF;
END $$;
-- Migration 5 (invitations, ANYM-555): both roles read the owner's opt-in (gateway previews, worker re-checks before the
-- send, core.py validate_plan); only the owner CLI writes it. Skipped before migration 5.
DO $$ BEGIN
 IF to_regclass('fcs.invitation_opt_ins') IS NOT NULL THEN
  GRANT SELECT ON fcs.invitation_opt_ins TO fcs_gateway, fcs_worker;
 END IF;
END $$;
-- Neither process can issue/revoke keys, change grants, memberships, providers or write opt-in.
-- Provider secrets and raw encrypted source payload keys are outside PostgreSQL, worker-only.
-- Migration 6 (member links, ANYM-554): the gateway reads the owner's links to resolve a person in
-- calendar_find_time; only the owner CLI writes them. Skipped before migration 6.
DO $$ BEGIN
 IF to_regclass('fcs.member_people') IS NOT NULL THEN
  GRANT SELECT ON fcs.member_people TO fcs_gateway;
 END IF;
END $$;
-- Migration 7 (series reads, ANYM-614): the gateway asks for a read of a series and reads the answer; the worker
-- takes the request and writes the answer. Skipped before migration 7.
DO $$ BEGIN
 IF to_regclass('fcs.series_reads') IS NOT NULL THEN
  GRANT SELECT ON fcs.series_reads TO fcs_gateway, fcs_worker;
  GRANT INSERT ON fcs.series_reads TO fcs_gateway;
  GRANT UPDATE (state, result, error) ON fcs.series_reads TO fcs_worker;
 END IF;
END $$;
