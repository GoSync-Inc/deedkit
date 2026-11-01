-- rollout: manual
-- ANYM-676 (press_button) and ANYM-678 (webview-open, one manual release instead of two): the two permission
-- CHECKs accept 'bot-press' and 'webview-open', and a partial index serves the press_uncertain lookup over earlier
-- press_button rows of one message. Risk tier: medium (docs/operations/production.md "Migration risk tiers": a CHECK
-- widening pair proven a superset on the same column, and a non-unique index on an existing table). No data change,
-- no GRANT. A schema-63 runtime refuses this schema anyway; a key holding either permission needs it.
SET LOCAL lock_timeout='2s';
ALTER TABLE principals DROP CONSTRAINT supported_permissions;
ALTER TABLE principals ADD CONSTRAINT supported_permissions CHECK(permissions <@ ARRAY[
 'cursor-write','download','read','review-write','send','read-live','contacts-read','profile-read',
 'groups-read','message-edit','message-delete','message-react','message-pin','read-state',
 'chat-manage','contacts-manage','groups-manage','profile-manage','subscription-write','upload',
 'subscription-ack','feedback-write','delivery-write','bot-press','webview-open']::text[]);
ALTER TABLE api_keys DROP CONSTRAINT key_supported_permissions;
ALTER TABLE api_keys ADD CONSTRAINT key_supported_permissions CHECK(permissions IS NULL OR permissions <@ ARRAY[
 'cursor-write','download','read','review-write','send','read-live','contacts-read','profile-read',
 'groups-read','message-edit','message-delete','message-react','message-pin','read-state',
 'chat-manage','contacts-manage','groups-manage','profile-manage','subscription-write','upload',
 'subscription-ack','feedback-write','delivery-write','bot-press','webview-open']::text[]);
CREATE INDEX messenger_commands_press ON messenger_commands(account,message,created_at) WHERE operation='press_button';
