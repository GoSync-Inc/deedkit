-- rollout: manual
-- ANYM-986: a send may ask, in the same call, to be woken if no reply comes within a time. The send row keeps the
-- confirmed intent (`await_reply`, {"within_s": 60..1209600}) and the outcome of creating the reply_to_send
-- subscription after the message is `sent` (`await_state` pending, created or refused, with the subscription ID or
-- a fixed reason code). The API-side job in await_reply.py reads the row as its queue.
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": four nullable ADD COLUMNs with CHECKs
-- on the new columns only, and one partial index that matches no row yet; no row is read, rewritten or deleted;
-- migration_rollout.risk_class says non-critical, with no DO block). It
-- is DROP-free and grants only on the columns it adds: INSERT(await_reply,await_state) and
-- UPDATE(await_state,await_subscription_id,await_reason) to messaging_gateway, in gateway_role.sql (ANYM-590 L1).
-- A schema-91 runtime never names the columns and is refused by database_newer_than_runtime.
SET LOCAL lock_timeout='2s';
ALTER TABLE outgoing_messages
    ADD COLUMN IF NOT EXISTS await_reply jsonb
        CONSTRAINT outgoing_await_reply_shape CHECK (await_reply IS NULL OR coalesce(
            jsonb_typeof(await_reply)='object' AND await_reply ? 'within_s' AND await_reply-'within_s'='{}'::jsonb AND
            jsonb_typeof(await_reply->'within_s')='number' AND
            await_reply->'within_s' BETWEEN '60'::jsonb AND '1209600'::jsonb AND
            (await_reply->'within_s')=to_jsonb(trunc((await_reply->>'within_s')::numeric)),false)),
    ADD COLUMN IF NOT EXISTS await_state text
        CONSTRAINT outgoing_await_state_values CHECK (await_state IN ('pending','created','refused')),
    ADD COLUMN IF NOT EXISTS await_subscription_id text
        CONSTRAINT outgoing_await_subscription_shape CHECK (await_subscription_id IS NULL OR
            (await_state='created' AND length(await_subscription_id) BETWEEN 1 AND 128)),
    ADD COLUMN IF NOT EXISTS await_reason text
        CONSTRAINT outgoing_await_reason_values CHECK (await_reason IS NULL OR (await_state='refused' AND await_reason IN
            ('no_delivery_endpoint','ambiguous_delivery_endpoint','subscription_limit','permission_changed',
             'key_inactive','send_not_sent','subscription_refused')))
        -- The intent and its state exist together; a created subscription names itself, a refusal names its reason.
        -- Inline on the last new column, so a replay (ADD COLUMN IF NOT EXISTS) adds nothing and no DO block is needed.
        CONSTRAINT outgoing_await_state_pair CHECK (
            (await_reply IS NULL)=(await_state IS NULL) AND
            (await_state IS DISTINCT FROM 'created' OR await_subscription_id IS NOT NULL) AND
            (await_state IS DISTINCT FROM 'refused' OR await_reason IS NOT NULL));
CREATE INDEX IF NOT EXISTS outgoing_await_pending ON outgoing_messages(created_at) WHERE await_state='pending';
