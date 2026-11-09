-- rollout: manual
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": CREATE INDEX, SET LOCAL and a DO
-- block that only checks a precondition).
-- Group attention reads its candidates (ANYM-915). A group with more than ATTENTION_WINDOW incoming messages since
-- the owner's latest one no longer walks that history (production: up to 29,192 rows and 28,113 quoted-reply probes
-- for 0 rows per read); it reads the rows whose metadata names the owner and the replies to the chat's outgoing
-- messages. Two partial indexes on message_metadata find them: the boolean keys (production about 600 rows) and the
-- reply reference (production about 69,000 of 203,000 rows).
-- Additive: no row, column, function, grant or writer changes; older runtimes plan their queries with them. "manual"
-- only because it indexes an existing table (classify); unattended_class still admits it (as 068, 072 and 074).
-- Built inside the migration transaction: the SHARE lock holds metadata writes for the build (production table 98 MB);
-- lock_timeout 2s refuses to queue behind a long writer.
SET LOCAL lock_timeout='2s';
CREATE INDEX IF NOT EXISTS message_metadata_owner_attention ON message_metadata(message)
    WHERE metadata->'mentions_owner'='true'::jsonb OR metadata->'reply_to_owner'='true'::jsonb;
CREATE INDEX IF NOT EXISTS message_metadata_reply_reference ON message_metadata((metadata->>'reply_to_provider_id'))
    WHERE metadata->>'reply_to_provider_id' IS NOT NULL;
SET LOCAL lock_timeout=DEFAULT;
-- IF NOT EXISTS keeps any relation of that name: an interrupted concurrent build (INVALID), a plain or unique index,
-- other or more keys, another access method. Only valid, ready, non-unique partial btree indexes with one key, the
-- message column for the first and one expression for the second, let the ledger advance.
DO $$ BEGIN
 IF NOT EXISTS(SELECT FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class x ON x.oid=i.indexrelid
   JOIN pg_catalog.pg_attribute t ON t.attrelid=i.indrelid AND t.attname='message'
   WHERE i.indexrelid=to_regclass('public.message_metadata_owner_attention') AND i.indrelid='public.message_metadata'::regclass
    AND i.indisvalid AND i.indisready AND NOT i.indisunique AND NOT i.indisexclusion
    AND i.indpred IS NOT NULL AND i.indexprs IS NULL
    AND i.indnatts=1 AND i.indnkeyatts=1 AND i.indoption[0]=0
    AND x.relam=(SELECT a.oid FROM pg_catalog.pg_am a WHERE a.amname='btree')
    AND i.indkey[0]=t.attnum) THEN
  RAISE EXCEPTION 'message_metadata_owner_attention is not the valid btree index this migration defines: DROP INDEX CONCURRENTLY public.message_metadata_owner_attention, then migrate again';
 END IF;
 IF NOT EXISTS(SELECT FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class x ON x.oid=i.indexrelid
   WHERE i.indexrelid=to_regclass('public.message_metadata_reply_reference') AND i.indrelid='public.message_metadata'::regclass
    AND i.indisvalid AND i.indisready AND NOT i.indisunique AND NOT i.indisexclusion
    AND i.indpred IS NOT NULL AND i.indexprs IS NOT NULL
    AND i.indnatts=1 AND i.indnkeyatts=1 AND i.indoption[0]=0 AND i.indkey[0]=0
    AND x.relam=(SELECT a.oid FROM pg_catalog.pg_am a WHERE a.amname='btree')) THEN
  RAISE EXCEPTION 'message_metadata_reply_reference is not the valid btree index this migration defines: DROP INDEX CONCURRENTLY public.message_metadata_reply_reference, then migrate again';
 END IF;
END $$;
