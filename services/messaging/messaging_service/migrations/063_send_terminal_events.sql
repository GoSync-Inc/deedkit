-- rollout: manual
-- ANYM-655: same collector boundary and grants; genuine unsent terminal results
-- may wake the originating key. Applied migration055 remains immutable.
SET LOCAL lock_timeout='2s';
CREATE INDEX outgoing_expiry ON outgoing_messages(account,expires_at,id) WHERE status='queued';

CREATE OR REPLACE FUNCTION public.collector_account_guard() RETURNS trigger
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
    IF TG_TABLE_NAME='events' AND TG_OP='INSERT' AND side.message IS NULL THEN
     IF side.kind IS DISTINCT FROM 'send.failed' OR NOT EXISTS(SELECT FROM public.outgoing_messages o
       WHERE o.id=side.snapshot->>'send_id' AND o.account=bound AND o.status IN ('failed','unknown','expired','cancelled')
       AND o.chat=side.snapshot->>'chat' AND o.principal=side.snapshot->>'principal'
       AND o.key_id::text=side.snapshot->>'key_id') THEN
      RAISE EXCEPTION 'collector_account_mismatch' USING ERRCODE='insufficient_privilege';
     END IF;
    ELSIF NOT EXISTS(SELECT FROM public.messages m WHERE m.id=side.message AND m.account=bound) THEN
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
