-- Calendar invitations (ANYM-555). Additive only: version-4 code never reads this table.
-- The owner's opt-in that lets calendar_schedule add guests to new events in a calendar, which Google may email
-- (a send under the owner's account). No row or enabled=false: off. Only the owner CLI writes it. A new table, not a
-- calendars column: running processes keep their prepared `calendars` statements across the migration. No foreign
-- key: the migration takes no lock on calendars (as migrations 3 and 4).
CREATE TABLE invitation_opt_ins(calendar_ref text PRIMARY KEY, enabled boolean NOT NULL,
 changed_at timestamptz NOT NULL DEFAULT now());
