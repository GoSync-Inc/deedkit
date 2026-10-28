-- rollout: manual
-- messages_send (ANYM-546): the durable preview record behind a confirm_token and the parent record of a composite
-- send. Additive: older runtimes never read these tables. Manual only because of the grants below.
-- A preview keeps only the hash of its token. Text leaves both tables once no unsent step needs it (send_intent.py).
SET LOCAL lock_timeout='2s';
CREATE TABLE send_previews (
 token_hash text PRIMARY KEY CHECK(token_hash ~ '^[a-f0-9]{64}$'),
 principal text NOT NULL REFERENCES principals(id),
 key_id uuid NOT NULL REFERENCES api_keys(id),
 tool text NOT NULL CHECK(tool ~ '^[a-z][a-z0-9_]{0,63}$'),
 arguments jsonb NOT NULL,
 resolution jsonb NOT NULL,
 intent text,
 expires_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX send_previews_unbound ON send_previews(expires_at) WHERE intent IS NULL;
CREATE TABLE send_intents (
 id text PRIMARY KEY DEFAULT ('si_' || replace(gen_random_uuid()::text,'-','')) CHECK(id ~ '^si_[a-f0-9]{32}$'),
 principal text NOT NULL REFERENCES principals(id),
 key_id uuid NOT NULL REFERENCES api_keys(id),
 tool text NOT NULL CHECK(tool ~ '^[a-z][a-z0-9_]{0,63}$'),
 request_id text CHECK(request_id ~ '^[a-zA-Z0-9_.:-]{8,128}$'),
 token_hash text UNIQUE CHECK(token_hash ~ '^[a-f0-9]{64}$'),
 parent_key text NOT NULL,
 request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{64}$'),
 plan jsonb NOT NULL,
 steps jsonb NOT NULL,
 attempt integer NOT NULL DEFAULT 1 CHECK(attempt BETWEEN 1 AND 100),
 state text NOT NULL DEFAULT 'running' CHECK(state IN ('running','succeeded','failed','unknown')),
 result jsonb,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(request_id IS NOT NULL OR token_hash IS NOT NULL),
 UNIQUE(principal,request_id)
);
DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_gateway') THEN
  GRANT SELECT,INSERT,UPDATE,DELETE ON send_previews TO messaging_gateway;
  GRANT SELECT,INSERT,UPDATE ON send_intents TO messaging_gateway;
 END IF;
END $$;
