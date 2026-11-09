-- ANYM-908: the negotiation worker's grants on existing objects, installed by the operator as owner after schema 76
-- (whose migration creates the NOLOGIN role messaging_negotiations and its grants on the six negotiation tables), so
-- this file only grants on existing objects; a missing role fails it at once.
-- Read-only on the archive: no message, chat or metadata write, no send, key, provider-session or egress permission.
-- Each grant names what it is for; the worker's start fence (negotiation_worker.REQUIRED) checks each one alone.
GRANT USAGE ON SCHEMA public TO messaging_negotiations;
-- The schema version read (store.schema_version).
GRANT SELECT(name,value) ON probe_metadata TO messaging_negotiations;
-- The journal cursor (message.created, message.edited, message.deleted in id order) and the 14-day backfill start.
GRANT SELECT(id,account,message,kind,created_at) ON events TO messaging_negotiations;
-- The rules' input (negotiation_store.SOURCES_SQL), deletion and purge state, and the group reply match
-- (conversation_state.group_attention_sql compares provider_id).
GRANT SELECT(id,account,chat,provider_id,sender,direction,body,sent_at,revision,deleted,purged)
    ON messages TO messaging_negotiations;
-- Scope (private, group, channel) and the reply match (provider_chat).
GRANT SELECT(id,account,chat_type,provider_chat) ON chats TO messaging_negotiations;
-- Mentions of and replies to the owner.
GRANT SELECT(message,revision,metadata) ON message_metadata TO messaging_negotiations;
-- The version heartbeat (heartbeats.beat): its upsert needs SELECT for the conflict target. Only the row
-- component='negotiations' (row-level security, the policy of the negotiation_heartbeat migration).
GRANT SELECT,INSERT,UPDATE ON component_heartbeats TO messaging_negotiations;
