-- rollout: expand
-- ANYM-575: the module router's own state. One cursor per module (the exclusive module seq it has read
-- through and the producer's journal_id), advanced in the same short transaction as the publish; and the
-- ranges of the module journal it could not publish (a quarantined event, a pruned range, a source reset),
-- which keep the source's coverage incomplete until a re-fetch publishes them or the operator abandons
-- them. Sequence numbers, reason codes and times only. Older runtimes never read these tables.
CREATE TABLE IF NOT EXISTS module_router_cursors (
 source text PRIMARY KEY CHECK(source IN ('deedkit:calendar','deedkit:calls')),
 journal_id text CHECK(journal_id IS NULL OR length(journal_id) BETWEEN 1 AND 64),
 after_seq bigint NOT NULL CHECK(after_seq BETWEEN 0 AND 9007199254740991),
 origin text NOT NULL CHECK(origin IN ('head','operator','router')),
 updated_at timestamptz NOT NULL DEFAULT now()
);
-- kind quarantine: (after_seq, through_seq] held events the contract refused; retention_gap: the producer
-- pruned it before the router read it; source_reset: a new journal_id or a cursor going back (through_seq
-- NULL), the router reads the source again only after the operator sets its cursor. A resolved row stays
-- as history and the same range can open again (a new id); the router opens one only when no open row of
-- the same (source, kind, after_seq) exists, under its cursor row lock. journal_id names the producer
-- journal incarnation; attempted_at orders the re-fetches so no range starves another.
CREATE TABLE IF NOT EXISTS module_router_gaps (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 source text NOT NULL CHECK(source IN ('deedkit:calendar','deedkit:calls')),
 kind text NOT NULL CHECK(kind IN ('quarantine','retention_gap','source_reset')),
 journal_id text CHECK(journal_id IS NULL OR length(journal_id) BETWEEN 1 AND 64),
 after_seq bigint NOT NULL CHECK(after_seq BETWEEN 0 AND 9007199254740991),
 through_seq bigint CHECK(through_seq IS NULL OR through_seq>after_seq AND through_seq<=9007199254740991),
 code text NOT NULL CHECK(code ~ '^[a-z_]{1,64}$'),
 opened_at timestamptz NOT NULL DEFAULT now(),
 attempted_at timestamptz,
 resolved_at timestamptz,
 resolution text CHECK(resolution IN ('refetched','abandoned')),
 CHECK((resolved_at IS NULL)=(resolution IS NULL)),
 CHECK((through_seq IS NULL)=(kind='source_reset'))
);
CREATE INDEX IF NOT EXISTS module_router_gaps_source ON module_router_gaps(source,resolved_at,after_seq);
