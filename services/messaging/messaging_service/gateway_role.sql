-- Run as owner. No password here. Assign LOGIN membership separately using secrets.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_gateway') THEN
        CREATE ROLE messaging_gateway NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO messaging_gateway;
GRANT SELECT ON probe_metadata,accounts,principals,api_keys,chats,messages,attachments,
    events,feed_counter,source_status,media_jobs,message_revisions TO messaging_gateway;
GRANT SELECT,INSERT,UPDATE,DELETE ON consumers,deliveries,download_grants TO messaging_gateway;
GRANT SELECT ON reviews,review_sources TO messaging_gateway;
GRANT SELECT ON sender_profiles TO messaging_gateway;
GRANT SELECT ON attachment_text TO messaging_gateway;
GRANT INSERT(position,principal,request_id,message,revision,result_ref) ON reviews TO messaging_gateway;
GRANT INSERT(review,message,revision) ON review_sources TO messaging_gateway;
GRANT SELECT,UPDATE(value) ON review_counter TO messaging_gateway;
GRANT INSERT(principal,key_id,operation,outcome,error_code,resource_ids) ON audit_log TO messaging_gateway;
-- Audit rows are append-only to the gateway. No SELECT/UPDATE/DELETE; no grants
-- on credentials, ACL mutation, DDL, provider sessions or collector ownership.
GRANT SELECT ON message_metadata,attachment_enrichment,collector_health,collector_chats,whatsapp_checkpoints TO messaging_gateway;

GRANT SELECT ON attachment_text_chunks,attachment_enrichment_chunks TO messaging_gateway;
GRANT SELECT ON message_conflicts TO messaging_gateway;

GRANT SELECT ON processing_profiles TO messaging_gateway;
GRANT SELECT(account,expires_at) ON collector_leases TO messaging_gateway;
GRANT SELECT ON whatsapp_identity_observations TO messaging_gateway;
GRANT SELECT ON account_policies TO messaging_gateway;
GRANT SELECT ON outgoing_messages TO messaging_gateway;
GRANT SELECT(epoch) ON collector_leases TO messaging_gateway;
GRANT INSERT(principal,key_id,scope_version,key_permission_version,request_id,request_hash,account,chat,
    provider_chat,body,reply_provider_id,source_text,source_format,render_artifact,render_policy,preview_digest) ON outgoing_messages TO messaging_gateway;
GRANT SELECT ON message_receipts,receipt_event_jobs,receipt_event_publications TO messaging_gateway;
GRANT SELECT ON messenger_commands TO messaging_gateway;
GRANT INSERT(principal,key_id,scope_version,key_permission_version,request_id,request_hash,
    account,chat,message,expected_revision,operation,permission,mutation,payload,preview_digest)
    ON messenger_commands TO messaging_gateway;
-- ANYM-546: messages_send preview records (unbound ones age out) and composite parent records.
GRANT SELECT,INSERT,UPDATE,DELETE ON send_previews TO messaging_gateway;
GRANT SELECT,INSERT,UPDATE ON send_intents TO messaging_gateway;
-- Definitions are credential-bound in the shared application authorization.
-- Matching is a separate least-privilege worker, never a gateway write path.
GRANT SELECT,INSERT,UPDATE ON subscriptions TO messaging_gateway;
GRANT SELECT,INSERT ON subscription_sources TO messaging_gateway;
GRANT SELECT ON subscription_matches TO messaging_gateway;
GRANT SELECT ON chat_read_observations TO messaging_gateway;
GRANT SELECT ON owner_message_reads TO messaging_gateway;
GRANT SELECT,INSERT,UPDATE,DELETE ON reply_dismissals TO messaging_gateway;
GRANT SELECT,INSERT,UPDATE,DELETE ON reply_snoozes TO messaging_gateway;
GRANT SELECT,INSERT,DELETE,UPDATE(reason,note,updated_at) ON reply_mutes TO messaging_gateway;
GRANT SELECT,INSERT,UPDATE ON file_uploads TO messaging_gateway;
GRANT SELECT ON media_reservations TO messaging_gateway;
-- Schedule only existing originals; no state, source, attempt or lease mutation.
GRANT UPDATE(requested_until,request_key_digest) ON media_jobs TO messaging_gateway;

-- Separate subscription consumption, local feedback and opaque receiver binding.
GRANT SELECT,INSERT,UPDATE ON subscription_consumption TO messaging_gateway;
GRANT SELECT,INSERT,UPDATE,DELETE ON subscription_receipts TO messaging_gateway;
GRANT SELECT(id,principal,key_id,enabled) ON subscription_receivers TO messaging_gateway;
GRANT SELECT,INSERT,UPDATE,DELETE ON subscription_delivery TO messaging_gateway;
GRANT SELECT,INSERT ON subscription_outbox TO messaging_gateway;
GRANT UPDATE(state,finished_at,error_code) ON subscription_outbox TO messaging_gateway;
-- Schema 38: inspect held hints; a same-receiver reconfiguration carries them to its version.
GRANT SELECT ON subscription_hint_windows TO messaging_gateway;
GRANT UPDATE(subscription_version,due_at,silence_state,silence_fired_at,silence_due_at,
    silence_held_reason,updated_at) ON subscription_hint_windows TO messaging_gateway;
-- Schema 39: submission columns only; disposition fields are the operator's (ANYM-343).
GRANT SELECT ON agent_feedback TO messaging_gateway;
REVOKE INSERT,UPDATE ON agent_feedback FROM messaging_gateway;
GRANT INSERT(principal,key_id,request_id,request_hash,category,description,operation,refs)
    ON agent_feedback TO messaging_gateway;
-- Managed originals and descriptions are separate from immutable receipts.
GRANT SELECT ON attachment_residency,attachment_eviction_journal,
    attachment_understanding,file_understanding_profiles TO messaging_gateway;
GRANT EXECUTE ON FUNCTION public.touch_original(text,bigint) TO messaging_gateway;
GRANT EXECUTE ON FUNCTION public.queue_managed_original(text) TO messaging_gateway;
-- Cold-first rehydration (ANYM-350): queue a restore of a verified R2 copy.
-- Without these grants the request path keeps the provider re-download.
GRANT SELECT ON attachment_cold_copies TO messaging_gateway;
GRANT UPDATE(restore_state,restore_attachment,restore_requested_at,restore_finished_at,restore_error)
    ON attachment_cold_copies TO messaging_gateway;
-- Owner-made person links (ANYM-283): read only; the owner CLI writes them.
GRANT SELECT ON person_links TO messaging_gateway;
-- Deletion status in get_sync_status (ANYM-511): content-free purge marks and chat cutoffs.
GRANT SELECT ON message_purges,chat_tombstones TO messaging_gateway;
-- Schema 48: the hint dispatcher's liveness row (ANYM-518), read by capabilities.
GRANT SELECT ON subscription_dispatcher_heartbeat TO messaging_gateway;
-- Schema 49 (ANYM-519): self-service endpoints. Header values and signing secrets are write-only here:
-- INSERT and UPDATE, never SELECT (no RETURNING or SET reads them either); only the dispatcher reads them.
-- signing_secret UPDATE exists only to clear it on delete. Privilege separation, not encryption.
GRANT SELECT(id,principal,key_id,request_id,request_hash,url,host,label,header_names,headers_fingerprint,
    secret_fingerprint,pending_fingerprint,rotation_started_at,state,version,created_at,updated_at)
    ON delivery_endpoints TO messaging_gateway;
GRANT INSERT(principal,key_id,request_id,request_hash,url,host,label,header_names,headers_fingerprint,
    secret_fingerprint,header_values,signing_secret) ON delivery_endpoints TO messaging_gateway;
GRANT UPDATE(header_names,headers_fingerprint,header_values,pending_secret,pending_fingerprint,rotation_started_at,
    signing_secret,state,version,updated_at) ON delivery_endpoints TO messaging_gateway;
GRANT SELECT,INSERT ON delivery_endpoint_tests TO messaging_gateway;
GRANT UPDATE(state,finished_at,error_code) ON delivery_endpoint_tests TO messaging_gateway;
-- Schema 50 (ANYM-534): only this component's heartbeat row (row-level security).
GRANT SELECT,INSERT,UPDATE ON component_heartbeats TO messaging_gateway;
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_policies WHERE tablename='component_heartbeats' AND policyname='messaging_gateway_beat') THEN
        CREATE POLICY messaging_gateway_beat ON component_heartbeats TO messaging_gateway USING (component='api') WITH CHECK (component='api');
    END IF;
END $$;
-- Schema 53 (ANYM-548): the owner's preferred channel per person and purpose, read only; the owner CLI writes it.
GRANT SELECT ON person_preferences TO messaging_gateway;
-- Schema 57 (ANYM-562): read module streams and pages, ACK the module position, re-arm a held stream.
GRANT SELECT ON module_events,module_event_counter,subscription_module_sources,subscription_module_matches,
    module_source_health TO messaging_gateway;
GRANT SELECT,INSERT ON subscription_module_streams TO messaging_gateway;
GRANT UPDATE(matched_through,acked_through,gap,blocked_reason,rollback_hold) ON subscription_module_streams
    TO messaging_gateway;
