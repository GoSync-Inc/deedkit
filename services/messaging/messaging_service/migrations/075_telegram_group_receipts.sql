-- rollout: manual
-- ANYM-868: a Telegram group or supergroup outbox boundary is an aggregate read (someone in the group read up to
-- max_id, never who). The Telegram branch of the row CHECK of 023 also allows audience provider_aggregate, for a
-- marked group chat ID only (a negative number); every other branch is unchanged, so the rule only widens.
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": a CHECK swap; no row is read,
-- rewritten or deleted). The new CHECK is NOT VALID, so PostgreSQL scans no rows and the ACCESS EXCLUSIVE lock
-- covers only the catalog change; old rows satisfy it by construction. No VALIDATE runs in this release: the
-- runner commits all pending files at once (store.py migrate_expand), so a VALIDATE in a later file would hold the
-- same lock for the scan. New writes and updates are checked by the new rule at once.
-- Rollback is application-only and keeps this schema: a schema-71 runtime never writes an aggregate Telegram row,
-- and its reads (receipts.py ROW_PROOFS_SQL, REFERENCE_TG_SQL) do not filter on audience.
-- One ALTER TABLE takes the lock once. IF EXISTS on both names keeps a rerun over a rewound ledger idempotent.
SET LOCAL lock_timeout='2s';
ALTER TABLE message_receipts DROP CONSTRAINT IF EXISTS message_receipts_check,
    DROP CONSTRAINT IF EXISTS message_receipts_source_check,
    ADD CONSTRAINT message_receipts_source_check CHECK(
        (platform='telegram' AND provider_id IS NULL AND max_id IS NOT NULL AND max_id>0
            AND state='read' AND basis='telegram_outbox_boundary'
            AND (audience='private_peer' OR (audience='provider_aggregate' AND provider_chat ~ '^-[1-9][0-9]{0,18}$')))
        OR (platform='whatsapp' AND provider_id IS NOT NULL AND max_id IS NULL
            AND basis='whatsapp_message_ack')) NOT VALID;
