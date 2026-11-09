-- rollout: manual
-- ANYM-907: each chat's scheduling negotiation (ANYM-893 evaluate), stored with versions that are never reused, its
-- source messages, its times and one transition row per committed change; a rebuild queue and the worker's cursor.
-- Derived from message text, never a copy of it: states, times, zones and message references only.
-- It creates the role messaging_negotiations (NOLOGIN, as the role files do) and grants on these new tables only.
-- Release: declared manual for the deploy pause, and production.py migrate-expand still applies it unattended
-- (migration_rollout.unattended_class is expand: non-critical, one transaction, no long name), as any expand. The worker's grants on existing tables (messages, chats, events, ...) come with ANYM-908. messaging_purge
-- gets nothing: the purge job (ANYM-510) is unchanged, and the negotiation worker removes what a purge leaves.
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": new tables, a new role and grants on
-- the new tables only; no existing row, column or grant changes). No foreign key names an existing table, so no
-- existing write path checks or locks these tables. store.migrate re-applies these grants after a restore
-- (pg_restore --no-acl drops them).
SET LOCAL lock_timeout='2s';
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_negotiations') THEN
  CREATE ROLE messaging_negotiations NOLOGIN;
 END IF;
END $$;
GRANT USAGE ON SCHEMA public TO messaging_negotiations;
CREATE TABLE IF NOT EXISTS negotiations (
 id text PRIMARY KEY CHECK(id ~ '^neg_[0-9a-f]{32}$'),
 account text NOT NULL,
 chat text NOT NULL,
 state text NOT NULL CHECK(state IN ('proposed','counter','agreed','cancelled','stale','removed')),
 medium text CHECK(medium IN ('call','video_call','meeting','unknown')),
 confidence text CHECK(confidence IN ('low','medium','high')),
 version bigint NOT NULL CHECK(version>0),
 awaiting text CHECK(awaiting IN ('owner','peer','others','none')),
 rescheduled boolean,
 ambiguous boolean,
 agreed_start timestamptz,
 agreed_end timestamptz,
 agreed_zone text,
 previous_agreed_start timestamptz,
 previous_agreed_end timestamptz,
 previous_agreed_zone text,
 fingerprint text CHECK(fingerprint ~ '^[a-f0-9]{64}$'),
 opened_at timestamptz NOT NULL,
 updated_at timestamptz NOT NULL,
 last_source_at timestamptz,
 -- A removed tombstone keeps only its ids, state, version and times of change.
 CHECK(state<>'removed' OR (medium IS NULL AND confidence IS NULL AND awaiting IS NULL AND rescheduled IS NULL
  AND ambiguous IS NULL AND agreed_start IS NULL AND agreed_end IS NULL AND agreed_zone IS NULL
  AND previous_agreed_start IS NULL AND previous_agreed_end IS NULL AND previous_agreed_zone IS NULL
  AND fingerprint IS NULL AND last_source_at IS NULL))
);
CREATE INDEX IF NOT EXISTS negotiation_chat ON negotiations(account,chat,opened_at DESC,id);
CREATE TABLE IF NOT EXISTS negotiation_sources (
 -- One link per message and negotiation, never moved: a purge finds every negotiation a message ever fed.
 message text NOT NULL,
 negotiation text NOT NULL REFERENCES negotiations(id),
 revision integer,
 removed_at timestamptz,
 intent text,
 confidence text,
 medium text,
 cue_start integer,
 cue_end integer,
 -- A removed source keeps only ids: negotiation, message, revision and removed_at.
 CHECK(removed_at IS NULL OR (intent IS NULL AND confidence IS NULL AND medium IS NULL AND cue_start IS NULL
  AND cue_end IS NULL)),
 PRIMARY KEY(message,negotiation)
);
CREATE INDEX IF NOT EXISTS negotiation_source_owner ON negotiation_sources(negotiation);
CREATE TABLE IF NOT EXISTS negotiation_times (
 negotiation text NOT NULL REFERENCES negotiations(id),
 ord integer NOT NULL CHECK(ord>=0),
 message text NOT NULL,
 status text NOT NULL CHECK(status IN ('current','countered','declined','agreed','declined_only')),
 starts_at timestamptz NOT NULL,
 ends_at timestamptz NOT NULL,
 granularity text NOT NULL,
 zone text NOT NULL,
 zone_source text NOT NULL,
 assumed text[] NOT NULL DEFAULT '{}',
 PRIMARY KEY(negotiation,ord)
);
CREATE TABLE IF NOT EXISTS negotiation_transitions (
 negotiation text NOT NULL REFERENCES negotiations(id),
 version bigint NOT NULL CHECK(version>0),
 kind text NOT NULL CHECK(kind IN ('opened','changed','agreed','cancelled','stale','removed')),
 trigger_message text,
 state text CHECK(state IN ('proposed','counter','agreed','cancelled','stale','removed')),
 awaiting text CHECK(awaiting IN ('owner','peer','others','none')),
 created_at timestamptz NOT NULL,
 published_at timestamptz,
 event_id bigint,
 invalidated_at timestamptz,
 PRIMARY KEY(negotiation,version)
);
CREATE INDEX IF NOT EXISTS negotiation_transition_unpublished ON negotiation_transitions(created_at,negotiation,version)
 WHERE published_at IS NULL AND invalidated_at IS NULL;
CREATE TABLE IF NOT EXISTS negotiation_rebuild (
 account text NOT NULL,
 chat text NOT NULL,
 queued_at timestamptz NOT NULL,
 PRIMARY KEY(account,chat)
);
CREATE TABLE IF NOT EXISTS negotiation_cursor (
 id integer PRIMARY KEY CHECK(id=1),
 event_id bigint NOT NULL DEFAULT 0 CHECK(event_id>=0),
 updated_at timestamptz NOT NULL,
 -- The purge discovery's place in negotiation_sources (its primary key); empty: the start.
 purge_message text NOT NULL DEFAULT '',
 purge_negotiation text NOT NULL DEFAULT ''
);
GRANT SELECT,INSERT,UPDATE,DELETE ON negotiations,negotiation_sources,negotiation_times,negotiation_transitions,
 negotiation_rebuild,negotiation_cursor TO messaging_negotiations;
-- ANYM-909: agents read negotiations through the API role. Guarded: a database without the role (a fresh test
-- cluster) gets it from gateway_role.sql, and store.migrate re-applies it.
DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_gateway') THEN
  GRANT SELECT ON negotiations,negotiation_sources,negotiation_times TO messaging_gateway;
 END IF;
END $$;
