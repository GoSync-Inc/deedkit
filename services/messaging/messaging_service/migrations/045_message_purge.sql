-- rollout: expand
-- Messenger deletion purge (ANYM-511). New tables carry no foreign keys (044 precedent: a key locks
-- the referenced table at create). messages gains a defaulted flag (no rewrite; a brief lock under the
-- 2 s migrate bound). events gains an index on message: its build holds SHARE on events, so event
-- inserts wait for it (about 250k rows in production). Guard triggers keep every later write of a
-- purged message content-free, so deployed schema-44 code keeps writing through them unchanged.
ALTER TABLE messages ADD COLUMN purged boolean NOT NULL DEFAULT false;
CREATE INDEX events_message ON events(message);
-- One row per deleted message: pending until the worker clears it, then the content-free mark.
CREATE TABLE message_purges (
 message text PRIMARY KEY,
 account text NOT NULL,
 provider_chat text NOT NULL,
 provider_id text NOT NULL,
 reason text NOT NULL CHECK(reason IN ('deleted_for_everyone','deleted_for_me','chat_deleted','chat_cleared',
  'telegram_deleted','existing_deleted')),
 observed_at timestamptz NOT NULL,
 purged_at timestamptz,
 counts jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(counts)='object'),
 attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
 failure_code text CHECK(failure_code ~ '^[a-z_]{1,64}$'),
 next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX message_purges_pending ON message_purges(account,next_attempt_at) WHERE purged_at IS NULL;
CREATE INDEX message_purges_provider ON message_purges(account,provider_chat,provider_id) WHERE purged_at IS NOT NULL;
-- A whole-chat deletion or history clear (stories ANYM-514, ANYM-515): a cutoff, never content.
CREATE TABLE chat_tombstones (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 account text NOT NULL,
 provider_chat text NOT NULL,
 cutoff_at timestamptz,
 cutoff_id text,
 reason text NOT NULL CHECK(reason IN ('chat_deleted','chat_cleared')),
 observed_at timestamptz NOT NULL,
 due_at timestamptz NOT NULL,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','applied','cancelled')),
 CHECK((cutoff_at IS NULL)<>(cutoff_id IS NULL))
);
CREATE INDEX chat_tombstones_due ON chat_tombstones(account,due_at) WHERE state='pending';
-- ingest_receipts has no index by message; the purge finds a message's receipts through this table.
-- The guard trigger fills it for every new receipt; the worker backfills older receipts once.
CREATE TABLE ingest_receipt_links (
 digest text PRIMARY KEY,
 account text NOT NULL,
 provider_chat text NOT NULL,
 provider_id text NOT NULL
);
CREATE INDEX ingest_receipt_links_message ON ingest_receipt_links(account,provider_chat,provider_id);
-- Worker state. The event cursor starts at the current feed head: older deletions are the
-- operator's purge-deleted backlog (reason existing_deleted). Ships enabled (owner 2026-10-19).
CREATE TABLE purge_state (
 name text PRIMARY KEY CHECK(name IN ('enabled','event_cursor','receipt_cursor','receipt_backfill')),
 value text NOT NULL
);
INSERT INTO purge_state(name,value) SELECT 'enabled','true' UNION ALL
 SELECT 'event_cursor',coalesce(max(id),0)::text FROM events UNION ALL
 SELECT 'receipt_cursor','' UNION ALL SELECT 'receipt_backfill','pending';

-- The content-free envelope: identifiers, times and file sizes stay; text, names and metadata go.
CREATE FUNCTION public.purged_envelope(e jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
 SELECT jsonb_strip_nulls(jsonb_build_object(
  'account',e->'account','platform',e->'platform','chat',e->'chat','provider_chat',e->'provider_chat',
  'chat_title',e->'chat_title','provider_id',e->'provider_id','sender',e->'sender',
  'sender_profile',CASE WHEN jsonb_typeof(e->'sender_profile')='object' THEN jsonb_build_object(
   'display_name',e->'sender_profile'->'display_name','username',e->'sender_profile'->'username') END,
  'sent_at',e->'sent_at','updated_at',e->'updated_at','direction',e->'direction','origin',e->'origin',
  'message',e->'message','revision',e->'revision',
  'media',CASE WHEN jsonb_typeof(e->'media')='array' THEN (SELECT coalesce(jsonb_agg(jsonb_build_object(
   'provider_id',m->'provider_id','mime',m->'mime','bytes',m->'bytes')),'[]'::jsonb)
   FROM jsonb_array_elements(e->'media') m) END))
  || '{"body":"","deleted":true,"purged":true}'::jsonb
$$;
-- Live-search hits naming a purged message keep their identifiers and lose their text.
CREATE FUNCTION public.purged_command_hits(acc text,result jsonb) RETURNS jsonb LANGUAGE sql STABLE AS $$
 SELECT CASE WHEN jsonb_typeof(result->'hits')<>'array' THEN result ELSE jsonb_set(result,'{hits}',
  (SELECT coalesce(jsonb_agg(CASE WHEN jsonb_typeof(h)='object' AND EXISTS(SELECT 1 FROM public.messages m
    WHERE m.account=acc AND m.provider_id=h->>'provider_id' AND m.chat=h->>'chat' AND m.purged)
   THEN h||'{"text":"","purged":true}'::jsonb ELSE h END ORDER BY n),'[]'::jsonb)
   FROM jsonb_array_elements(result->'hits') WITH ORDINALITY AS x(h,n))) END
$$;
-- Every finisher publishes through this guard: a write naming a purged message is stored
-- content-free or dropped, so work leased before the purge cannot bring content back.
CREATE FUNCTION public.purge_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $$
DECLARE target text; gone boolean;
BEGIN
 IF TG_TABLE_NAME='ingest_receipts' THEN
  INSERT INTO public.ingest_receipt_links(digest,account,provider_chat,provider_id)
   VALUES(NEW.digest,NEW.account,coalesce(NEW.envelope->>'chat',''),coalesce(NEW.envelope->>'provider_id',''))
   ON CONFLICT(digest) DO NOTHING;
  IF EXISTS(SELECT 1 FROM public.message_purges p WHERE p.account=NEW.account
    AND p.provider_chat=NEW.envelope->>'chat' AND p.provider_id=NEW.envelope->>'provider_id'
    AND p.purged_at IS NOT NULL) THEN
   NEW.envelope := public.purged_envelope(NEW.envelope);
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME IN ('attachments','message_revisions','events','message_metadata','messenger_commands') THEN
  target := NEW.message;
 ELSE
  SELECT a.message INTO target FROM public.attachments a WHERE a.id=NEW.attachment;
 END IF;
 IF TG_TABLE_NAME='messenger_commands' AND NEW.result IS NOT NULL THEN
  NEW.result := public.purged_command_hits(NEW.account,NEW.result);
 END IF;
 IF target IS NULL THEN RETURN NEW; END IF;
 SELECT m.purged INTO gone FROM public.messages m WHERE m.id=target;
 IF NOT coalesce(gone,false) THEN RETURN NEW; END IF;
 IF TG_TABLE_NAME='attachments' THEN NEW.filename := '';
 ELSIF TG_TABLE_NAME IN ('message_revisions','events') THEN NEW.snapshot := public.purged_envelope(NEW.snapshot);
 ELSIF TG_TABLE_NAME='messenger_commands' THEN
  IF NEW.result IS NOT NULL THEN NEW.result := '{"purged":true}'::jsonb; END IF;
 ELSE RETURN NULL;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER purge_guard BEFORE INSERT ON ingest_receipts FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT OR UPDATE ON attachments FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT ON message_revisions FOR EACH ROW EXECUTE FUNCTION purge_guard();
-- Named to fire after snapshot_event_identity, which adds metadata to the snapshot.
CREATE TRIGGER z_purge_guard BEFORE INSERT ON events FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT OR UPDATE ON message_metadata FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE UPDATE OF result ON messenger_commands FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT ON attachment_text FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT ON attachment_text_chunks FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT ON attachment_enrichment FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT ON attachment_enrichment_chunks FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT ON attachment_understanding FOR EACH ROW EXECUTE FUNCTION purge_guard();
REVOKE ALL ON FUNCTION public.purge_guard() FROM PUBLIC;

-- The audit CHECK stays NOT VALID as 036 left it (see 039).
ALTER TABLE audit_log DROP CONSTRAINT audit_log_operation_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_operation_check CHECK(operation IN (
 'capabilities','list_chats','search','get_messages','get_context','get_updates',
 'get_attachment','download','ack_updates','issue_key','revoke_key','set_access','http_boundary',
 'get_reviews','record_review','search_people','search_files','get_file_text',
 'get_file_occurrences','get_file_segments','get_sync_status','resolve_reference',
 'send_message','get_send_status','send_dispatch','set_sending','preview_message',
 'set_key_permissions','set_operations','list_messenger_operations','preview_messenger_operation',
 'execute_messenger_operation','get_messenger_operation','messenger_dispatch',
 'create_subscription','list_subscriptions','get_subscription','update_subscription','get_subscription_events',
 'dismiss_reply','prepare_file_upload','upload_file_chunk','get_file_upload','request_file_download',
 'fetch_subscription_events','ack_subscription_events','wait_for_subscription_events',
 'configure_subscription_delivery','test_subscription','submit_feedback','list_feedback',
 'extend_key','set_feedback_status','snooze_reply','restore_reply','mute_reply_source',
 'unmute_reply_source','list_reply_dismissals','message_purge')) NOT VALID;
