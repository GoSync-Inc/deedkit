-- rollout: manual
-- ANYM-464: an owner-confirmed Telegram message or file is scheduled for a set time; Telegram holds it. A send or a
-- send_file operation rests in 'scheduled' and ends 'sent'/'succeeded' when Telegram publishes it, or
-- 'scheduled_cancelled' when it is deleted before the time. Five nullable or defaulted columns per table hold the
-- current time Telegram reports (schedule_at), the scheduled-list message ID, why it was cancelled, whether the owner
-- edited its text in the app, and when the collector made its one overdue check. They hold no message text.
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": two CHECK widening pairs proven a
-- superset on the same column, as 064 and 085; ADD COLUMN, nullable or with a constant default, CHECKs on the new
-- columns only; two partial indexes on rows no runtime writes yet; no row is read, rewritten or deleted). Grants on
-- the new columns only, in collector_role.sql and gateway_role.sql. A schema-85 runtime never names the columns and
-- never sees the new statuses: only a schema-87 runtime writes them.
SET LOCAL lock_timeout='2s';
ALTER TABLE outgoing_messages DROP CONSTRAINT outgoing_messages_status_check;
ALTER TABLE outgoing_messages ADD CONSTRAINT outgoing_messages_status_check CHECK(status IN
 ('queued','dispatching','sent','failed','expired','cancelled','unknown','scheduled','scheduled_cancelled'));
ALTER TABLE messenger_commands DROP CONSTRAINT messenger_commands_status_check;
ALTER TABLE messenger_commands ADD CONSTRAINT messenger_commands_status_check CHECK(status IN
 ('queued','dispatching','succeeded','failed','expired','cancelled','unknown','scheduled','scheduled_cancelled'));
ALTER TABLE outgoing_messages
 ADD COLUMN IF NOT EXISTS schedule_at timestamptz,
 ADD COLUMN IF NOT EXISTS scheduled_message_id text CONSTRAINT outgoing_scheduled_message_id
  CHECK(scheduled_message_id ~ '^[1-9][0-9]{0,18}$'),
 ADD COLUMN IF NOT EXISTS cancel_reason text CONSTRAINT outgoing_cancel_reason CHECK(cancel_reason IN ('owner_app','agent')),
 ADD COLUMN IF NOT EXISTS edited_in_app boolean NOT NULL DEFAULT false,
 ADD COLUMN IF NOT EXISTS schedule_checked_at timestamptz;
ALTER TABLE messenger_commands
 ADD COLUMN IF NOT EXISTS schedule_at timestamptz,
 ADD COLUMN IF NOT EXISTS scheduled_message_id text CONSTRAINT command_scheduled_message_id
  CHECK(scheduled_message_id ~ '^[1-9][0-9]{0,18}$'),
 ADD COLUMN IF NOT EXISTS cancel_reason text CONSTRAINT command_cancel_reason CHECK(cancel_reason IN ('owner_app','agent')),
 ADD COLUMN IF NOT EXISTS edited_in_app boolean NOT NULL DEFAULT false,
 ADD COLUMN IF NOT EXISTS schedule_checked_at timestamptz;
CREATE INDEX IF NOT EXISTS outgoing_scheduled ON outgoing_messages(account,schedule_at,id) WHERE status='scheduled';
CREATE INDEX IF NOT EXISTS messenger_commands_scheduled ON messenger_commands(account,schedule_at,id)
 WHERE status='scheduled';
