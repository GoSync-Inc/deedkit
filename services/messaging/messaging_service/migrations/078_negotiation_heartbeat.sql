-- rollout: manual
-- ANYM-908: the negotiation worker reports its version in component_heartbeats. The component CHECK of 050 gains
-- 'negotiations', and row-level security keeps messaging_negotiations on that one row, as 050 does for each
-- component. The role exists since 076; its grants on this table come with negotiation_role.sql.
-- Manual (docs/operations/production.md "Migration releases" item 4): a constraint replaced and a policy created on
-- an existing table, released by hand at once together with negotiation_role.sql.
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": no row is deleted or rewritten and
-- no column dropped or retyped; the wider CHECK accepts every existing row). lock_timeout bounds the brief
-- ACCESS EXCLUSIVE lock on the four-row table; only the heartbeat writers touch it.
SET LOCAL lock_timeout='2s';
ALTER TABLE component_heartbeats DROP CONSTRAINT component_heartbeats_component_check;
ALTER TABLE component_heartbeats ADD CONSTRAINT component_heartbeats_component_check
 CHECK(component IN ('api','matcher','dispatcher','negotiations'));
DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_negotiations') AND NOT EXISTS (SELECT FROM pg_policies
   WHERE tablename='component_heartbeats' AND policyname='messaging_negotiations_beat') THEN
  CREATE POLICY messaging_negotiations_beat ON component_heartbeats TO messaging_negotiations
   USING (component='negotiations') WITH CHECK (component='negotiations');
 END IF;
END $$;
