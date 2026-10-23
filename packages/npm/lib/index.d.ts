export type ReplyReason = 'no_reply_needed' | 'spam_or_mailing' | 'handled_elsewhere' | 'other';
export type Json = null | boolean | number | string | Json[] | {[key: string]: Json};
export type Arguments = {[key: string]: Json};
/** Archive search; live (Telegram, first page only) also asks Telegram itself (ANYM-478). */
export type SearchArgs = Arguments & {
  /** off (default): archive only; auto: only when the archive answer is thin; on: always. */
  live?: 'off' | 'auto' | 'on';
  /** One wait for all live searches, 0-20 seconds (default 15). */
  wait_s?: number | null;
  /** Keep one per intent: a repeat queues no new provider request. */
  request_id?: string;
};
/** Reply-queue promotional filter (ANYM-427); exclude is the default for awaiting_reply=true. */
export type PromotionalFilter = 'exclude' | 'include' | 'only';
export type ListChatsArgs = Arguments & {awaiting_reply?: boolean; exclude_likely_broadcasts?: boolean;
  include_muted?: boolean; promotional?: PromotionalFilter};
export type ListChatsResult = Record<string, unknown> & {
  items: Array<Record<string, unknown> & {
    /** Compact conversation hint; likely_promotional and promotional_basis are absent when not promotional. */
    awaiting_reply?: boolean | (Record<string, unknown> & {likely_promotional?: true;
      promotional_basis?: 'business_sender' | 'repeated_days'});
    awaiting_reply_likely_promotional?: true;
    awaiting_reply_promotional_basis?: 'business_sender' | 'repeated_days'}>;
  next_cursor?: string | null;
  awaiting_reply_total?: number;
  /** Present only with promotional=exclude and awaiting_reply=true; absent means none. */
  excluded_promotional_total?: number;
  excluded_likely_broadcasts_total?: number;
  muted_total?: number};
export type MessageFormat = 'plain' | 'markdown' | 'html';
export interface MessageDraft {
  account: string;
  chat_id: string;
  text: string;
  reply_to?: string;
  format?: MessageFormat;
  link_preview?: boolean;
  allow_lossy?: boolean;
}
export interface MessageAction extends MessageDraft {
  request_id: string;
  preview_digest?: string;
  /** Bounded receipt wait, 0–25 seconds. Zero is omitted for older servers. */
  wait_s?: number;
}
export interface MessengerOperationPreview {
  account: string;
  operation: string;
  chat_id?: string;
  message_id?: string;
  /** Exact archived message revision; required by the selected message action. */
  expected_revision?: number;
  /** Strict allowlisted parameters from listMessengerOperations, never a raw RPC payload. */
  parameters?: Arguments;
}
export interface MessengerOperationAction extends MessengerOperationPreview {
  request_id: string;
  /** Required for mutations; binds payload but never proves owner approval. */
  preview_digest?: string;
}
export interface FileUploadPreparation {
  account: string;
  chat_id: string;
  /** Basename only, no URL or server path. */
  filename: string;
  /** MIME type without parameters, e.g. image/jpeg. */
  mime: string;
  /** 1–33554432 bytes. */
  bytes: number;
  /** Lowercase SHA-256 of the exact original bytes. */
  sha256: string;
  /** Stable across explicit reconciliation/resume; never generate another ID after uncertainty. */
  request_id: string;
}
export interface FileUploadChunk {
  upload_id: string;
  offset: number;
  /** Canonical padded base64 for 1–262144 raw bytes. */
  data_base64: string;
}
export interface FileUploadStatus {
  upload_id: string;
  account: string;
  chat_id: string;
  filename: string;
  mime: string;
  bytes: number;
  sha256: string;
  offset: number;
  status: 'uploading' | 'ready' | 'expired';
  chunk_bytes: number;
  expires_at: string;
  changes_provider_state: false;
  owner_approval_verified_by_server: false;
}
export interface LocalFileUploadOptions {
  account: string;
  chat_id: string;
  request_id: string;
  /** Defaults to the local basename. */
  filename?: string;
  /** Defaults to application/octet-stream; use image/jpeg or image/png for photos. */
  mime?: string;
  signal?: AbortSignal;
}
export type SubscriptionEventKind = 'message.created' | 'message.edited' | 'message.deleted' |
  'message.status' | 'attachment.ready' | 'file.text.ready' | 'file.transcript.ready' |
  'file.description.ready' | 'file.processing.failed';
export type SubscriptionOrigin = 'live' | 'backfill' | 'reconcile' | 'fixture';
export type SubscriptionWakeRule = 'incoming_message' | 'owner_mention' | 'reply_to_owner' | 'reply_to_confirmed_send';
export interface SubscriptionSelection {
  /** Frozen 1–64 authorized archive chat IDs, never names or arbitrary provider references. */
  chat_ids: string[];
  /** Exact verified account-scoped IDs in those chats, resolved through searchPeople. */
  sender_ids?: string[] | null;
  /** Defaults to message.created; file readiness is independent of message arrival. */
  kinds?: SubscriptionEventKind[] | null;
  direction?: '' | 'incoming' | 'outgoing';
  /** Defaults to live; historical origins require explicit selection. */
  origins?: SubscriptionOrigin[] | null;
  /** Correlation only, not authority to schedule another task. */
  label?: string;
  /** Future zoned ISO timestamp, at most 366 days away. */
  expires_at?: string | null;
  /** Native immutable evidence only; alternatives are ORed. */
  wake_rules?: SubscriptionWakeRule[] | null;
  /** This credential's confirmed sent reference; required by reply_to_confirmed_send. */
  reply_to_send?: string | null;
}
export interface SubscriptionLifecycleAction {
  subscription_id: string;
  version: number;
  action: 'pause' | 'resume' | 'close';
  resume_policy?: 'catch_up' | 'skip';
}
export interface SubscriptionPage {
  subscription_id: string;
  /** 1–100 references; default 50. Fetching never acknowledges them. */
  limit?: number;
}
export interface SubscriptionPageAck {
  subscription_id: string;
  /** Exact short-lived receipt issued by fetchSubscriptionEvents. */
  receipt: string;
  /** ack_version from the fetched page, not the subscription lifecycle version. */
  version: number;
}
export interface SubscriptionWait extends SubscriptionPage {
  /** 0–25 seconds; default 25. Explicit client deadlines still apply. */
  timeout?: number;
}
export interface SubscriptionDelivery {
  subscription_id: string;
  version: number;
  /** Opaque operator-registered UUID. Null or omission disables; never a URL. */
  receiver_id?: string | null;
}
export interface FeedbackReferences {
  /** At most 32 authorized archive IDs per reference type. */
  message_ids?: string[];
  chat_ids?: string[];
  subscription_ids?: string[];
}
export interface PrivateFeedback {
  request_id: string;
  category: 'bug' | 'feature' | 'quality' | 'other';
  /** 1–4000 characters; do not include credentials or private originals. */
  description: string;
  operation?: string | null;
  refs?: FeedbackReferences | null;
}
export declare const tools: readonly string[];
export declare class AnyMsgError extends Error {
  readonly code: string;
  readonly status: number;
  readonly requestId: string | null;
  readonly retryable: boolean;
}
export type CapabilitySection = 'identity' | 'accounts' | 'coverage' | 'tool_manifest' | 'search_options'
  | 'sending' | 'file_uploads' | 'subscriptions' | 'feedback' | 'messenger_operations' | 'limits';
export interface CapabilitiesArgs {
  detail?: 'summary' | 'full';
  sections?: CapabilitySection[];
}
export declare class AnyMsgClient {
  constructor(options: {baseUrl: string; token: string; timeoutMs?: number;
    maxResponseBytes?: number; fetch?: typeof globalThis.fetch});
  call<T = Record<string, unknown>>(method: string, arguments_?: Arguments, options?: {signal?: AbortSignal}): Promise<T>;
  /** The short summary by default; detail 'full' is the whole payload, sections only those blocks. */
  capabilities(args?: CapabilitiesArgs): Promise<Record<string, unknown>>;
  prepareFileUpload(args: FileUploadPreparation, options?: {signal?: AbortSignal}): Promise<FileUploadStatus>;
  uploadFileChunk(args: FileUploadChunk, options?: {signal?: AbortSignal}): Promise<FileUploadStatus>;
  getFileUpload(args: {upload_id:string}, options?: {signal?: AbortSignal}): Promise<FileUploadStatus>;
  /** Stream a local regular file, reject changes, and stage only. Never previews or sends. */
  uploadFile(file: string, options: LocalFileUploadOptions): Promise<FileUploadStatus>;
  dismissReply(args: {chat_id: string; message_id: string; expected_revision: number; reason?: ReplyReason; note?: string}): Promise<Record<string, unknown>>;
  snoozeReply(args: {chat_id: string; message_id: string; expected_revision: number; until: string; note?: string}): Promise<Record<string, unknown>>;
  /** Undo this credential's dismissal or snooze of the exact hint; after an uncertain result read listReplyDismissals instead of retrying. */
  restoreReply(args: {chat_id: string; message_id: string; expected_revision: number}): Promise<Record<string, unknown>>;
  muteReplySource(args: {chat_id?: string; account?: string; sender_id?: string; reason?: ReplyReason; note?: string}): Promise<Record<string, unknown>>;
  unmuteReplySource(args: {chat_id?: string; account?: string; sender_id?: string}): Promise<Record<string, unknown>>;
  listReplyDismissals(args: {kind?: 'dismissed' | 'snoozed' | 'muted'; after?: string; limit?: number}): Promise<Record<string, unknown>>;
  listChats(args?: ListChatsArgs): Promise<ListChatsResult>;
  search(args?: SearchArgs): Promise<Record<string, unknown>>;
  searchPeople(args?: Arguments): Promise<Record<string, unknown>>;
  searchFiles(args?: Arguments): Promise<Record<string, unknown>>;
  getMessages(args: Arguments): Promise<Record<string, unknown>>;
  getContext(args: Arguments): Promise<Record<string, unknown>>;
  getSyncStatus(args?: Arguments): Promise<Record<string, unknown>>;
  resolveReference(args: Arguments): Promise<Record<string, unknown>>;
  getAttachment(args: Arguments): Promise<Record<string, unknown>>;
  /** Schedule bounded retrieval of an existing original; requires download. Poll getAttachment passively. */
  requestFileDownload(args: {attachment_id: string}): Promise<Record<string, unknown>>;
  getFileText(args: Arguments): Promise<Record<string, unknown>>;
  getFileSegments(args: Arguments): Promise<Record<string, unknown>>;
  getFileOccurrences(args: Arguments): Promise<Record<string, unknown>>;
  getUpdates(args?: Arguments): Promise<Record<string, unknown>>;
  ackUpdates(args: Arguments): Promise<Record<string, unknown>>;
  getReviews(args: Arguments): Promise<Record<string, unknown>>;
  recordReview(args: Arguments): Promise<Record<string, unknown>>;
  /** Passive rendering. Its digest binds the payload but does not prove owner approval. */
  previewMessage(args: MessageDraft): Promise<Record<string,unknown>>;
  /** Rich content requires the reviewed preview digest and exact owner confirmation. */
  sendMessage(args: MessageAction): Promise<Record<string,unknown>>;
  getSendStatus(args: {send_id:string}): Promise<Record<string,unknown>>;
  /** Passive catalog and scoped permission read, with no provider request. */
  listMessengerOperations(args: {account:string}): Promise<Record<string,unknown>>;
  /** Passive validation; show exact targets, effects and warnings before owner confirmation. */
  previewMessengerOperation(args: MessengerOperationPreview): Promise<Record<string,unknown>>;
  /** Separate live-operation permissions and account opt-in; never retries submitted/unknown effects. */
  executeMessengerOperation(args: MessengerOperationAction): Promise<Record<string,unknown>>;
  /** Read a scoped receipt without another provider request. */
  getMessengerOperation(args: {operation_id:string}): Promise<Record<string,unknown>>;
  /** Requires explicit subscription-write; uncertain creations are never automatically retried. */
  createSubscription(args: SubscriptionSelection): Promise<Record<string,unknown>>;
  /** Current credential only; sources are masked after a permission/scope change. */
  listSubscriptions(args?: {limit?:number}): Promise<Record<string,unknown>>;
  /** Matcher catch-up is separate from live provider freshness and complete history. */
  getSubscription(args: {subscription_id:string}): Promise<Record<string,unknown>>;
  /** Version-checked local lifecycle operation; no messenger action or agent wake-up. */
  updateSubscription(args: SubscriptionLifecycleAction): Promise<Record<string,unknown>>;
  /** Passive matched-reference inspection, independent of the durable ACK position. */
  getSubscriptionEvents(args: {subscription_id:string; after?:number; limit?:number}): Promise<Record<string,unknown>>;
  /** Fetch an unacknowledged page; receipt issuance requires explicit subscription-ack. */
  fetchSubscriptionEvents(args: SubscriptionPage, options?: {signal?:AbortSignal}): Promise<Record<string,unknown>>;
  /** ACK one issued page; no provider read-state change or automatic retry. */
  ackSubscriptionEvents(args: SubscriptionPageAck, options?: {signal?:AbortSignal}): Promise<Record<string,unknown>>;
  /** Bounded wait with reauthorization, no implicit ACK or provider polling. */
  waitForSubscriptionEvents(args: SubscriptionWait, options?: {signal?:AbortSignal}): Promise<Record<string,unknown>>;
  /** Select a registered receiver only. Receiver success never proves a host turn. */
  configureSubscriptionDelivery(args: SubscriptionDelivery, options?: {signal?:AbortSignal}): Promise<Record<string,unknown>>;
  /** Queue a synthetic hint without creating an archive event or acknowledging it. */
  testSubscription(args: {subscription_id:string; request_id:string}, options?: {signal?:AbortSignal}): Promise<Record<string,unknown>>;
  /** Store local private feedback; never forwards or publishes the description. */
  submitFeedback(args: PrivateFeedback, options?: {signal?:AbortSignal}): Promise<Record<string,unknown>>;
  /** Only the current credential's records, without former source references. */
  listFeedback(args?: {limit?:number}, options?: {signal?:AbortSignal}): Promise<Record<string,unknown>>;
}
