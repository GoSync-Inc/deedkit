-- rollout: manual
-- ANYM-447: finished hints and closed definitions age out after 30 days, keeping counters and a tombstone.
-- closed_at is the closure time: a trigger sets it on every transition to closed, expired or access_changed, so a
-- runtime of schema 61 that closes a definition during the rolling release sets it too, and clears it when a
-- definition is not terminal. Definitions already terminal get the migration time (backfill), so nothing is deleted
-- earlier than 30 days after this migration. deleted_hint_counts keeps get_subscription's per-state hint counts for
-- the outbox rows the worker deletes (hints_deleted_at: its last deletion); hints_deleted_through is the highest
-- position of the events hints it deleted, which bounds the range of the next kept one. tombstoned_at marks a definition whose
-- matches, receipts, outbox rows and consumption were deleted; its floor is then its matched_through. No index: the
-- worker's probes use the outbox's (subscription,...) unique key and the small subscriptions table. Manual: the
-- backfill writes an existing table and the trigger is new on it.
SET LOCAL lock_timeout='2s';
ALTER TABLE subscriptions ADD COLUMN closed_at timestamptz;
ALTER TABLE subscriptions ADD COLUMN tombstoned_at timestamptz;
ALTER TABLE subscriptions ADD COLUMN deleted_hint_counts jsonb NOT NULL DEFAULT '{}'
 CHECK(jsonb_typeof(deleted_hint_counts)='object' AND octet_length(deleted_hint_counts::text)<=512);
ALTER TABLE subscriptions ADD COLUMN hints_deleted_at timestamptz;
ALTER TABLE subscriptions ADD COLUMN hints_deleted_through bigint NOT NULL DEFAULT 0 CHECK(hints_deleted_through>=0);
CREATE FUNCTION public.subscription_closed_at() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.state IN ('closed','expired','access_changed') THEN
  -- An explicit value wins (quarantine restore, operator release); a later terminal change keeps the first.
  IF NEW.closed_at IS NULL THEN NEW.closed_at:=clock_timestamp(); END IF;
 ELSE
  NEW.closed_at:=NULL;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.subscription_closed_at() FROM PUBLIC;
CREATE TRIGGER subscription_closed_at BEFORE INSERT OR UPDATE OF state,closed_at ON subscriptions
 FOR EACH ROW EXECUTE FUNCTION public.subscription_closed_at();
UPDATE subscriptions SET closed_at=clock_timestamp() WHERE state IN ('closed','expired','access_changed');
-- A quarantine before schema 40 left no hold (SUBSCRIPTIONS.md "Event retention"). quarantine_restore runs only in a
-- *_restore_test database, so there every closed definition is held until the operator's release, as a quarantine
-- of this schema holds it.
UPDATE subscriptions SET retention_hold=true WHERE state='closed' AND current_database() LIKE '%\_restore\_test';
SET LOCAL lock_timeout=DEFAULT;

-- The worker deletes finished hints (keeping their counts) and a closed definition's matches, receipts, outbox rows
-- and consumption, then marks the tombstone. The trigger needs no column grant.
DO $$ BEGIN
 IF EXISTS(SELECT FROM pg_roles WHERE rolname='messaging_subscriptions') THEN
  GRANT UPDATE(tombstoned_at,deleted_hint_counts,hints_deleted_at,hints_deleted_through) ON subscriptions TO messaging_subscriptions;
  GRANT DELETE ON subscription_outbox,subscription_consumption TO messaging_subscriptions;
  GRANT SELECT(subscription),DELETE ON subscription_receipts TO messaging_subscriptions;
 END IF;
END $$;
