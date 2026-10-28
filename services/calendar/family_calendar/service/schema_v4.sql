-- One-call scheduling (ANYM-552). Additive only: version-3 code never reads these columns.
-- A family's own timezone and default calendar; both optional (null: the default calendar's zone, then UTC).
ALTER TABLE groups ADD COLUMN timezone text, ADD COLUMN default_calendar_ref text;
-- A schedule preview is a plan with a confirm token: the sha256 of the token, the tool it was made by, the
-- canonical request it resolved, and the operation that consumed it (set once, never cleared).
ALTER TABLE plans ADD COLUMN token_hash text, ADD COLUMN tool text, ADD COLUMN request jsonb,
 ADD COLUMN operation_id text;
CREATE UNIQUE INDEX plans_token ON plans(token_hash) WHERE token_hash IS NOT NULL;
CREATE UNIQUE INDEX plans_consumed ON plans(operation_id) WHERE operation_id IS NOT NULL;
