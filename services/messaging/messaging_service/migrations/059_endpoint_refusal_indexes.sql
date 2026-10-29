-- rollout: manual
-- An endpoint's vendor refusal (ANYM-583): the endpoint view reads, in one statement, the latest-created test, the
-- latest finished test, the newest delivered and refused test and the newest delivered and refused hint. Each read
-- gets a partial index in its own order, so the view never scans an endpoint's history (049 indexes only queued
-- tests). Read side only: no table, column, grant or writer change; older runtimes never use these indexes.
-- Production builds the six indexes before this file, outside its transaction, with the CREATE INDEX CONCURRENTLY
-- form of each definition below (release step 0), so migrate only checks them and takes no table lock. A database
-- without them (tests, a new install) builds them here, inside the migration transaction: it takes the table's
-- SHARE lock (lock_timeout 2s) and only then measures, building only while the heap is at most
-- deedkit.migration_inline_index_bytes (default 8 MiB); a larger table refuses with the concurrent command instead
-- of blocking the delivery writers for the length of a build.
SET LOCAL lock_timeout='2s';
DO $$
DECLARE
 wanted record;
 limit_bytes bigint := coalesce(nullif(current_setting('deedkit.migration_inline_index_bytes',true),''),'8388608')::bigint;
BEGIN
 -- The expected text is the default deparse; quote_all_identifiers=on would quote every name.
 PERFORM set_config('quote_all_identifiers','off',true);
 FOR wanted IN SELECT * FROM (VALUES
  ('delivery_endpoint_test_latest','public.delivery_endpoint_tests',
   'CREATE INDEX delivery_endpoint_test_latest ON public.delivery_endpoint_tests USING btree (endpoint, created_at DESC, id DESC)'),
  ('delivery_endpoint_test_finished','public.delivery_endpoint_tests',
   'CREATE INDEX delivery_endpoint_test_finished ON public.delivery_endpoint_tests USING btree (endpoint, finished_at DESC, id DESC) WHERE (state = ANY (ARRAY[''delivered''::text, ''failed''::text]))'),
  ('delivery_endpoint_test_delivered','public.delivery_endpoint_tests',
   'CREATE INDEX delivery_endpoint_test_delivered ON public.delivery_endpoint_tests USING btree (endpoint, finished_at DESC, id DESC) WHERE (state = ''delivered''::text)'),
  ('delivery_endpoint_test_refused','public.delivery_endpoint_tests',
   'CREATE INDEX delivery_endpoint_test_refused ON public.delivery_endpoint_tests USING btree (endpoint, finished_at DESC, id DESC) WHERE ((state = ''failed''::text) AND (error_code = ''vendor_refused''::text))'),
  ('subscription_hint_endpoint_delivered','public.subscription_outbox',
   'CREATE INDEX subscription_hint_endpoint_delivered ON public.subscription_outbox USING btree (endpoint, finished_at DESC, id DESC) WHERE ((endpoint IS NOT NULL) AND (state = ''delivered''::text))'),
  ('subscription_hint_endpoint_refused','public.subscription_outbox',
   'CREATE INDEX subscription_hint_endpoint_refused ON public.subscription_outbox USING btree (endpoint, finished_at DESC, id DESC) WHERE ((endpoint IS NOT NULL) AND (state = ''dead_letter''::text) AND (error_code = ''vendor_refused''::text))')
 ) AS v(name,tab,definition) LOOP
  IF to_regclass('public.'||wanted.name) IS NULL THEN
   -- The build's own lock first (within lock_timeout), so no writer grows the table between the size check and
   -- the build; the build then holds it for at most a limit_bytes heap.
   EXECUTE format('LOCK TABLE %s IN SHARE MODE',wanted.tab);
   IF pg_relation_size(wanted.tab::regclass)>limit_bytes THEN
    RAISE EXCEPTION '% is missing and % is too large to index inside the migration: run %, then migrate again',
     wanted.name,wanted.tab,replace(wanted.definition,'CREATE INDEX ','CREATE INDEX CONCURRENTLY ');
   END IF;
   EXECUTE wanted.definition;
  END IF;
  -- An interrupted concurrent build leaves an INVALID index under the same name.
  IF NOT coalesce((SELECT i.indisvalid AND i.indisready AND pg_get_indexdef(i.indexrelid)=wanted.definition
    FROM pg_index i WHERE i.indexrelid=('public.'||wanted.name)::regclass),false) THEN
   RAISE EXCEPTION '% is not a valid index: DROP INDEX CONCURRENTLY public.%, then %',wanted.name,wanted.name,
    replace(wanted.definition,'CREATE INDEX ','CREATE INDEX CONCURRENTLY ');
  END IF;
 END LOOP;
END $$;
SET LOCAL lock_timeout=DEFAULT;
