-- Narrow collector, indexer and enrichment writes (ANYM-254). Expand-only: deployed
-- schema-36 workers keep their table grants until the operator reapplies the new role
-- templates, which revoke direct journal writes once the new code is running.
SET LOCAL lock_timeout='2s';

-- Owner-maintained binding of a per-account collector login to its one account.
-- A role without a row keeps its table grants unchanged (shared legacy collector).
CREATE TABLE collector_account_roles (
 role_name text PRIMARY KEY,
 account text NOT NULL REFERENCES accounts(id),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Bound account of the caller-supplied role; the binding table stays private.
CREATE FUNCTION public.collector_bound_account(role_name text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT b.account FROM public.collector_account_roles b WHERE b.role_name=$1
$$;

-- Runs as the writing role. Both the current role and the authenticated login are
-- checked, so SET ROLE to an unbound role a login is a member of, or an owner function
-- called from a bound session, cannot write another account. Rows of a bound identity
-- must belong to its account before and after a write.
CREATE FUNCTION public.collector_account_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE bounds text[] := array_remove(ARRAY[public.collector_bound_account(current_user::text),
  public.collector_bound_account(session_user::text)],NULL);
 bound text; side record; pass integer;
BEGIN
 FOREACH bound IN ARRAY bounds LOOP
  FOR pass IN 1..2 LOOP
   IF pass=1 THEN
    CONTINUE WHEN TG_OP='INSERT'; side := OLD;
   ELSE
    CONTINUE WHEN TG_OP='DELETE'; side := NEW;
   END IF;
   -- Nested branches: PL/pgSQL resolves every record field named in one expression.
   IF TG_ARGV[0] IN ('account','account_message') THEN
    IF side.account IS DISTINCT FROM bound THEN
     RAISE EXCEPTION 'collector_account_mismatch' USING ERRCODE='insufficient_privilege';
    END IF;
   END IF;
   IF TG_ARGV[0] IN ('message','account_message') THEN
    IF NOT EXISTS(SELECT FROM public.messages m WHERE m.id=side.message AND m.account=bound) THEN
     RAISE EXCEPTION 'collector_account_mismatch' USING ERRCODE='insufficient_privilege';
    END IF;
   ELSIF TG_ARGV[0]='job' THEN
    IF NOT EXISTS(SELECT FROM public.media_jobs j WHERE j.id=side.job AND j.account=bound) THEN
     RAISE EXCEPTION 'collector_account_mismatch' USING ERRCODE='insufficient_privilege';
    END IF;
   END IF;
  END LOOP;
 END LOOP;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;

DO $$
DECLARE target record;
BEGIN
 FOR target IN SELECT * FROM (VALUES
  ('chats','account'),('messages','account'),('events','account_message'),
  ('collector_chats','account'),('whatsapp_checkpoints','account'),('collector_leases','account'),
  ('source_status','account'),('ingest_receipts','account'),('provider_tombstones','account'),
  ('sender_profiles','account'),('collector_health','account'),
  ('whatsapp_identity_observations','account'),('chat_read_observations','account'),
  ('message_receipts','account'),('receipt_event_jobs','account'),('outgoing_messages','account'),
  ('messenger_commands','account'),('media_jobs','account_message'),
  ('attachments','message'),('message_revisions','message'),('message_metadata','message'),
  ('message_conflicts','message'),('receipt_event_publications','message'),
  ('media_reservations','job')) AS t(name,shape)
 LOOP
  EXECUTE format('CREATE TRIGGER collector_account_guard BEFORE INSERT OR UPDATE OR DELETE ON public.%I
   FOR EACH ROW EXECUTE FUNCTION public.collector_account_guard(%L)',target.name,target.shape);
 END LOOP;
END $$;

-- Fenced readiness publication for file workers. Accepts only file processing kinds of
-- the caller's stages, for an active attachment of the current message revision, with
-- a snapshot that equals the stored message and processing values of the exact shape
-- processing_events.publish produces. The journal and counter stay owner-only.
CREATE FUNCTION public.publish_processing_event(stages text[],event_account text,event_message text,
 event_kind text,processing_key text,event_snapshot jsonb) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE m record; formatting jsonb; source_origin text; p jsonb := event_snapshot->'processing';
 existing bigint; position bigint; required text[] := ARRAY['artifact_version','attachment_id','basis',
  'source_revision','stage','state','text_chars','truncated'];
BEGIN
 IF processing_key IS NULL OR processing_key !~ '^processing_[0-9a-f]{64}$' OR event_account IS NULL THEN
  RAISE EXCEPTION 'processing_event_refused' USING ERRCODE='insufficient_privilege';
 END IF;
 -- The ingestion lock of the account, as processing_events.publish requires of its caller
 -- (re-entrant there): an edit, deletion or a same-key call waits until this commits.
 PERFORM pg_advisory_xact_lock(hashtextextended('archive:'||event_account,0));
 SELECT id INTO existing FROM public.events WHERE event_key=processing_key;
 IF FOUND THEN RETURN existing; END IF;
 SELECT * INTO m FROM public.messages WHERE id=event_message AND account=event_account AND NOT deleted
  FOR SHARE;
 -- Any unknown (NULL) comparison refuses, like a mismatch.
 IF NOT FOUND OR coalesce(jsonb_typeof(event_snapshot)<>'object' OR jsonb_typeof(p)<>'object'
  OR NOT (p->>'stage')=ANY(stages)
  OR p->>'state' NOT IN ('ready','failed')
  OR event_kind IS DISTINCT FROM CASE WHEN p->>'state'='failed' THEN 'file.processing.failed'
   ELSE CASE p->>'stage' WHEN 'text' THEN 'file.text.ready' WHEN 'transcript' THEN 'file.transcript.ready'
    WHEN 'description' THEN 'file.description.ready' END END
  -- Values, not only names: no free text can ride along in an allowed field.
  OR NOT p ?& required
  OR EXISTS(SELECT FROM jsonb_object_keys(p) k WHERE NOT k=ANY(required)
   AND k NOT IN ('extractor','provider','model','error_code','retryable'))
  OR jsonb_typeof(p->'attachment_id')<>'string'
  OR p->>'artifact_version' !~ '^artifact_[0-9a-f]{64}$' OR jsonb_typeof(p->'artifact_version')<>'string'
  OR p->>'basis' IS DISTINCT FROM CASE WHEN p->>'stage'='text' THEN 'extracted' ELSE 'model_generated' END
  OR jsonb_typeof(p->'truncated')<>'boolean'
  OR jsonb_typeof(p->'text_chars')<>'number'
  OR CASE WHEN p->>'text_chars' ~ '^[0-9]{1,7}$' THEN (p->>'text_chars')::integer>1000000 ELSE true END
  OR EXISTS(SELECT FROM unnest(ARRAY['extractor','provider','model']) f WHERE p ? f
   AND (jsonb_typeof(p->f)<>'string' OR p->>f !~ '^[A-Za-z0-9_.:-]{1,128}$'))
  OR (p->>'state'='failed' AND (jsonb_typeof(p->'error_code')<>'string' OR p->>'error_code' !~ '^[a-z_]{1,64}$'
   OR jsonb_typeof(p->'retryable')<>'boolean'))
  OR (p->>'state'='ready' AND (p ? 'error_code' OR p ? 'retryable'))
  OR p->'source_revision' IS DISTINCT FROM to_jsonb(m.revision)
  OR NOT EXISTS(SELECT FROM public.attachments a WHERE a.id=p->>'attachment_id' AND a.message=m.id AND a.active)
  OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(event_snapshot) k)
   IS DISTINCT FROM ARRAY['account','body','chat','deleted','direction','formatting','message','processing',
    'provider_id','revision','sender','sent_at']
  OR event_snapshot->>'message' IS DISTINCT FROM m.id OR event_snapshot->>'account' IS DISTINCT FROM m.account
  OR event_snapshot->>'chat' IS DISTINCT FROM m.chat OR event_snapshot->>'sender' IS DISTINCT FROM m.sender
  OR event_snapshot->>'provider_id' IS DISTINCT FROM m.provider_id OR event_snapshot->>'body' IS DISTINCT FROM m.body
  OR event_snapshot->'revision' IS DISTINCT FROM to_jsonb(m.revision)
  OR event_snapshot->'deleted' IS DISTINCT FROM 'false'::jsonb
  OR event_snapshot->'direction' IS DISTINCT FROM to_jsonb(m.direction)
  OR jsonb_typeof(event_snapshot->'sent_at')<>'string',true) THEN
  RAISE EXCEPTION 'processing_event_refused' USING ERRCODE='insufficient_privilege';
 END IF;
 -- The attachment stays active until this transaction ends. A direct write takes no
 -- advisory lock: after waiting for it the lock rechecks every predicate on the new row.
 PERFORM FROM public.attachments a WHERE a.id=p->>'attachment_id' AND a.message=m.id AND a.active FOR SHARE;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'processing_event_refused' USING ERRCODE='insufficient_privilege';
 END IF;
 BEGIN
  IF (event_snapshot->>'sent_at')::timestamptz IS DISTINCT FROM m.sent_at THEN
   RAISE EXCEPTION 'processing_event_refused' USING ERRCODE='insufficient_privilege';
  END IF;
 EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
  RAISE EXCEPTION 'processing_event_refused' USING ERRCODE='insufficient_privilege';
 END;
 -- Formatting proof of this exact revision, or the runtime's fixed unknown value.
 SELECT r.snapshot->'formatting',coalesce(r.snapshot->>'origin','reconcile') INTO formatting,source_origin
 FROM public.message_revisions r WHERE r.message=m.id AND r.revision=m.revision;
 IF formatting IS NULL OR formatting='null'::jsonb THEN
  formatting := '{"version":1,"state":"unknown","basis":null,"wire_sha256":null,"entities":null,
   "unsupported_entities":[],"warnings":["native_entities_not_captured"]}'::jsonb;
 END IF;
 IF event_snapshot->'formatting' IS DISTINCT FROM formatting THEN
  RAISE EXCEPTION 'processing_event_refused' USING ERRCODE='insufficient_privilege';
 END IF;
 UPDATE public.feed_counter SET value=value+1 WHERE id=1 RETURNING value INTO position;
 INSERT INTO public.events(id,account,message,kind,revision,origin,snapshot,event_key)
 VALUES(position,m.account,m.id,event_kind,m.revision,coalesce(source_origin,'reconcile'),event_snapshot,processing_key);
 RETURN position;
END $$;

-- One entry point per worker role; each fixes the stages that worker produces.
CREATE FUNCTION public.publish_file_text_event(event_account text,event_message text,event_kind text,
 processing_key text,event_snapshot jsonb) RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT public.publish_processing_event(ARRAY['text'],$1,$2,$3,$4,$5)
$$;
CREATE FUNCTION public.publish_file_enrichment_event(event_account text,event_message text,event_kind text,
 processing_key text,event_snapshot jsonb) RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT public.publish_processing_event(ARRAY['transcript','description'],$1,$2,$3,$4,$5)
$$;
-- New functions are executable by PUBLIC by default; a restore without ACLs reopens them,
-- so Store.migrate() and every worker role template repeat these revokes. The guard's
-- lookup collector_bound_account stays PUBLIC: every writing role runs the trigger.
REVOKE ALL ON FUNCTION public.publish_processing_event(text[],text,text,text,text,jsonb),
 public.publish_file_text_event(text,text,text,text,jsonb),
 public.publish_file_enrichment_event(text,text,text,text,jsonb) FROM PUBLIC;
SET LOCAL lock_timeout=DEFAULT;
