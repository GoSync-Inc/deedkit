-- rollout: manual
-- Component heartbeats (ANYM-534): the API, the matcher and the dispatcher each refresh one row with their
-- package version and the newest schema they know. `set-delivery-features --enable` refuses unless all three
-- are fresh and agree. Additive: older runtimes write nothing here. Manual only because of the grants below.
-- Row-level security keeps each role on its own row, so one component can never report for another.
SET LOCAL lock_timeout='2s';
CREATE TABLE component_heartbeats (
 component text PRIMARY KEY CHECK(component IN ('api','matcher','dispatcher')),
 version text NOT NULL CHECK(version ~ '^[0-9A-Za-z.+-]{1,64}$'),
 schema_version integer NOT NULL CHECK(schema_version>0),
 beat_at timestamptz NOT NULL
);
ALTER TABLE component_heartbeats ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_gateway') THEN
  GRANT SELECT,INSERT,UPDATE ON component_heartbeats TO messaging_gateway;
  CREATE POLICY messaging_gateway_beat ON component_heartbeats TO messaging_gateway
   USING (component='api') WITH CHECK (component='api');
 END IF;
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_subscriptions') THEN
  GRANT SELECT,INSERT,UPDATE ON component_heartbeats TO messaging_subscriptions;
  CREATE POLICY messaging_subscriptions_beat ON component_heartbeats TO messaging_subscriptions
   USING (component='matcher') WITH CHECK (component='matcher');
 END IF;
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_event_delivery') THEN
  GRANT SELECT,INSERT,UPDATE ON component_heartbeats TO messaging_event_delivery;
  CREATE POLICY messaging_event_delivery_beat ON component_heartbeats TO messaging_event_delivery
   USING (component='dispatcher') WITH CHECK (component='dispatcher');
 END IF;
END $$;
