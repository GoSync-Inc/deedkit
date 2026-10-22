-- rollout: manual
-- Read-state freshness (ANYM-275): continuity evidence for the collector's receiving period
-- and passive WhatsApp read sources. Additive: a schema-41 runtime refuses this schema anyway,
-- the new columns are nullable and the new table is written only by schema-42 collectors.
-- Manual only because of the grants below. A short lock wait fails the upgrade instead of
-- stalling requests (their statement timeout is 5 s).
SET LOCAL lock_timeout='2s';
-- One receiving period per account: it starts when the provider connection is ready under
-- a lease epoch, a drained capture spool refreshes its heartbeat, an undrained one marks the
-- backlog and a provider update gap is remembered until the next period starts.
ALTER TABLE collector_health
 ADD COLUMN receiving_epoch bigint,
 ADD COLUMN receiving_since timestamptz,
 ADD COLUMN receiving_heartbeat_at timestamptz,
 ADD COLUMN receiving_backlog_since timestamptz,
 ADD COLUMN receiving_gap_at timestamptz;
-- Each read source keeps its own basis. A count is reported only from a provider count or a
-- whole-chat baseline; count_basis says why a row has no count.
ALTER TABLE chat_read_observations DROP CONSTRAINT chat_read_observations_basis_check;
ALTER TABLE chat_read_observations ADD CONSTRAINT chat_read_observations_basis_check CHECK(basis IN (
 'telegram_dialog','whatsapp_cached_chat','whatsapp_app_state_read','whatsapp_owner_receipt','whatsapp_history_sync'));
ALTER TABLE chat_read_observations
 ADD COLUMN marked_unread boolean,
 ADD COLUMN baseline_at timestamptz,
 ADD COLUMN count_basis text CONSTRAINT chat_read_observations_count_basis_check
  CHECK(count_basis IN ('provider','partial','stale_since_baseline'));
-- Per-message owner-read evidence from the owner's own-device receipts: exactly the listed
-- messages, never a watermark for earlier or unlisted ones.
CREATE TABLE owner_message_reads (
 chat text NOT NULL,
 account text NOT NULL,
 provider_id text NOT NULL CHECK(length(provider_id) BETWEEN 1 AND 256),
 read_at timestamptz NOT NULL,
 basis text NOT NULL CHECK(basis IN ('whatsapp_owner_receipt')),
 PRIMARY KEY(chat,provider_id),
 FOREIGN KEY(chat,account) REFERENCES chats(id,account) ON DELETE CASCADE
);
CREATE TRIGGER collector_account_guard BEFORE INSERT OR UPDATE OR DELETE ON public.owner_message_reads
 FOR EACH ROW EXECUTE FUNCTION public.collector_account_guard('account');
DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_collector') THEN
  GRANT SELECT,INSERT,UPDATE ON owner_message_reads TO messaging_collector;
 END IF;
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_gateway') THEN
  GRANT SELECT ON owner_message_reads TO messaging_gateway;
 END IF;
END $$;
