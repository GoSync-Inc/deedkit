-- Operator-only grant template (ANYM-936). Run in the archive database after migrations (schema 82).
-- session_holder is a NOLOGIN group: the session holders (ANYM-937 WhatsApp, ANYM-938 Telegram) connect as one
-- login per account, created by the hand release (docs/operations/production.md "Session holder role (hand
-- release)") as a member of this group and bound with INSERT INTO session_holder_accounts(role_name,account).
-- Row security (migration 082, policy session_holder_scope) limits every row a holder reads or writes to its bound account;
-- an unbound member, or SET ROLE session_holder, sees no row. No password here; no access to messages or any other
-- table, no DELETE, no DDL.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='session_holder') THEN
        CREATE ROLE session_holder NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO session_holder;
-- Every caller of session_claims, collector_leases and source_status needs this read: the session_holder_scope
-- policies read the binding as the caller. Migration 082 grants it too; a pg_restore --no-acl drops it and keeps
-- the policies, so this file, applied after such a restore with the other role files, gives it back.
GRANT SELECT ON session_holder_accounts TO PUBLIC;
-- ANYM-914's statements (session_claim.py ACQUIRE_SQL, REFRESH_SQL, RELEASE_SQL, WAITING_SQL, ROW_SQL) unchanged.
GRANT SELECT,INSERT,UPDATE ON session_claims TO session_holder;
-- The attach check of the collector's write lease (ANYM-937 criterion 3).
GRANT SELECT(account,owner,epoch,expires_at) ON collector_leases TO session_holder;
-- The claim's restore gate (ACQUIRE_SQL reads status='restored_disabled').
GRANT SELECT(account,status) ON source_status TO session_holder;
