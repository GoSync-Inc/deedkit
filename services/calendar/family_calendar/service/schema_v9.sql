-- rollout: expand
-- Scheduled refresh and starting reminders (ANYM-576). Additive only: version-8 code never reads these tables. No
-- foreign key: the migration takes no lock on calendars (as 3-8).
-- The operator's refresh configuration of a calendar (owner CLI `service refresh-calendar`); a calendar without a row
-- is never refreshed. The worker writes only the schedule and health columns: next_at (NULL: due at once), the last
-- successful refresh, the consecutive failures (auth_failures: of them, provider authorization refused) and health,
-- which is auth_failed after AUTH_STRIKES refusals in a row until the next successful refresh.
CREATE TABLE refresh_calendars(calendar_ref text PRIMARY KEY,
 window_days int NOT NULL DEFAULT 14 CHECK(window_days BETWEEN 1 AND 60),
 cadence_minutes int NOT NULL DEFAULT 5 CHECK(cadence_minutes BETWEEN 1 AND 1440),
 leads int[] NOT NULL DEFAULT '{15}' CHECK(cardinality(leads) BETWEEN 1 AND 4),
 next_at timestamptz, last_ok_at timestamptz, failures int NOT NULL DEFAULT 0, auth_failures int NOT NULL DEFAULT 0,
 error text, health text NOT NULL DEFAULT 'ok' CHECK(health IN ('ok','auth_failed')), health_since timestamptz,
 configured_at timestamptz NOT NULL DEFAULT now());
-- One `calendar.event.starting` per (event_ref, start_at, lead_minutes), in change_journal's seq: the worker takes
-- journal_head's row and adds to head in the transaction that inserts it, as change_journal's writers do.
CREATE TABLE starting_reminders(seq bigint PRIMARY KEY CHECK(seq > 0), calendar_ref text NOT NULL,
 event_ref text NOT NULL, start_at timestamptz NOT NULL,
 lead_minutes int NOT NULL CHECK(lead_minutes BETWEEN 1 AND 1440), at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(event_ref, start_at, lead_minutes));
