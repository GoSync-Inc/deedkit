-- rollout: manual
-- ANYM-529: search reaches shared contact cards through the few metadata rows that carry one.
-- Production builds the index before this file, outside its transaction (`CREATE INDEX CONCURRENTLY
-- IF NOT EXISTS message_metadata_contact ON message_metadata(message) WHERE metadata ? 'contact'`,
-- release step 0), so migrate only checks it; a database without it builds it here.
CREATE INDEX IF NOT EXISTS message_metadata_contact ON message_metadata(message) WHERE metadata ? 'contact';
-- An interrupted concurrent build leaves an INVALID index that IF NOT EXISTS then skips.
DO $$ BEGIN
 IF NOT coalesce((SELECT i.indisvalid AND i.indisready AND pg_get_indexdef(i.indexrelid)=
   'CREATE INDEX message_metadata_contact ON public.message_metadata USING btree (message) WHERE (metadata ? ''contact''::text)'
   FROM pg_index i WHERE i.indexrelid='public.message_metadata_contact'::regclass),false) THEN
  RAISE EXCEPTION 'message_metadata_contact is not a valid index on message_metadata(message) WHERE metadata ? ''contact'': DROP INDEX CONCURRENTLY message_metadata_contact, then CREATE INDEX CONCURRENTLY message_metadata_contact ON message_metadata(message) WHERE metadata ? ''contact''';
 END IF;
END $$;
