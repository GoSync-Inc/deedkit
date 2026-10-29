-- Operator-only installation after schema26. No provider sessions, token hashes,
-- message mutation, key administration or outbound send permissions.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_subscriptions') THEN
        CREATE ROLE messaging_subscriptions NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO messaging_subscriptions;
GRANT SELECT(id,accounts,chat_ids,permissions,enabled,scope_version)
    ON principals TO messaging_subscriptions;
GRANT SELECT(id,principal,revoked,expires_at,permissions,permission_version)
    ON api_keys TO messaging_subscriptions;
GRANT SELECT ON events,feed_counter,subscription_sources TO messaging_subscriptions;
GRANT SELECT ON subscriptions TO messaging_subscriptions;
GRANT UPDATE(state,version,updated_at,matched_through,blocked_reason,gap,
             missing_evidence,last_matched_at) ON subscriptions TO messaging_subscriptions;
GRANT SELECT,INSERT ON subscription_matches TO messaging_subscriptions;
-- Schema 40 retention: read the operator's activation, delete ACKed references at or below
-- acked_through and move the floor.
GRANT SELECT ON probe_metadata TO messaging_subscriptions;
GRANT SELECT ON subscription_consumption TO messaging_subscriptions;
GRANT UPDATE(retention_floor) ON subscriptions TO messaging_subscriptions;
GRANT DELETE ON subscription_matches TO messaging_subscriptions;
-- Schema 62 (ANYM-447): delete finished hints (keeping their counts) and, 30 days after closure, a closed
-- definition's receipts, outbox rows and consumption, then mark its tombstone. No receipt hash read.
GRANT UPDATE(tombstoned_at,deleted_hint_counts,hints_deleted_at,hints_deleted_through) ON subscriptions TO messaging_subscriptions;
GRANT DELETE ON subscription_outbox,subscription_consumption TO messaging_subscriptions;
GRANT SELECT(subscription),DELETE ON subscription_receipts TO messaging_subscriptions;

-- Atomically enqueue opaque references only; no endpoint/secret access or egress.
GRANT SELECT ON subscription_delivery TO messaging_subscriptions;
GRANT SELECT,INSERT ON subscription_outbox TO messaging_subscriptions;
-- Schema 38: durable held hints and silence timers (debounce, batch, quiet hours, budget).
GRANT SELECT,INSERT,UPDATE ON subscription_hint_windows TO messaging_subscriptions;
-- Schema 49 (ANYM-519): an endpoint binding stages a hint only while its endpoint is enabled; no
-- secret, header value or URL column.
GRANT SELECT(id,state) ON delivery_endpoints TO messaging_subscriptions;
-- Schema 50 (ANYM-534): only this component's heartbeat row (row-level security).
GRANT SELECT,INSERT,UPDATE ON component_heartbeats TO messaging_subscriptions;
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_policies WHERE tablename='component_heartbeats' AND policyname='messaging_subscriptions_beat') THEN
        CREATE POLICY messaging_subscriptions_beat ON component_heartbeats TO messaging_subscriptions USING (component='matcher') WITH CHECK (component='matcher');
    END IF;
END $$;
-- Schema 55 (ANYM-535): source.health rows only through the definer function, which derives each
-- from collector_health and the lease; no direct journal, counter or collector-record write.
GRANT EXECUTE ON FUNCTION public.journal_source_health(integer) TO messaging_subscriptions;
-- Schema 56 (ANYM-562): the module stream. Read the module journal, insert and prune module match
-- references, move the module stream's matcher positions; no publish, no source or ACK write.
GRANT SELECT ON module_events,module_event_counter,subscription_module_sources,module_source_health
    TO messaging_subscriptions;
GRANT SELECT,INSERT,DELETE ON subscription_module_matches TO messaging_subscriptions;
GRANT SELECT ON subscription_module_streams TO messaging_subscriptions;
GRANT UPDATE(matched_through,hinted_through,retention_floor,blocked_reason,gap,missing_evidence,last_matched_at)
    ON subscription_module_streams TO messaging_subscriptions;
GRANT EXECUTE ON FUNCTION public.module_hints_ready() TO messaging_subscriptions;
-- A restore runs without ACLs; the definer functions never stay open to PUBLIC.
REVOKE ALL ON FUNCTION public.module_hints_ready(),public.publish_module_event(text,text,text,text,text,text,jsonb,text,text),
    public.report_module_reachability(text,boolean),public.module_event_valid(text,text,text,text,text,jsonb,text,text),
    public.subscription_module_fence() FROM PUBLIC;
-- Schema 60 (ANYM-522): a dynamic definition's per-batch scope snapshot (chat IDs and types, no titles) and
-- the reply_to_own_send lookup: the key's confirmed sends and, for the WhatsApp stanza projection, the exact
-- outgoing archive row and the account's platform. IDs and status only, never message text.
GRANT SELECT(id,account,chat_type) ON chats TO messaging_subscriptions;
GRANT SELECT(principal,key_id,status,account,chat,provider_chat,provider_message_id)
    ON outgoing_messages TO messaging_subscriptions;
GRANT SELECT(account,chat,provider_id,sender,direction,deleted) ON messages TO messaging_subscriptions;
GRANT SELECT(id,platform) ON accounts TO messaging_subscriptions;
