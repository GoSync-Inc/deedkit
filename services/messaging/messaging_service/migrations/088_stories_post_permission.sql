-- rollout: manual
-- ANYM-932 (post_story, delete_story): the two permission CHECKs accept 'stories-post', so a key can be granted
-- posting and deleting the owner's Telegram stories (and WhatsApp statuses, ANYM-933). Risk tier: non-critical
-- (docs/operations/production.md "Migration risk tiers": a CHECK widening pair proven a superset on the same column,
-- as 064 and 085). No data change, no GRANT, and no key or principal receives the permission here: a grant is the
-- owner's separate step.
SET LOCAL lock_timeout='2s';
ALTER TABLE principals DROP CONSTRAINT supported_permissions;
ALTER TABLE principals ADD CONSTRAINT supported_permissions CHECK(permissions <@ ARRAY[
 'cursor-write','download','read','review-write','send','read-live','contacts-read','profile-read',
 'groups-read','message-edit','message-delete','message-react','message-pin','read-state',
 'chat-manage','contacts-manage','groups-manage','profile-manage','subscription-write','upload',
 'subscription-ack','feedback-write','delivery-write','bot-press','webview-open','groups-moderate',
 'stories-post']::text[]);
ALTER TABLE api_keys DROP CONSTRAINT key_supported_permissions;
ALTER TABLE api_keys ADD CONSTRAINT key_supported_permissions CHECK(permissions IS NULL OR permissions <@ ARRAY[
 'cursor-write','download','read','review-write','send','read-live','contacts-read','profile-read',
 'groups-read','message-edit','message-delete','message-react','message-pin','read-state',
 'chat-manage','contacts-manage','groups-manage','profile-manage','subscription-write','upload',
 'subscription-ack','feedback-write','delivery-write','bot-press','webview-open','groups-moderate',
 'stories-post']::text[]);
