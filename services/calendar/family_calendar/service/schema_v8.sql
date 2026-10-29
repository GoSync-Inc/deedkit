-- rollout: expand
-- Calendar change journal (ANYM-565). Additive only: version-7 code never reads these tables. No foreign key: the
-- migration takes no lock on calendars (as 3-7).
-- A calendar the journal has a baseline for: its first complete read seeds the baseline and journals nothing.
-- sync_token is Google's nextSyncToken of the calendar (incremental sync); null before it has one or after a 410.
CREATE TABLE journal_calendars(calendar_ref text PRIMARY KEY, seeded_at timestamptz NOT NULL DEFAULT now(),
 refreshed_at timestamptz NOT NULL DEFAULT now(), sync_token text);
-- The last stored state of each event_ref read after the baseline: the next read's comparison baseline, and a
-- cancelled event's snapshot, readable for 30 days after cancelled_at and then deleted by the worker. master_ref is
-- the resource_ref of a Google instance's series master, which a series write names. probed_at: when the worker last
-- read back the CalDAV resource after a window read lost it (fair probe order).
CREATE TABLE event_states(event_ref text PRIMARY KEY, calendar_ref text NOT NULL, resource_ref text NOT NULL,
 master_ref text, series_ref text, original_start text, start_at timestamptz NOT NULL, end_at timestamptz NOT NULL,
 busy boolean NOT NULL, details jsonb NOT NULL, version bigint NOT NULL DEFAULT 1,
 seen_at timestamptz NOT NULL DEFAULT now(), cancelled_at timestamptz, probed_at timestamptz);
CREATE INDEX event_states_window ON event_states(calendar_ref, start_at);
CREATE INDEX event_states_series ON event_states(calendar_ref, series_ref);
-- The journal GET /v1/events serves: references only. seq is gapless and committed in order: every writer takes
-- journal_head's one row (id 1) and adds to head in the transaction that inserts its rows.
CREATE TABLE change_journal(seq bigint PRIMARY KEY CHECK(seq > 0), calendar_ref text NOT NULL,
 type text NOT NULL CHECK(type IN ('created','changed','cancelled')), event_ref text NOT NULL, series_ref text,
 start_at timestamptz NOT NULL, version bigint NOT NULL, changed jsonb, occurrences int NOT NULL DEFAULT 1,
 origin text NOT NULL, at timestamptz NOT NULL DEFAULT now());
CREATE TABLE journal_head(id int PRIMARY KEY CHECK(id = 1), journal_id uuid NOT NULL DEFAULT gen_random_uuid(),
 head bigint NOT NULL DEFAULT 0, retention_floor bigint NOT NULL DEFAULT 0);
