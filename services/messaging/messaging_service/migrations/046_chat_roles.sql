-- rollout: expand
-- The account's own role in a Telegram channel or group (ANYM-533). Nullable columns without a default:
-- a catalog-only change under the brief chats lock that migrate() bounds. The collector writes them from
-- each discovery pass; the code validates the values (chat_roles.py), so deployed schema-45 code, which
-- names its columns, keeps working unchanged. NULL reads unknown.
ALTER TABLE chats ADD COLUMN my_role text, ADD COLUMN can_post boolean, ADD COLUMN role_observed_at timestamptz,
 ADD COLUMN username text;
