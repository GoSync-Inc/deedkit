-- rollout: manual
-- A person's preferred channel per purpose (ANYM-548): one messaging account of the person's own links, set
-- or cleared only by the owner (operator CLI). The resolver reads it behind the caller's visible members, so a
-- preference never reveals a hidden identity. Additive: older runtimes never read this table. Manual only
-- because of the grant below.
SET LOCAL lock_timeout='2s';
CREATE TABLE person_preferences (
 person uuid NOT NULL,
 purpose text NOT NULL CHECK(purpose IN ('message','call')),
 account text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 set_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(person,purpose)
);
-- Writes only through the owner connection (operator CLI); the agent-facing role reads.
DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_gateway') THEN
  GRANT SELECT ON person_preferences TO messaging_gateway;
 END IF;
END $$;
