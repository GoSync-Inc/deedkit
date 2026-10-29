-- Separate dispatcher login. Do not add public egress to the internal matcher.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_event_delivery') THEN
        CREATE ROLE messaging_event_delivery NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO messaging_event_delivery;
GRANT SELECT(id,accounts,chat_ids,permissions,enabled,scope_version)
    ON principals TO messaging_event_delivery;
GRANT SELECT(id,principal,revoked,expires_at,permissions,permission_version)
    ON api_keys TO messaging_event_delivery;
GRANT SELECT ON subscriptions,subscription_sources,subscription_receivers,subscription_delivery
    TO messaging_event_delivery;
GRANT UPDATE(state,version,updated_at) ON subscriptions TO messaging_event_delivery;
GRANT SELECT ON subscription_outbox TO messaging_event_delivery;
GRANT UPDATE(state,attempts,next_attempt_at,lease_token,lease_until,finished_at,error_code)
    ON subscription_outbox TO messaging_event_delivery;
-- Schema 38: per-key admission before the first POST of a hint ID.
GRANT UPDATE(admitted_at) ON subscription_outbox TO messaging_event_delivery;
-- Schema 48: the dispatcher's liveness row (ANYM-518).
GRANT SELECT,INSERT,UPDATE ON subscription_dispatcher_heartbeat TO messaging_event_delivery;
-- Schema 49 (ANYM-519): the only reader of endpoint secrets and header values; it promotes a rotation
-- after 24 h, records test results and reads the operator's activation (delivery_features).
GRANT SELECT ON probe_metadata TO messaging_event_delivery;
GRANT SELECT ON delivery_endpoints TO messaging_event_delivery;
GRANT UPDATE(signing_secret,pending_secret,secret_fingerprint,pending_fingerprint,rotation_started_at,updated_at)
    ON delivery_endpoints TO messaging_event_delivery;
GRANT SELECT ON delivery_endpoint_tests TO messaging_event_delivery;
GRANT SELECT(subscription,acked_through) ON subscription_consumption TO messaging_event_delivery;
-- References without message text: IDs, timing and native wake evidence only (049).
GRANT EXECUTE ON FUNCTION public.subscription_hint_references(uuid,bigint,bigint,text[],text[]) TO messaging_event_delivery;
GRANT UPDATE(state,lease_token,lease_until,http_status,latency_ms,error_code,finished_at)
    ON delivery_endpoint_tests TO messaging_event_delivery;
-- Schema 50 (ANYM-534): only this component's heartbeat row (row-level security).
GRANT SELECT,INSERT,UPDATE ON component_heartbeats TO messaging_event_delivery;
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_policies WHERE tablename='component_heartbeats' AND policyname='messaging_event_delivery_beat') THEN
        CREATE POLICY messaging_event_delivery_beat ON component_heartbeats TO messaging_event_delivery USING (component='dispatcher') WITH CHECK (component='dispatcher');
    END IF;
END $$;
-- Schema 56 (ANYM-562): module_events hint bodies list type, subject and module of matched events.
GRANT SELECT ON module_events,subscription_module_matches,subscription_module_streams TO messaging_event_delivery;
-- Schema 60 (ANYM-522): a dynamic definition's hint is rechecked against the key's current scope under the
-- fence: each reference's account, chat, hidden mark and own-send evidence; no journal snapshot.
GRANT SELECT(subscription,event,account,chat,hidden,own_send_reply) ON subscription_matches TO messaging_event_delivery;
