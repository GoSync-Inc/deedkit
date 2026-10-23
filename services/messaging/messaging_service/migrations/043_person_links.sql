-- rollout: manual
-- Owner-made person links (ANYM-283): one person is the set of rows sharing a person UUID,
-- each an exact (account, provider sender) identity. Source messages and profiles are never
-- changed; unlinking deletes only the row. No names or labels are stored. Additive: a
-- schema-42 runtime refuses this schema anyway. Manual only because of the grant below.
SET LOCAL lock_timeout='2s';
CREATE TABLE person_links (
 person uuid NOT NULL,
 account text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 sender text NOT NULL CHECK(length(sender) BETWEEN 1 AND 256),
 linked_at timestamptz NOT NULL DEFAULT now(),
 -- An identity belongs to at most one person, also under concurrent links.
 PRIMARY KEY(account,sender)
);
CREATE INDEX person_links_person ON person_links(person,account,sender);
-- Writes only through the owner connection (operator CLI); the agent-facing role reads.
DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_gateway') THEN
  GRANT SELECT ON person_links TO messaging_gateway;
 END IF;
END $$;
