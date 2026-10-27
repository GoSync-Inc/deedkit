-- rollout: manual
-- Hint dispatcher liveness (ANYM-518): one row the dispatcher refreshes on each pass; capabilities call
-- the webhook transport operational only while it is newer than 120 s. No row means no dispatcher has
-- run. Additive: a schema-47 runtime refuses this schema anyway. Manual only because of the grants below.
SET LOCAL lock_timeout='2s';
CREATE TABLE subscription_dispatcher_heartbeat (
 id smallint PRIMARY KEY CHECK(id=1),
 beat_at timestamptz NOT NULL
);
-- The dispatcher writes; the agent-facing role reads.
DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_event_delivery') THEN
  GRANT SELECT,INSERT,UPDATE ON subscription_dispatcher_heartbeat TO messaging_event_delivery;
 END IF;
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_gateway') THEN
  GRANT SELECT ON subscription_dispatcher_heartbeat TO messaging_gateway;
 END IF;
END $$;
