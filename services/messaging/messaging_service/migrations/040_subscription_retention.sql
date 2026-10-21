-- Acknowledged subscription event retention (ANYM-446): a retained floor per subscription and a
-- durable quarantine hold. Expand-only: older code never reads the new columns, and floor 0
-- with no hold is the behavior before retention. Pruning uses the (subscription,event) primary
-- key for both the oldest-reference probe and the range delete, so no new index is needed.
-- Rollout: a verified backup, this migration, the release with the floor-aware reader and worker,
-- then `set-subscription-retention --enable` (probe_metadata) starts pruning.
SET LOCAL lock_timeout='2s';
-- The highest pruned position; 0 when nothing was pruned. Readers below it get retention_gap.
ALTER TABLE subscriptions ADD COLUMN retention_floor bigint NOT NULL DEFAULT 0 CHECK(retention_floor>=0);
-- Set by quarantine restore on every definition it closes, cleared only by an operator release.
ALTER TABLE subscriptions ADD COLUMN retention_hold boolean NOT NULL DEFAULT false;
SET LOCAL lock_timeout=DEFAULT;

-- The worker reads the operator's activation, bounds deletion by the consumer's acked_through
-- and moves the floor with it.
DO $$ BEGIN
 IF EXISTS(SELECT FROM pg_roles WHERE rolname='messaging_subscriptions') THEN
  GRANT SELECT ON probe_metadata TO messaging_subscriptions;
  GRANT SELECT ON subscription_consumption TO messaging_subscriptions;
  GRANT UPDATE(retention_floor) ON subscriptions TO messaging_subscriptions;
  GRANT DELETE ON subscription_matches TO messaging_subscriptions;
 END IF;
END $$;
