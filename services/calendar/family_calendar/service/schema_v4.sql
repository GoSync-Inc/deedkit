-- One-call scheduling (ANYM-552). Additive only: version-3 code never reads these columns.
-- A family's own timezone and default calendar; both optional (null: the default calendar's zone, then UTC).
ALTER TABLE groups ADD COLUMN timezone text, ADD COLUMN default_calendar_ref text;
-- A schedule preview is a plan with a confirm token: the sha256 of the token, the tool it was made by, the
-- canonical request it resolved, and the operation that consumed it (set once, never cleared).
ALTER TABLE plans ADD COLUMN token_hash text, ADD COLUMN tool text, ADD COLUMN request jsonb,
 ADD COLUMN operation_id text;
CREATE UNIQUE INDEX plans_token ON plans(token_hash) WHERE token_hash IS NOT NULL;
CREATE UNIQUE INDEX plans_consumed ON plans(operation_id) WHERE operation_id IS NOT NULL;
-- A calendar choice: the request a selection_required stored, so each candidate previews it in its calendar by
-- this token's sha256 and never repeats the caller's title. Kept like plans; valid 600 s. No foreign key: the
-- migration takes no lock on principals (as migration 3).
CREATE TABLE schedule_selections(token_hash text PRIMARY KEY, principal_id text NOT NULL,
 request jsonb NOT NULL, expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
