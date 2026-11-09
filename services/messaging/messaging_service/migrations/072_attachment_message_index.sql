-- rollout: manual
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": CREATE INDEX, SET LOCAL and a DO
-- block that only checks a precondition).
-- A message's attachment rows get an index (ANYM-852). Archive.ingest updates `attachments WHERE message=?` (the
-- active flag) on every created, edited or deleted message and checks `attachments WHERE message=?` on every text
-- envelope of a stored message (ANYM-839), under the account lock and, on the media path, the global residency
-- lock. No index on attachments starts with `message`, so each lookup read the whole table (production
-- 2026-11-09: 56,523 rows, 1,528 buffers, 2.4-3.7 ms). A message's FK check on DELETE FROM messages gains it too.
-- Additive: no row, column, function, grant or writer changes; older runtimes plan their queries with it. "manual"
-- only because it indexes an existing table (classify); unattended_class still admits it (as 068).
-- Built inside the migration transaction: the SHARE lock holds attachment INSERTs and UPDATEs for the build
-- (production's 12.5 MB heap: about 40 ms on a synthetic copy), lock_timeout 2s refuses to queue behind a long writer.
SET LOCAL lock_timeout='2s';
CREATE INDEX IF NOT EXISTS attachment_message ON attachments(message);
SET LOCAL lock_timeout=DEFAULT;
-- IF NOT EXISTS keeps any relation of that name: an interrupted concurrent build (INVALID), a partial or unique
-- index, other or more keys, another collation or operator class. Only a valid, ready, non-unique plain btree on
-- exactly (message), with the column's collation and the type's default btree operator class, lets the ledger
-- advance. A unique one would refuse a message's second attachment.
DO $$ BEGIN
 IF NOT EXISTS(SELECT FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class x ON x.oid=i.indexrelid
   JOIN pg_catalog.pg_attribute m ON m.attrelid=i.indrelid AND m.attname='message'
   WHERE i.indexrelid=to_regclass('public.attachment_message') AND i.indrelid='public.attachments'::regclass
    AND i.indisvalid AND i.indisready AND NOT i.indisunique AND NOT i.indisexclusion
    AND i.indpred IS NULL AND i.indexprs IS NULL
    AND i.indnatts=1 AND i.indnkeyatts=1 AND i.indoption[0]=0
    AND x.relam=(SELECT a.oid FROM pg_catalog.pg_am a WHERE a.amname='btree')
    AND i.indkey[0]=m.attnum AND i.indcollation[0]=m.attcollation
    AND i.indclass[0]=(SELECT c.oid FROM pg_catalog.pg_opclass c WHERE c.opcmethod=x.relam
     AND c.opcintype=m.atttypid AND c.opcdefault)) THEN
  RAISE EXCEPTION 'attachment_message is not a valid btree index on attachments(message): DROP INDEX CONCURRENTLY public.attachment_message, then migrate again';
 END IF;
END $$;
