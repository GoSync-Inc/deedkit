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
