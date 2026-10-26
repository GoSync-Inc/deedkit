-- rollout: manual
-- Manual: it seeds worker state, adds guard functions and triggers and re-adds the audit CHECK, so the
-- release is the standing manual one (backup gate first); every change still adds, nothing drops data.
-- Messenger deletion purge (ANYM-511). New tables carry no foreign keys (044 precedent: a key locks
-- the referenced table at create).
-- Locks: every table this file alters or adds a trigger to is locked first, all in one attempt that
-- never queues (NOWAIT), retried for at most 2 s, as lock_replay_tables does (store.py). Waits one
-- table at a time would each stay under the 2 s lock bound while the locks already held kept writers
-- waiting for their sum. Once the locks are held every later statement only changes the catalog.
-- The events index is built before this file, outside its transaction (`CREATE INDEX CONCURRENTLY IF
-- NOT EXISTS events_message ON events(message)`, release step 0); this file checks that the index is
-- valid, ready and on events(message). Only a small database (an exact bounded count and size, not
-- the planner's estimate) builds it here, inside the locked phase.
-- Guard triggers keep every later write of a purged message content-free, so deployed schema-44 code
-- keeps writing through them unchanged.
DO $$ DECLARE deadline timestamptz := clock_timestamp()+interval '2 seconds'; BEGIN
 LOOP
  BEGIN
   LOCK TABLE messages,chats,audit_log IN ACCESS EXCLUSIVE MODE NOWAIT;
   LOCK TABLE events,ingest_receipts,attachments,message_revisions,message_metadata,attachment_text,
    attachment_text_chunks,attachment_enrichment,attachment_enrichment_chunks,attachment_understanding,
    reviews,messenger_commands,outgoing_messages IN SHARE ROW EXCLUSIVE MODE NOWAIT;
   EXIT;
  EXCEPTION WHEN lock_not_available THEN
   IF clock_timestamp()>=deadline THEN RAISE; END IF;
   PERFORM pg_sleep(0.05);
  END;
 END LOOP;
END $$;
DO $$ DECLARE ix regclass := to_regclass('public.events_message'); ok boolean; BEGIN
 IF ix IS NULL THEN
  IF pg_relation_size('public.events')>8*1024*1024 THEN
   RAISE EXCEPTION 'events_message missing: run CREATE INDEX CONCURRENTLY IF NOT EXISTS events_message ON events(message) first';
  END IF;
  IF (SELECT count(*) FROM (SELECT 1 FROM events LIMIT 10001) s)>10000 THEN
   RAISE EXCEPTION 'events_message missing: run CREATE INDEX CONCURRENTLY IF NOT EXISTS events_message ON events(message) first';
  END IF;
  CREATE INDEX events_message ON events(message);
 ELSE
  SELECT i.indisvalid AND i.indisready AND i.indrelid='public.events'::regclass AND i.indpred IS NULL
    AND i.indexprs IS NULL AND i.indnkeyatts=1 AND i.indkey[0]=a.attnum AND am.amname='btree'
    -- The purge's `message=ANY(...)` uses the column's collation and the type's default equality.
    AND i.indcollation[0]=a.attcollation AND EXISTS(SELECT 1 FROM pg_opclass o WHERE o.oid=i.indclass[0]
     AND o.opcmethod=am.oid AND o.opcintype=a.atttypid AND o.opcdefault) INTO ok
   FROM pg_index i JOIN pg_class ic ON ic.oid=i.indexrelid JOIN pg_am am ON am.oid=ic.relam
   JOIN pg_attribute a ON a.attrelid='public.events'::regclass AND a.attname='message'
   WHERE i.indexrelid=ix;
  -- An interrupted concurrent build leaves an INVALID index that IF NOT EXISTS then skips.
  IF NOT coalesce(ok,false) THEN
   RAISE EXCEPTION 'events_message is not a valid btree index on events(message): DROP INDEX CONCURRENTLY events_message, then CREATE INDEX CONCURRENTLY events_message ON events(message)';
  END IF;
 END IF;
END $$;
ALTER TABLE messages ADD COLUMN purged boolean NOT NULL DEFAULT false;
-- The chat visibility probe (is any message of this chat left unpurged?) reads this index, however
-- long the chat's purged history. It needs the new column, so production builds it right after
-- `migrate`, CONCURRENTLY (release step 2); only a small database builds it here, in the locked phase.
-- Without it a slow probe leaves the chat to the worker's hide retry (message_purges.hide_pending).
DO $$ BEGIN
 IF pg_relation_size('public.messages')<=8*1024*1024
   AND (SELECT count(*) FROM (SELECT 1 FROM messages LIMIT 10001) s)<=10000 THEN
  CREATE INDEX messages_unpurged ON messages(account,chat) WHERE NOT purged;
 END IF;
END $$;
-- Set by the worker when a chat's last unpurged message is purged; the listing hides the chat while
-- it is set, and the next archived message of the chat clears it (purge_chat_reopen below).
ALTER TABLE chats ADD COLUMN purged_at timestamptz;
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
 next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 -- Purged, but its chat's visibility probe ran out of time: the worker retries the hide.
 hide_pending boolean NOT NULL DEFAULT false
);
CREATE INDEX message_purges_pending ON message_purges(account,next_attempt_at) WHERE purged_at IS NULL;
CREATE INDEX message_purges_hide ON message_purges(account) WHERE hide_pending;
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
-- The guard trigger fills it for every new receipt; the worker backfills older receipts once and
-- scrubs, chunk by chunk, those of messages purged before their link existed.
CREATE TABLE ingest_receipt_links (
 digest text PRIMARY KEY,
 account text NOT NULL,
 provider_chat text NOT NULL,
 provider_id text NOT NULL
);
CREATE INDEX ingest_receipt_links_message ON ingest_receipt_links(account,provider_chat,provider_id);
-- Worker state. The event cursor starts at the current feed head: older deletions are the
-- operator's purge-deleted backlog (reason existing_deleted). Ships enabled (ANYM-511 Accepted line).
CREATE TABLE purge_state (
 name text PRIMARY KEY CHECK(name IN ('enabled','event_cursor','receipt_cursor','receipt_backfill')),
 value text NOT NULL
);
INSERT INTO purge_state(name,value) SELECT 'enabled','true' UNION ALL
 SELECT 'event_cursor',coalesce(max(id),0)::text FROM events UNION ALL
 SELECT 'receipt_cursor','' UNION ALL SELECT 'receipt_backfill','pending';

-- The content-free envelope: identifiers, times, file sizes, delivery state and file processing
-- state stay; text, names and other metadata go.
CREATE FUNCTION public.purged_envelope(e jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
 SELECT jsonb_strip_nulls(jsonb_build_object(
  'account',e->'account','platform',e->'platform','chat',e->'chat','provider_chat',e->'provider_chat',
  'chat_title',e->'chat_title','provider_id',e->'provider_id','sender',e->'sender',
  'sender_profile',CASE WHEN jsonb_typeof(e->'sender_profile')='object' THEN jsonb_build_object(
   'display_name',e->'sender_profile'->'display_name','username',e->'sender_profile'->'username') END,
  'sent_at',e->'sent_at','updated_at',e->'updated_at','direction',e->'direction','origin',e->'origin',
  'message',e->'message','revision',e->'revision','delivery_receipt',e->'delivery_receipt',
  'metadata',CASE WHEN jsonb_typeof(e#>'{metadata,processing}')='object' THEN
   jsonb_build_object('processing',e#>'{metadata,processing}') END,
  'media',CASE WHEN jsonb_typeof(e->'media')='array' THEN (SELECT coalesce(jsonb_agg(jsonb_build_object(
   'provider_id',m->'provider_id','mime',m->'mime','bytes',m->'bytes')),'[]'::jsonb)
   FROM jsonb_array_elements(e->'media') m) END))
  || '{"body":"","deleted":true,"purged":true}'::jsonb
$$;
-- A purged message's command keeps only what routes, authorizes and correlates it: request ID and
-- status (columns), the authorization binding, chat, message, provider and file identifiers
-- (commands._check_saved reads them). Every other field is content (text, captions, file names,
-- coordinates, contact names ...) and goes, whatever its name: an allowlist, not a denylist.
CREATE FUNCTION public.purged_fields(x jsonb,keep text[]) RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
 SELECT CASE WHEN jsonb_typeof(x) IS DISTINCT FROM 'object' THEN x ELSE (SELECT coalesce(jsonb_object_agg(k,CASE
   WHEN k=ANY(keep) THEN v
   WHEN jsonb_typeof(v)='string' THEN '"[deleted]"'::jsonb
   WHEN jsonb_typeof(v)='array' THEN (SELECT coalesce(jsonb_agg('"[deleted]"'::jsonb),'[]'::jsonb) FROM jsonb_array_elements(v))
   ELSE 'null'::jsonb END),'{}'::jsonb) FROM jsonb_each(x) AS p(k,v)) END
$$;
CREATE FUNCTION public.purged_command_parameters(x jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
 SELECT public.purged_fields(x,ARRAY['around_message','attachment_id','before_message','chat_types','contact_ids',
  'count','create','enabled','from_time','label_ids','limit','mode','multiple','offset','option_indices','seconds',
  'source_message_id','source_revision','to_time','upload_id','user_id','user_ids'])
$$;
CREATE FUNCTION public.purged_command_payload(x jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
 SELECT public.purged_fields(x,ARRAY['platform','catalog_version','operation','account','adapter',
   'authorization_binding','chat_id','message_id','expected_revision','provider_chat','provider_message_id',
   'source_chat_id','source_provider_chat','source_provider_message','file_upload_id','file_message_id',
   'file_message_revision','file_chat_id','file_digest','file_bytes','file_mime'])
  ||jsonb_build_object('parameters',public.purged_command_parameters(x->'parameters'))
  ||CASE WHEN jsonb_typeof(x->'request_input')='object' THEN jsonb_build_object('request_input',
   public.purged_fields(x->'request_input',ARRAY['account','operation','chat_id','message_id','expected_revision',
    'preview_digest'])||jsonb_build_object('parameters',public.purged_command_parameters(x#>'{request_input,parameters}')))
   ELSE '{}'::jsonb END
$$;
-- The result keeps its outcome and the provider identifiers of the message it produced.
CREATE FUNCTION public.purged_command_result(r jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
 SELECT CASE WHEN r IS NULL THEN NULL ELSE jsonb_strip_nulls(jsonb_build_object('outcome',r->'outcome',
  'provider_message_id',r->'provider_message_id',
  'data',CASE WHEN jsonb_typeof(r#>'{data,archive_reference}')='object' THEN
   jsonb_build_object('archive_reference',r#>'{data,archive_reference}') END))||'{"purged":true}'::jsonb END
$$;
-- Live-search hits naming a purged message keep their identifiers and lose their text. A stored
-- receipt keeps them under data.hits (commands.py:70 safe_result, telegram_operations.py:1283,1397).
-- VOLATILE: called from the guard after its lock wait, it must read the purge that just committed.
CREATE FUNCTION public.purged_command_hits(acc text,result jsonb) RETURNS jsonb LANGUAGE sql VOLATILE AS $$
 SELECT CASE WHEN jsonb_typeof(result#>'{data,hits}') IS DISTINCT FROM 'array' THEN result ELSE jsonb_set(result,'{data,hits}',
  (SELECT coalesce(jsonb_agg(CASE WHEN jsonb_typeof(h)='object' AND EXISTS(SELECT 1 FROM public.messages m
    WHERE m.account=acc AND m.provider_id=h->>'provider_id' AND m.chat=h->>'chat' AND m.purged)
   THEN h||'{"text":"","purged":true}'::jsonb ELSE h END ORDER BY n),'[]'::jsonb)
   FROM jsonb_array_elements(result#>'{data,hits}') WITH ORDINALITY AS x(h,n))) END
$$;
-- Content finishers publish through this guard: a write naming a purged message is stored
-- content-free or dropped, so work leased before the purge cannot bring content back. Their
-- writers either hold `archive:<account>` (ingest) or a lease the purge revokes, so they serialize
-- with the purge transaction.
CREATE FUNCTION public.purge_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $$
DECLARE target text; gone boolean;
BEGIN
 IF TG_TABLE_NAME='ingest_receipts' THEN
  -- A partial restore may lack the link table until migrate replays it; the receipt still lands.
  IF to_regclass('public.ingest_receipt_links') IS NOT NULL THEN
   INSERT INTO public.ingest_receipt_links(digest,account,provider_chat,provider_id)
    VALUES(NEW.digest,NEW.account,coalesce(NEW.envelope->>'chat',''),coalesce(NEW.envelope->>'provider_id',''))
    ON CONFLICT(digest) DO NOTHING;
  END IF;
  -- The live flag decides: a mark left by a removed account never scrubs a new message.
  IF EXISTS(SELECT 1 FROM public.message_purges p JOIN public.messages m ON m.id=p.message
    WHERE p.account=NEW.account AND p.provider_chat=NEW.envelope->>'chat'
    AND p.provider_id=NEW.envelope->>'provider_id' AND p.purged_at IS NOT NULL AND m.purged) THEN
   NEW.envelope := public.purged_envelope(NEW.envelope);
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME IN ('attachments','message_revisions','events','message_metadata') THEN
  target := NEW.message;
 ELSE
  SELECT a.message INTO target FROM public.attachments a WHERE a.id=NEW.attachment;
 END IF;
 IF target IS NULL THEN RETURN NEW; END IF;
 SELECT m.purged INTO gone FROM public.messages m WHERE m.id=target;
 IF NOT coalesce(gone,false) THEN RETURN NEW; END IF;
 IF TG_TABLE_NAME='attachments' THEN NEW.filename := '';
 ELSIF TG_TABLE_NAME IN ('message_revisions','events') THEN NEW.snapshot := public.purged_envelope(NEW.snapshot);
 ELSE RETURN NULL;
 END IF;
 RETURN NEW;
END $$;
-- Publications that hold no archive lock or lease the purge revokes: a review, a command (its
-- request, its result, the output it produced) and a send's provider receipt. A write that can newly
-- name a purged message first waits for any purge of its account (the purge holds `purge:<account>`
-- exclusively, at most its 3 s transaction bound) and then checks with a fresh snapshot, so a
-- publication either commits before the purge reads it or sees the purge. Rows the purge already
-- matches need no wait: its UPDATE waits for their row lock and rewrites the committed value.
CREATE FUNCTION public.purge_publication_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $$
DECLARE acc text; gone boolean;
BEGIN
 IF TG_TABLE_NAME='reviews' THEN
  SELECT m.account INTO acc FROM public.messages m WHERE m.id=NEW.message;
  IF acc IS NULL THEN RETURN NEW; END IF;
  PERFORM pg_advisory_xact_lock_shared(hashtextextended('purge:'||acc,0));
  IF EXISTS(SELECT 1 FROM public.messages m WHERE m.id=NEW.message AND m.purged) THEN
   NEW.result_ref := '[deleted]';
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='outgoing_messages' THEN
  IF NEW.provider_message_id IS NOT DISTINCT FROM OLD.provider_message_id
    AND NEW.archive_reference IS NOT DISTINCT FROM OLD.archive_reference THEN RETURN NEW; END IF;
  PERFORM pg_advisory_xact_lock_shared(hashtextextended('purge:'||NEW.account,0));
  IF EXISTS(SELECT 1 FROM public.message_purges p JOIN public.messages m ON m.id=p.message
    WHERE p.account=NEW.account AND p.purged_at IS NOT NULL AND m.purged AND CASE WHEN NEW.archive_reference IS NULL
     THEN p.provider_chat=NEW.provider_chat AND p.provider_id=NEW.provider_message_id
     ELSE p.provider_chat=NEW.archive_reference->>'provider_chat_id'
      AND p.provider_id=NEW.archive_reference->>'provider_message_id' END) THEN
   NEW.body := '[deleted]';
   NEW.source_text := CASE WHEN NEW.source_text IS NULL THEN NULL ELSE '[deleted]' END;
   NEW.render_artifact := CASE WHEN NEW.render_artifact IS NULL THEN NULL ELSE '{}'::jsonb END;
  END IF;
  RETURN NEW;
 END IF;
 -- messenger_commands
 -- Nested: PL/pgSQL resolves OLD.result even when the left operand of OR is true.
 IF TG_OP='INSERT' THEN
  PERFORM pg_advisory_xact_lock_shared(hashtextextended('purge:'||NEW.account,0));
 ELSIF NEW.message IS NULL THEN
  IF OLD.result IS NULL THEN
   PERFORM pg_advisory_xact_lock_shared(hashtextextended('purge:'||NEW.account,0));
  END IF;
 END IF;
 IF NEW.message IS NOT NULL THEN
  SELECT m.purged INTO gone FROM public.messages m WHERE m.id=NEW.message;
 ELSIF NEW.result IS NOT NULL THEN
  -- A command that produced a message (send_poll, send_file ...) is matched by that output.
  gone := EXISTS(SELECT 1 FROM public.message_purges p JOIN public.messages m ON m.id=p.message
   WHERE p.account=NEW.account AND p.purged_at IS NOT NULL AND m.purged AND (
    (p.provider_chat=NEW.payload->>'provider_chat' AND p.provider_id=NEW.result->>'provider_message_id') OR
    (p.provider_chat=NEW.result#>>'{data,archive_reference,provider_chat_id}'
     AND p.provider_id=NEW.result#>>'{data,archive_reference,provider_message_id}')));
 END IF;
 IF coalesce(gone,false) THEN
  NEW.payload := public.purged_command_payload(NEW.payload);
  NEW.result := public.purged_command_result(NEW.result);
 ELSIF NEW.result IS NOT NULL THEN
  NEW.result := public.purged_command_hits(NEW.account,NEW.result);
 END IF;
 RETURN NEW;
END $$;
-- The next archived message of a chat the purge hid lists it again.
CREATE FUNCTION public.purge_chat_reopen() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $$
BEGIN
 UPDATE public.chats SET purged_at=NULL WHERE id=NEW.chat AND purged_at IS NOT NULL;
 RETURN NULL;
END $$;
CREATE TRIGGER purge_guard BEFORE INSERT ON ingest_receipts FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT OR UPDATE ON attachments FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT ON message_revisions FOR EACH ROW EXECUTE FUNCTION purge_guard();
-- Named to fire after snapshot_event_identity, which adds metadata to the snapshot.
CREATE TRIGGER z_purge_guard BEFORE INSERT ON events FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT OR UPDATE ON message_metadata FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT ON attachment_text FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT ON attachment_text_chunks FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT ON attachment_enrichment FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT ON attachment_enrichment_chunks FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT ON attachment_understanding FOR EACH ROW EXECUTE FUNCTION purge_guard();
CREATE TRIGGER purge_guard BEFORE INSERT ON reviews FOR EACH ROW EXECUTE FUNCTION purge_publication_guard();
CREATE TRIGGER purge_guard BEFORE INSERT OR UPDATE OF result ON messenger_commands
 FOR EACH ROW EXECUTE FUNCTION purge_publication_guard();
CREATE TRIGGER purge_guard BEFORE UPDATE OF provider_message_id,archive_reference ON outgoing_messages
 FOR EACH ROW EXECUTE FUNCTION purge_publication_guard();
CREATE TRIGGER purge_chat_reopen AFTER INSERT ON messages FOR EACH ROW EXECUTE FUNCTION purge_chat_reopen();
REVOKE ALL ON FUNCTION public.purge_guard(),public.purge_publication_guard(),public.purge_chat_reopen() FROM PUBLIC;

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
