-- rollout: manual
-- Per-host hint budget (ANYM-585): the dispatcher counts endpoint hints admitted to one host in the rolling hour
-- across every key. The outbox is never pruned, so the count reads this index from the hour's start and never the
-- history. Additive: older runtimes never use it. Manual only because it indexes an existing table; production's
-- outbox held 0 rows on 2026-10-28, so the build inside the migration transaction is milliseconds.
SET LOCAL lock_timeout='2s';
CREATE INDEX subscription_hint_host_admission ON subscription_outbox(admitted_at)
 WHERE endpoint IS NOT NULL AND admitted_at IS NOT NULL;
SET LOCAL lock_timeout=DEFAULT;
