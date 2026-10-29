-- rollout: expand
-- ANYM-522: a dynamic definition (all chats the key can see) records each reference's account and chat,
-- so every disclosure point filters by the key's current scope without reading the journal, and the
-- dispatcher, which reads no message snapshot, can too. hidden marks a reference whose chat or account
-- left the principal's scope (set_access); it stays hidden after a re-add. own_send_reply is the
-- matcher's reply_to_own_send evidence. Explicit definitions write none of them; older runtimes ignore them.
SET LOCAL lock_timeout='2s';
ALTER TABLE subscription_matches ADD COLUMN account text;
ALTER TABLE subscription_matches ADD COLUMN chat text;
ALTER TABLE subscription_matches ADD COLUMN hidden boolean NOT NULL DEFAULT false;
ALTER TABLE subscription_matches ADD COLUMN own_send_reply boolean NOT NULL DEFAULT false;
