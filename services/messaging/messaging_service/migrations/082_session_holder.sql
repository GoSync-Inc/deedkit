-- rollout: manual
-- Risk tier: non-critical (one new table, row security with one permissive policy on three existing tables; no
-- existing row, column or grant changes; docs/operations/production.md "Migration risk tiers").
-- ANYM-936: a session holder login (a member of the NOLOGIN role session_holder, session_holder_role.sql) reaches
-- only its own account's rows of session_claims, collector_leases and source_status. Every other role keeps exactly
-- what its grants give today: the one policy on each table passes every row for a caller that is not a holder, and a
-- table owner bypasses row security, so migrations, backups and owner functions are unchanged. The policy calls no
-- function: two uncorrelated subqueries, which PostgreSQL plans once per statement. No policy names a role, so a
-- restore without ACLs needs nothing from it; session_holder is a fixed restore role because the role file names it.
-- Every statement is repeatable (a rewound ledger in tests runs it again). The new role file changes the role-file set, so the range pauses and ships by the hand release in
-- docs/operations/production.md "Session holder role (hand release, ANYM-936)".
SET LOCAL lock_timeout='2s';

-- Owner-maintained binding of one holder login to one account. Readable by every role because the policy below
-- reads it as the caller; it holds role names and account IDs only (as collector_account_roles, 037).
CREATE TABLE IF NOT EXISTS session_holder_accounts (
 role_name text PRIMARY KEY CHECK(length(role_name) BETWEEN 1 AND 63),
 account text NOT NULL UNIQUE REFERENCES accounts(id),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
GRANT SELECT ON session_holder_accounts TO PUBLIC;

-- A caller is a holder when its current role or its login (never a superuser) is session_holder or reaches it
-- through memberships that can use it (inherit or set). A holder passes a row only when that row's account is the
-- one its current role is bound to, and every bound identity of the session agrees; so SET ROLE session_holder, an
-- unbound login or a login bound elsewhere passes no row. Anyone else passes every row.
ALTER TABLE session_claims ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS session_holder_scope ON session_claims;
CREATE POLICY session_holder_scope ON session_claims
 USING (NOT EXISTS (WITH RECURSIVE up(id) AS (
   SELECT u.oid FROM pg_catalog.pg_roles u WHERE u.rolname IN (current_user,session_user) AND NOT u.rolsuper
   UNION SELECT m.roleid FROM pg_catalog.pg_auth_members m JOIN up ON m.member=up.id
   WHERE m.inherit_option OR m.set_option)
  SELECT FROM up JOIN pg_catalog.pg_roles g ON g.oid=up.id WHERE g.rolname='session_holder')
  OR (account IN (SELECT b.account FROM public.session_holder_accounts b WHERE b.role_name=current_user)
   AND account=ALL(SELECT b.account FROM public.session_holder_accounts b WHERE b.role_name IN (current_user,session_user))))
 WITH CHECK (NOT EXISTS (WITH RECURSIVE up(id) AS (
   SELECT u.oid FROM pg_catalog.pg_roles u WHERE u.rolname IN (current_user,session_user) AND NOT u.rolsuper
   UNION SELECT m.roleid FROM pg_catalog.pg_auth_members m JOIN up ON m.member=up.id
   WHERE m.inherit_option OR m.set_option)
  SELECT FROM up JOIN pg_catalog.pg_roles g ON g.oid=up.id WHERE g.rolname='session_holder')
  OR (account IN (SELECT b.account FROM public.session_holder_accounts b WHERE b.role_name=current_user)
   AND account=ALL(SELECT b.account FROM public.session_holder_accounts b WHERE b.role_name IN (current_user,session_user))));
ALTER TABLE collector_leases ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS session_holder_scope ON collector_leases;
CREATE POLICY session_holder_scope ON collector_leases
 USING (NOT EXISTS (WITH RECURSIVE up(id) AS (
   SELECT u.oid FROM pg_catalog.pg_roles u WHERE u.rolname IN (current_user,session_user) AND NOT u.rolsuper
   UNION SELECT m.roleid FROM pg_catalog.pg_auth_members m JOIN up ON m.member=up.id
   WHERE m.inherit_option OR m.set_option)
  SELECT FROM up JOIN pg_catalog.pg_roles g ON g.oid=up.id WHERE g.rolname='session_holder')
  OR (account IN (SELECT b.account FROM public.session_holder_accounts b WHERE b.role_name=current_user)
   AND account=ALL(SELECT b.account FROM public.session_holder_accounts b WHERE b.role_name IN (current_user,session_user))))
 WITH CHECK (NOT EXISTS (WITH RECURSIVE up(id) AS (
   SELECT u.oid FROM pg_catalog.pg_roles u WHERE u.rolname IN (current_user,session_user) AND NOT u.rolsuper
   UNION SELECT m.roleid FROM pg_catalog.pg_auth_members m JOIN up ON m.member=up.id
   WHERE m.inherit_option OR m.set_option)
  SELECT FROM up JOIN pg_catalog.pg_roles g ON g.oid=up.id WHERE g.rolname='session_holder')
  OR (account IN (SELECT b.account FROM public.session_holder_accounts b WHERE b.role_name=current_user)
   AND account=ALL(SELECT b.account FROM public.session_holder_accounts b WHERE b.role_name IN (current_user,session_user))));
ALTER TABLE source_status ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS session_holder_scope ON source_status;
CREATE POLICY session_holder_scope ON source_status
 USING (NOT EXISTS (WITH RECURSIVE up(id) AS (
   SELECT u.oid FROM pg_catalog.pg_roles u WHERE u.rolname IN (current_user,session_user) AND NOT u.rolsuper
   UNION SELECT m.roleid FROM pg_catalog.pg_auth_members m JOIN up ON m.member=up.id
   WHERE m.inherit_option OR m.set_option)
  SELECT FROM up JOIN pg_catalog.pg_roles g ON g.oid=up.id WHERE g.rolname='session_holder')
  OR (account IN (SELECT b.account FROM public.session_holder_accounts b WHERE b.role_name=current_user)
   AND account=ALL(SELECT b.account FROM public.session_holder_accounts b WHERE b.role_name IN (current_user,session_user))))
 WITH CHECK (NOT EXISTS (WITH RECURSIVE up(id) AS (
   SELECT u.oid FROM pg_catalog.pg_roles u WHERE u.rolname IN (current_user,session_user) AND NOT u.rolsuper
   UNION SELECT m.roleid FROM pg_catalog.pg_auth_members m JOIN up ON m.member=up.id
   WHERE m.inherit_option OR m.set_option)
  SELECT FROM up JOIN pg_catalog.pg_roles g ON g.oid=up.id WHERE g.rolname='session_holder')
  OR (account IN (SELECT b.account FROM public.session_holder_accounts b WHERE b.role_name=current_user)
   AND account=ALL(SELECT b.account FROM public.session_holder_accounts b WHERE b.role_name IN (current_user,session_user))));
