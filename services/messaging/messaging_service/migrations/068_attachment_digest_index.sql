-- rollout: manual
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": CREATE INDEX, SET LOCAL and a DO
-- block that only checks a precondition).
-- An original's identity lookup gets an index. register_resident_original (033), which the attachments INSERT
-- trigger calls for every new file, checks `attachments WHERE digest=? AND bytes=?`, and lock_lifecycle_sources
-- (033) reads `attachments WHERE digest=?`; both run while they hold the global 'attachment-residency-v1'
-- advisory lock. The only digest index (013 attachment_occurrences) is partial on `active`, so both read the whole
-- table under that lock (production 2026-11-09: 56,523 rows, 7 ms per lookup, 3 to 9 sessions waiting for it).
-- Additive: no row, column, function, grant or writer changes; older runtimes plan their queries with it. "manual"
-- only because it indexes an existing table (classify); unattended_class still admits it (as 052).
-- Built inside the migration transaction: the SHARE lock holds attachment INSERTs for the build (production's
-- 12.5 MB heap: see the PR's measurement), lock_timeout 2s refuses to queue behind a long writer.
SET LOCAL lock_timeout='2s';
CREATE INDEX IF NOT EXISTS attachment_digest_bytes ON attachments(digest,bytes);
SET LOCAL lock_timeout=DEFAULT;
-- IF NOT EXISTS keeps any relation of that name: an interrupted concurrent build (INVALID), a partial index, other
-- keys, another collation or operator class. Only a valid, ready, plain btree on exactly (digest,bytes), each key
-- with its column's collation and the type's default btree operator class, lets the ledger advance.
DO $$ BEGIN
 IF NOT EXISTS(SELECT FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class x ON x.oid=i.indexrelid
   JOIN pg_catalog.pg_attribute d ON d.attrelid=i.indrelid AND d.attname='digest'
   JOIN pg_catalog.pg_attribute b ON b.attrelid=i.indrelid AND b.attname='bytes'
   WHERE i.indexrelid=to_regclass('public.attachment_digest_bytes') AND i.indrelid='public.attachments'::regclass
    AND i.indisvalid AND i.indisready AND i.indpred IS NULL AND i.indexprs IS NULL
    AND i.indnatts=2 AND i.indnkeyatts=2 AND i.indoption[0]=0 AND i.indoption[1]=0
    AND x.relam=(SELECT a.oid FROM pg_catalog.pg_am a WHERE a.amname='btree')
    AND i.indkey[0]=d.attnum AND i.indkey[1]=b.attnum
    AND i.indcollation[0]=d.attcollation AND i.indcollation[1]=b.attcollation
    AND i.indclass[0]=(SELECT c.oid FROM pg_catalog.pg_opclass c WHERE c.opcmethod=x.relam
     AND c.opcintype=d.atttypid AND c.opcdefault)
    AND i.indclass[1]=(SELECT c.oid FROM pg_catalog.pg_opclass c WHERE c.opcmethod=x.relam
     AND c.opcintype=b.atttypid AND c.opcdefault)) THEN
  RAISE EXCEPTION 'attachment_digest_bytes is not a valid btree index on attachments(digest,bytes): DROP INDEX CONCURRENTLY public.attachment_digest_bytes, then migrate again';
 END IF;
END $$;
