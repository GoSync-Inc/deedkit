-- rollout: manual
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": CREATE INDEX, SET LOCAL and a DO
-- block that only checks a precondition).
-- The journal's commit times get an index (ANYM-865). get_sync_status(include_latency=true) and `sync-latency` read
-- each stage over a 1 to 168 h window as `events WHERE created_at>=?`; events had no index starting with created_at,
-- so every such read scanned the whole journal (production about 300k rows). An id range is no substitute:
-- created_at is the inserting transaction's start, which no writer bounds, so it is not ordered by id.
-- Additive: no row, column, function, grant or writer changes; older runtimes plan their queries with it. "manual"
-- only because it indexes an existing table (classify); unattended_class still admits it (as 068 and 072).
-- Built inside the migration transaction: the SHARE lock holds event INSERTs for the build (a synthetic copy of
-- 350k events with production-shaped snapshots, 391 MB: 46 to 98 ms); lock_timeout 2s refuses to queue behind a
-- long writer.
SET LOCAL lock_timeout='2s';
CREATE INDEX IF NOT EXISTS events_created_at ON events(created_at);
SET LOCAL lock_timeout=DEFAULT;
-- IF NOT EXISTS keeps any relation of that name: an interrupted concurrent build (INVALID), a partial or unique
-- index, other or more keys, another operator class. Only a valid, ready, non-unique plain btree on exactly
-- (created_at) with the type's default btree operator class lets the ledger advance.
DO $$ BEGIN
 IF NOT EXISTS(SELECT FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class x ON x.oid=i.indexrelid
   JOIN pg_catalog.pg_attribute t ON t.attrelid=i.indrelid AND t.attname='created_at'
   WHERE i.indexrelid=to_regclass('public.events_created_at') AND i.indrelid='public.events'::regclass
    AND i.indisvalid AND i.indisready AND NOT i.indisunique AND NOT i.indisexclusion
    AND i.indpred IS NULL AND i.indexprs IS NULL
    AND i.indnatts=1 AND i.indnkeyatts=1 AND i.indoption[0]=0
    AND x.relam=(SELECT a.oid FROM pg_catalog.pg_am a WHERE a.amname='btree')
    AND i.indkey[0]=t.attnum
    AND i.indclass[0]=(SELECT c.oid FROM pg_catalog.pg_opclass c WHERE c.opcmethod=x.relam
     AND c.opcintype=t.atttypid AND c.opcdefault)) THEN
  RAISE EXCEPTION 'events_created_at is not a valid btree index on events(created_at): DROP INDEX CONCURRENTLY public.events_created_at, then migrate again';
 END IF;
END $$;
