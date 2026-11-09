-- rollout: manual
-- ANYM-837: an agent answers an archived WhatsApp bot menu with the bot's own structured choice. One nullable
-- column on outgoing_messages names the answered menu (archive and provider message IDs, its menu type); it holds
-- no option text: the option ID and label live in body, source_text and render_artifact, which the purge and
-- purge_publication_guard (045) already scrub. A menu choice is always a reply to its menu.
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": ADD COLUMN, nullable, no default,
-- with a CHECK on the new column only; no row is read, rewritten or deleted). Manual: the rollout grammar has no
-- expand form for a column with a CHECK. The grant INSERT(menu_choice) to messaging_gateway is in gateway_role.sql.
-- A schema-69 runtime never names the column; its dispatch_payload refuses a menu row by the artifact's eleventh key.
SET LOCAL lock_timeout='2s';
ALTER TABLE outgoing_messages ADD COLUMN IF NOT EXISTS menu_choice jsonb
    -- coalesce: a predicate that evaluates to NULL (a JSON null, a missing key) fails instead of passing.
    CONSTRAINT outgoing_menu_choice_shape CHECK (menu_choice IS NULL OR coalesce(
        jsonb_typeof(menu_choice)='object' AND reply_provider_id IS NOT NULL AND
        menu_choice ?& ARRAY['message_id','provider_message_id','menu_type'] AND
        menu_choice-'message_id'-'provider_message_id'-'menu_type'='{}'::jsonb AND
        jsonb_typeof(menu_choice->'message_id')='string' AND jsonb_typeof(menu_choice->'provider_message_id')='string' AND
        jsonb_typeof(menu_choice->'menu_type')='string' AND
        menu_choice->>'menu_type' IN ('list','buttons','interactive','template'),false));
