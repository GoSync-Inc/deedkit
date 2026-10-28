export type ToolName =
  | 'calendar_agenda'
  | 'calendar_apply'
  | 'calendar_assignment'
  | 'calendar_assignments'
  | 'calendar_calendars'
  | 'calendar_capabilities'
  | 'calendar_find_time'
  | 'calendar_members'
  | 'calendar_operation'
  | 'calendar_plan'
  | 'calendar_schedule'
  | 'calendar_slots'
  | 'calls_call'
  | 'calls_call_result'
  | 'calls_capabilities'
  | 'calls_dial_call'
  | 'calls_get_call_analysis'
  | 'calls_get_call_result'
  | 'calls_get_call_status'
  | 'calls_get_owner_requests'
  | 'calls_list_briefs'
  | 'calls_list_calls'
  | 'calls_preflight_call'
  | 'calls_prepare_call'
  | 'calls_prepare_external_call'
  | 'calls_prepare_followup'
  | 'calls_raise_call_cap'
  | 'calls_start_call'
  | 'calls_stop_call'
  | 'messages_ack_subscription_events'
  | 'messages_ack_updates'
  | 'messages_capabilities'
  | 'messages_change_contacts'
  | 'messages_change_message'
  | 'messages_configure_subscription_delivery'
  | 'messages_create_subscription'
  | 'messages_delete_delivery_endpoint'
  | 'messages_dismiss_reply'
  | 'messages_execute_messenger_operation'
  | 'messages_fetch_subscription_events'
  | 'messages_get_attachment'
  | 'messages_get_context'
  | 'messages_get_file_occurrences'
  | 'messages_get_file_segments'
  | 'messages_get_file_text'
  | 'messages_get_file_upload'
  | 'messages_get_messages'
  | 'messages_get_messenger_operation'
  | 'messages_get_reviews'
  | 'messages_get_send_status'
  | 'messages_get_subscription'
  | 'messages_get_subscription_events'
  | 'messages_get_sync_status'
  | 'messages_get_updates'
  | 'messages_list_chats'
  | 'messages_list_delivery_endpoints'
  | 'messages_list_feedback'
  | 'messages_list_messenger_operations'
  | 'messages_list_reply_dismissals'
  | 'messages_list_subscriptions'
  | 'messages_manage_chat'
  | 'messages_mute_reply_source'
  | 'messages_prepare_file_upload'
  | 'messages_preview_message'
  | 'messages_preview_messenger_operation'
  | 'messages_read_contacts'
  | 'messages_record_review'
  | 'messages_register_delivery_endpoint'
  | 'messages_request_file_download'
  | 'messages_resolve_reference'
  | 'messages_restore_reply'
  | 'messages_search'
  | 'messages_search_files'
  | 'messages_search_people'
  | 'messages_send'
  | 'messages_send_message'
  | 'messages_snooze_reply'
  | 'messages_submit_feedback'
  | 'messages_test_delivery_endpoint'
  | 'messages_test_subscription'
  | 'messages_unmute_reply_source'
  | 'messages_update_delivery_endpoint'
  | 'messages_update_subscription'
  | 'messages_upload_file_chunk'
  | 'messages_wait_for_subscription_events'
  | 'platform_capabilities'
  | 'platform_resolve_target'
  | 'platform_select_modules';

export interface ToolCatalogEntry {
  readonly name: ToolName;
  /** From the gateway catalog's readOnlyHint; any other tool may change state. */
  readonly readOnly: boolean;
}

export declare const tools: readonly ToolName[];
export declare const toolCatalog: readonly ToolCatalogEntry[];
export declare const UNKNOWN_OUTCOME: 'mutation_outcome_unknown';

export interface DeedKitClientOptions {
  /** Gateway public root (or proxy prefix), without /mcp or /v1. HTTPS except on loopback. */
  baseUrl: string;
  /** The agent's gateway key: 1 to 512 visible characters. */
  token: string;
  /** Per-request deadline; for downloads, the deadline for each wait on the server. Default 45000. */
  timeoutMs?: number;
  /** Largest accepted JSON response. Default 16 MiB, at most 32 MiB. */
  maxResponseBytes?: number;
  fetch?: typeof globalThis.fetch;
}

export interface CallOptions {
  signal?: AbortSignal;
}

export interface DownloadGrant {
  download_url: string;
  bytes: number;
  sha256: string;
  filename?: string;
  [key: string]: unknown;
}

export interface DownloadResult {
  path: string;
  bytes: number;
  sha256: string;
  /** Set only when the published file's staging link could not be removed. */
  staging_left?: string;
}

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type ToolArguments = { [key: string]: JsonValue };

export declare class DeedKitError extends Error {
  readonly name: 'DeedKitError';
  /** The gateway error code as returned, or a client code such as transport_error. */
  readonly code: string;
  readonly status: number;
  readonly tool: ToolName | null;
  /** 'unknown' means a mutation may already have happened: never retry it. */
  readonly outcome: 'rejected' | 'unknown';
  readonly unknownOutcome: boolean;
  readonly retryable: boolean;
  readonly automaticRetry: false;
  readonly requestId: string | null;
  readonly gatewayRequestId: string | null;
  readonly nextAction: string | null;
  readonly retryAfterSeconds: number | null;
  readonly operationSent: boolean | null;
  readonly phase: string | null;
  readonly reason: string | null;
  readonly backendError: string | null;
  readonly backendStatus: number | null;
  /** Downloads only: a staging file of this call that could not be removed. */
  readonly stagingLeft: string | null;
  toJSON(): Record<string, string | number | boolean | null>;
}

export declare class DeedKitClient {
  constructor(options: DeedKitClientOptions);
  static isReadOnly(tool: ToolName): boolean;
  call(tool: ToolName, args?: ToolArguments, options?: CallOptions): Promise<Record<string, unknown>>;
  platformCapabilities(options?: CallOptions): Promise<Record<string, unknown>>;
  /** Stream a grant into a private staging file, verify length and SHA-256, then link it to a new output name. */
  downloadFile(grant: DownloadGrant, output: string, options?: CallOptions): Promise<DownloadResult>;
  downloadAttachment(args: { attachment_id: string }, output: string, options?: CallOptions):
    Promise<DownloadResult & { filename: string | null }>;
}
