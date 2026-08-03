export type RuntimeMode = "connecting" | "live" | "mock";
export type MainView = "start" | "ask";
export type CardStatus = "pending" | "snoozed" | "resolved" | "dismissed" | string;
export type InboxThreadStateOperation = "mark_read" | "mark_unread" | "star" | "unstar" | "mark_important" | "mark_not_important" | "trash" | "untrash";
export type ResultFilter = "all" | "reply" | "review" | "cleanup";
export type LlmProvider = "anna-llm" | "dashscope";
export type StorageProvider = "aps" | "local";
export type LlmStatusValue = "unknown" | "checking" | "connected" | "unavailable" | "error";
export type ConnectivityStatusValue = LlmStatusValue;
export type DraftLength = "Brief" | "Standard" | "Detailed";
export type DraftWritingStyle = "Natural" | "Polished" | "Plain-spoken" | "Executive" | "Persuasive";
export type DraftTone = "Warm" | "Direct" | "Diplomatic" | "Enthusiastic" | "Calm" | "Apologetic";
export type DraftMood = "Confident" | "Grateful" | "Supportive" | "Neutral" | "Urgent";
export type DraftPreferenceField = "length" | "writingStyle" | "tone" | "mood";
export type DraftReplyGoal = "Accept" | "Decline" | "Ask for info" | "Follow up" | "Schedule";

export interface DraftPreferences {
  length: DraftLength;
  writingStyle: DraftWritingStyle;
  tone: DraftTone;
  mood: DraftMood;
}

export interface DraftReplyIntent {
  goal?: DraftReplyGoal;
  userTake?: string;
}

export const DRAFT_LENGTH_OPTIONS: DraftLength[] = ["Brief", "Standard", "Detailed"];
export const DRAFT_WRITING_STYLE_OPTIONS: DraftWritingStyle[] = ["Natural", "Polished", "Plain-spoken", "Executive", "Persuasive"];
export const DRAFT_TONE_OPTIONS: DraftTone[] = ["Warm", "Direct", "Diplomatic", "Enthusiastic", "Calm", "Apologetic"];
export const DRAFT_MOOD_OPTIONS: DraftMood[] = ["Confident", "Grateful", "Supportive", "Neutral", "Urgent"];
export const DRAFT_REPLY_GOAL_OPTIONS: DraftReplyGoal[] = ["Accept", "Decline", "Ask for info", "Follow up", "Schedule"];
export const DEFAULT_DRAFT_PREFERENCES: DraftPreferences = {
  length: "Standard",
  writingStyle: "Natural",
  tone: "Warm",
  mood: "Confident",
};

export interface LlmStatus {
  status: LlmStatusValue;
  checked: boolean;
  message?: string;
  elapsed_ms?: number;
}

/** Gmail API 连通性与延迟（users/me/profile RTT） */
export interface GmailApiStatus {
  status: ConnectivityStatusValue;
  checked: boolean;
  message?: string;
  elapsed_ms?: number;
  mailbox?: string;
}

/** 仅包含阶段、耗时和错误类型的调用链摘要；不含邮件或凭据。 */
export interface RuntimeDiagnosticSpan {
  stage: string;
  elapsed_ms: number;
  outcome: "ok" | "error";
  cached?: boolean;
  code?: string | number;
  endpoint?: string;
  error_type?: string;
  http_status?: number;
  source?: string;
}

export interface RuntimeDiagnostics {
  trace_id: string;
  operation: string;
  elapsed_ms: number;
  spans: RuntimeDiagnosticSpan[];
}

export interface MailboxInfo {
  email: string;
  display_name?: string;
  avatar_url?: string;
  provider?: "gmail" | "outlook" | string;
  auth_source?: string;
  authorized?: boolean;
  selected?: boolean;
  last_auth_checked_at?: string;
  last_scan_at?: string;
  last_scan_status?: string;
  last_error?: string;
  card_count?: number;
}

export interface MailboxCredentialsStatus {
  available: boolean;
  code: "ok" | "not_checked" | "not_granted" | "protocol_unsupported" | "unavailable";
  message: string;
  action: "none" | "retry" | "enable_connected_accounts" | "upgrade_runtime";
}

export interface MailboxListPayload {
  mailboxes: MailboxInfo[];
  selected: string[];
  discovered?: MailboxInfo[];
  credentials_status?: MailboxCredentialsStatus;
}

export interface RuntimeState {
  connected: boolean;
  mode: RuntimeMode;
  error?: string;
  client?: AnnaRuntimeClient;
}

export interface AnnaRuntimeClient {
  tools?: { invoke?: (args: ToolInvokeArgs, options?: { timeoutMs?: number; signal?: AbortSignal }) => Promise<unknown> };
  llm?: { complete?: (args: unknown, options?: { timeoutMs?: number; signal?: AbortSignal }) => Promise<unknown> };
  window?: { set_title?: (args: { title: string }) => Promise<unknown> };
  call?: (ns: string, method: string, args?: unknown, options?: { timeout?: number; timeoutMs?: number; signal?: AbortSignal }) => Promise<unknown>;
  dispose?: () => void;
}

export interface ToolInvokeArgs {
  tool_id: string;
  method: string;
  args: Record<string, unknown>;
  timeoutMs?: number;
}


export interface FrontendCardAction {
  id: string;
  label?: string;
  buttonLabel?: string;
  primary?: boolean;
  statusTitle?: string;
  status?: string;
}

export interface GapQuestion {
  id: string;
  question: string;
  hint?: string;
  required?: boolean;
}

export interface ReplyGaps {
  needs_user_input: boolean;
  summary?: string;
  questions: GapQuestion[];
}

export interface FrontendCard {
  uiKey?: string;
  id: string;
  title?: string;
  summary?: string;
  recommendation?: string;
  label?: string;
  priority?: string;
  item_type?: string;
  draft_reply?: string;
  thread_summary?: string;
  displaySection?: "main" | "lower" | string;
  details?: {
    needs?: string;
    latestActivity?: string;
    reviewed?: string;
    mailbox?: string;
  };
  original?: {
    source?: string;
    thread?: string;
    from?: string;
    to?: string;
    cc?: string;
    time?: string;
    status?: string;
    body?: string;
  };
  actions?: FrontendCardAction[];
  status?: CardStatus;
  resolution?: string;
  snooze_until?: string;
  resolved_at?: string;
  userAction?: "reply" | "review" | "cleanup" | string;
  cardType?: "cleanup_bundle" | string;
  bundledMessages?: CleanupMessage[];
  bundledCount?: number;
  replyGaps?: ReplyGaps;
  gmailState?: Record<string, unknown>;
  attachments?: MailAttachmentMeta[];
}

export interface InboxMessage {
  id: string;
  thread_id?: string;
  mailbox?: string;
  internal_date?: string;
  date?: string | null;
  from?: string | null;
  to?: string | null;
  subject?: string | null;
  latest_subject?: string | null;
  snippet?: string | null;
  body_preview?: string | null;
  draft_body?: string | null;
  draft_local?: boolean;
  label_ids?: string[];
  unread?: boolean;
  important?: boolean;
  starred?: boolean;
  has_attachment?: boolean;
  attachment_count?: number;
  body_cached?: boolean;
}

export interface InboxEmailDetailPayload {
  mailbox?: string;
  message?: InboxMessage & { body_text?: string | null; body_truncated?: boolean };
}

export interface InboxSyncBoundary {
  priority_days?: number;
  cache_total?: number;
  initial_sync_complete?: boolean;
  backfill_complete?: boolean;
  sync_stage?: string;
}

export interface InboxFeedPayload {
  mailbox?: string;
  days?: number;
  category?: string;
  query?: string;
  count?: number;
  offset?: number;
  next_offset?: number;
  has_more?: boolean;
  source?: "cache" | "gmail";
  page_token?: string;
  page_offset?: number;
  sync_boundary?: InboxSyncBoundary;
  messages: InboxMessage[];
  updated_at?: string;
}

export interface InboxCacheSyncPayload {
  mailbox?: string;
  mode?: "history" | "baseline_required" | "history_expired";
  history_id?: string;
  added?: number;
  updated?: number;
  deleted?: number;
  cache_total?: number;
  resync_required?: boolean;
  resync_reason?: "cursor_missing" | "history_expired";
  updated_at?: string;
}

export interface ContactAvatarPayload {
  mailbox?: string;
  avatars: Record<string, string>;
  permission_required?: boolean;
  required_scope?: string;
  required_scopes?: string[];
  service_disabled?: boolean;
  service?: string;
  activation_url?: string;
  warning?: string;
}

export interface CleanupMessage {
  id?: string;
  message_id?: string;
  mailbox?: string;
  from_addr?: string;
  subject?: string;
  snippet?: string;
  date?: string;
  item_type?: string;
  reason?: string;
}

export interface ScanState {
  last_scan_ts?: string;
  last_message_internal_date?: string;
  total_scans?: number;
  total_processed?: number;
}

export interface ActiveCardsPayload {
  cards: FrontendCard[];
  count?: number;
  total?: number;
  has_more?: boolean;
  offset?: number;
  limit?: number;
  action_count?: number;
  scan_state?: ScanState;
  cleanup_bundle?: CleanupMessage[] | null;
  cleanup_total?: number;
  cleanup_has_more?: boolean;
}

export interface CleanupBundlePayload {
  items: CleanupMessage[];
  total?: number;
  offset?: number;
  limit?: number;
  has_more?: boolean;
  error?: string;
}

export interface RunStatus {
  success?: boolean;
  run_id?: string;
  status?: "queued" | "running" | "done" | "failed" | string;
  stage?: string;
  progress?: Record<string, unknown>;
  partial?: Record<string, unknown>;
  warnings?: RunWarning[];
  result?: Record<string, unknown>;
  cards?: FrontendCard[] | null;
  scan_state?: ScanState | null;
  started_at?: string;
  updated_at?: string;
  needs_continue?: boolean;
  cards_added?: number;
  cards_version?: number;
  error?: string;
  diagnostics?: RuntimeDiagnostics;
}

export interface RunWarning {
  stage: string;
  at: string;
  detail: Record<string, unknown>;
}

export interface ScanPlan {
  mailbox?: string;
  scan_window_days?: number;
  max_messages?: number;
  scan_categories?: string[];
  updated_at?: string;
}

export interface RunHistoryEntry {
  run_id?: string;
  mailbox?: string;
  ts?: string;
  request?: string;
  mode?: string;
  strategy?: string;
  plan_id?: string;
  result?: string;
  summary?: string;
  // card-action fields
  entry_type?: string;       // "scan" | "card_action"
  card_id?: string;
  card_title?: string;
  action?: string;            // "read" | "snooze" | "reply" | "handled_manually" | "no_action_needed" | "cleanup_read" | "restore"
  detail?: string;
  // card context for rendering history entries
  card_summary?: string;
  card_from?: string;
  card_subject?: string;
  card_body?: string;
}

export interface CustomPlanSummary {
  plan_id: string;
  user_request?: string;
  title?: string;
  description?: string;
  gmail_queries?: CustomPlanQuery[];
  read_depth?: string;
  created_at?: string;
  last_used_at?: string;
  use_count?: number;
  last_result_summary?: string;
}

export interface CustomPlanQuery {
  query?: string;
  purpose?: string;
}

export interface CustomRunResultItem {
  subject?: string;
  context?: string;
  suggestion?: string;
  draft?: string;
  reply_gaps?: ReplyGaps;
  mailbox?: string;
  message_id?: string;
  thread_id?: string;
  from?: string;
  mail_links?: AskMailLink[];
}

/** 当前邮箱的 Inbox 展示偏好；由 Executa 按邮箱地址独立持久化。 */
/** LLM 连通性探测轮询间隔（秒）；0 表示关闭自动轮询 */
export type LlmStatusPollSeconds = 0 | 30 | 60 | 120 | 300;
export type InboxAutoSyncSeconds = 0 | 5 | 15 | 30 | 60;

export interface InboxSettings {
  mailbox: string;
  display_range_days: 7 | 30 | 60;
  time_section_mode: "detailed" | "recent_then_months" | "months_only";
  stars_enabled: boolean;
  stars_limit: number;
  todos_enabled: boolean;
  todos_limit: number;
  llm_status_poll_seconds: LlmStatusPollSeconds;
  auto_sync_seconds: InboxAutoSyncSeconds;
  custom_categories: InboxCustomCategory[];
  updated_at?: string;
}

export interface InboxCustomCategory {
  id: string;
  name: string;
  query: string;
  hide_when_empty: boolean;
  bundling_behavior: "default" | "by_sender" | "none";
}

export interface InboxSettingsPayload {
  settings: InboxSettings;
  etag?: string;
}

export interface AskMailLink {
  label: string;
  mailbox: string;
  thread_id: string;
  message_id: string;
  from?: string;
  date?: string;
  snippet?: string;
}

export interface CustomRunResultSection {
  heading?: string;
  body?: string;
  items?: CustomRunResultItem[];
}

/** 终端用户可见的 Sampling 预算与消耗摘要；不含邮件、提示词或凭据。 */
export interface SamplingUsageSummary {
  grant?: { max_calls?: number; max_tokens_total?: number; max_tokens_per_call?: number };
  reserved?: { calls?: number; tokens?: number };
  usage?: { input_tokens?: number; output_tokens?: number; total_tokens?: number };
  remaining_reservation_tokens?: number;
  remaining_calls?: number;
  failed_calls?: number;
  last_error?: string;
}

export interface CustomRunResult {
  runId?: string;
  planId?: string;
  plan_title?: string;
  plan_description?: string;
  plan_gmail_queries?: CustomPlanQuery[];
  plan_read_depth?: string;
  title?: string;
  summary?: string;
  sections?: CustomRunResultSection[];
  sampling?: SamplingUsageSummary;
  trace?: Record<string, unknown>;
  planner_fallback?: boolean;
  /** 本轮 cache-only 检索使用的本地 query（与主搜索框语法一致） */
  scan_query?: string;
  scan_source?: "cache" | string;
}

export interface MailAttachmentMeta {
  id: string;
  message_id: string;
  filename: string;
  mime_type: string;
  size: number;
  source: "gmail" | string;
  downloadable: boolean;
}

export interface AttachmentDownloadPayload {
  ok?: boolean;
  delivery?: "url" | "inline" | string;
  mode?: "preview" | "download" | string;
  filename?: string;
  mime_type?: string;
  size?: number;
  download_url?: string;
  preview_url?: string;
  storage_key?: string;
  content_b64?: string;
  expires_at?: string;
  error?: string;
  message_id?: string;
  attachment_id?: string;
}

export interface ThreadContextMessage {
  message_id?: string;
  from?: string;
  to?: string;
  cc?: string;
  date?: string;
  subject?: string;
  body?: string;
}

export interface ThreadContextPayload {
  thread_id?: string;
  message_count?: number;
  from?: string;
  to?: string;
  cc?: string;
  subject?: string;
  latest_time?: string;
  messages?: ThreadContextMessage[];
  returned_count?: number;
  has_more_messages?: boolean;
  next_before_index?: number | null;
}

export interface CardDetailPayload {
  card?: FrontendCard;
  thread_context?: ThreadContextPayload;
  contact_context?: Record<string, unknown>;
  latest_body?: string;
  latest_body_html?: string;
  body_loaded?: boolean;
  attachments?: MailAttachmentMeta[];
}

export interface InboxThreadMessage {
  id: string;
  thread_id: string;
  internal_date: string;
  from: string;
  to: string;
  cc?: string;
  bcc?: string;
  subject: string;
  label_ids: string[];
  body_text?: string;
  body_html?: string;
  body_truncated?: boolean;
  attachments: MailAttachmentMeta[];
}

export interface InboxThreadPagePayload {
  mailbox: string;
  thread_id: string;
  subject: string;
  latest_subject?: string;
  messages: InboxThreadMessage[];
  returned_count: number;
  has_earlier: boolean;
  next_before_index: number | null;
  latest_message_id: string;
}

export interface InboxMessageDisplayBodyPayload {
  mailbox: string;
  message_id: string;
  thread_id: string;
  body_text?: string;
  body_html?: string;
  /** 超大 HTML 的短期 loopback 地址；前端读取后不写入浏览器缓存。 */
  body_url?: string;
  body_truncated?: boolean;
}

export interface QuickReplySuggestion {
  id: string;
  label: string;
  intent: string;
}

export interface InboxThreadAssistPayload {
  thread_id: string;
  latest_message_id: string;
  /** 生成摘要使用的界面语言（zh / en） */
  locale?: string;
  overview: string;
  /** 是否需要用户回复；false 时展示 no_reply_reason，不展示快捷 draft 提示 */
  needs_reply?: boolean;
  /** 无需回复时的原因说明（与 overview 同语言） */
  no_reply_reason?: string;
  quick_replies: QuickReplySuggestion[];
  summary?: Record<string, unknown>;
  related_context?: string[];
  cached?: boolean;
  fallback_used?: boolean;
}

export interface AiGmailThreadContextRef {
  kind: "gmail_thread";
  mailbox: string;
  thread_id: string;
  anchor_message_id: string;
  latest_message_id: string;
}

export interface AiComposeContextRef {
  kind: "compose";
  session_id: string;
  mailbox: string;
  recipients: string[];
  subject: string;
  body: string;
}

export type AiMailContextRef = AiGmailThreadContextRef | AiComposeContextRef;

export interface SubmitMailPromptRequest {
  visiblePrompt: string;
  context: AiMailContextRef;
  expectedArtifact?: "draft_reply" | "summary" | "send_plan" | "compose_draft";
  contextTitle?: string;
  forceNewConversation?: boolean;
  userAnswers?: Record<string, string>;
  draftToRevise?: string;
  baseMessages?: AiChatMessage[];
  retryUserMessage?: AiChatMessage;
  draftComposerMode?: "reply" | "forward";
}

export interface DraftReplyArtifact {
  type: "draft_reply";
  mailbox: string;
  thread_id: string;
  body: string;
  source_prompt: string;
  /** 草稿要写入当前线程的回复或转发编辑器。 */
  composer_mode?: "reply" | "forward";
  /** 仅供草稿预览和转发编辑器使用；回复收件人由线程派生。 */
  recipients?: string[];
  subject?: string;
  /** 批量写稿时可选：来源 message / 主题，便于 UI 分行展示 */
  message_id?: string;
}

export interface MailPromptRunResult {
  mailbox: string;
  thread_id: string;
  anchor_message_id: string;
  latest_message_id: string;
  visible_prompt: string;
  thread_title?: string;
  assistant_text: string;
  assistant_followup_text?: string;
  artifact?: DraftReplyArtifact | ComposeDraftArtifact | SendPlanArtifact | null;
  reply_gaps?: ReplyGaps;
  compose_gaps?: ReplyGaps;
  fallback_used?: boolean;
}

export interface InboxThreadDraftPayload {
  mailbox: string;
  thread_id: string;
  exists: boolean;
  etag?: string;
  body: string;
  /** 富文本草稿的安全 HTML；缺失时由 body 按纯文本恢复。 */
  body_html?: string;
  attachments?: OutgoingAttachmentMeta[];
  updated_at?: string;
}

export interface SendPlanArtifact {
  type: "send_plan";
  mailbox: string;
  thread_id: string;
  messages: Array<{ recipients: string[]; subject: string; body: string }>;
  source_prompt: string;
}

export interface ComposeDraftArtifact {
  type: "compose_draft";
  mailbox: string;
  body: string;
  source_prompt: string;
  mode: "insert" | "replace";
  recipients?: string[];
  cc?: string[];
  bcc?: string[];
  subject?: string;
}

export interface ComposeContact {
  email: string;
  name?: string;
  avatar_url?: string;
}

/** 外发附件元数据（字节在后端 stage，不进 KV / JSON-RPC） */
export interface OutgoingAttachmentMeta {
  id: string;
  filename: string;
  mime_type: string;
  size: number;
  storage_key: string;
  /** 前端临时预览（blob: 或 APS 短期 URL），不持久化 */
  preview_url?: string;
  status?: "uploading" | "ready" | "error";
  progress?: number;
  error?: string;
}

export interface ComposeDraft {
  id: string;
  mailbox: string;
  /** forward 草稿回到原邮件详情时使用的路由信息。 */
  draft_mode?: "compose" | "forward";
  source_thread_id?: string;
  source_message_id?: string;
  recipients: string[];
  /** 抄送地址列表 */
  cc?: string[];
  /** 密送地址列表 */
  bcc?: string[];
  subject: string;
  body: string;
  /** 可选 HTML 正文（转发保留原格式时使用 multipart/alternative） */
  body_html?: string;
  /** 外发附件元数据列表 */
  attachments?: OutgoingAttachmentMeta[];
  created_at?: string;
  updated_at?: string;
  etag?: string;
}

export interface ComposeDraftListPayload {
  mailbox: string;
  count: number;
  drafts: ComposeDraft[];
}

export interface ContactMemorySummary {
  mailbox: string;
  contact_email: string;
  display_name?: string;
  thread_count: number;
  message_count: number;
  open_count: number;
  waiting_count: number;
  closed_count: number;
  unknown_count?: number;
  updated_at?: string;
  latest_subject?: string;
  latest_summary?: string;
  latest_status?: string;
}

export interface ContactMemoryFile {
  mailbox: string;
  contact_email: string;
  display_name?: string;
  threads: ContactThreadMemory[];
  stats?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export interface ContactThreadMemory {
  thread_id: string;
  subject?: string;
  thread_summary?: {
    summary?: string;
    current_state?: string;
    open_loop?: string;
    status?: string;
    importance?: string;
  };
  message_summaries?: ContactMessageMemory[];
  source_refs?: Record<string, unknown>[];
  updated_at?: string;
}

export interface ContactMessageMemory {
  message_id?: string;
  from_addr?: string;
  direction?: "inbound" | "outbound" | string;
  date?: string;
  summary?: string;
  action_signal?: string;
}

export interface ContactMemoryDetailPayload {
  memory?: ContactMemoryFile;
  summary?: ContactMemorySummary;
  error?: string;
}

export interface GmailAuthStatus {
  checked: boolean;
  authorized: boolean;
  source?: string;
}

export interface CustomRunProgress {
  runId: string;
  question: string;
  status: string;
  stage: string;
  stageKey: string;
  progress: Record<string, unknown>;
  partial: Record<string, unknown>;
  startedAt?: string;
  updatedAt?: string;
}

export interface AppState {
  runtime: RuntimeState;
  view: MainView;
  mailbox: string;
  mailboxes: MailboxInfo[];
  selectedMailboxes: string[];
  briefMailboxFilter: string[];
  allCards: FrontendCard[];
  strategyMode: string;
  loading: boolean;
  cards: FrontendCard[];
  actionCount: number;
  scanState: ScanState | null;
  history: RunHistoryEntry[];
  scanStatus: string;
  scanError: string;
  restoredCardIds: Set<string>;
  pendingAction: string;
  isScanning: boolean;
  isPreparingScan: boolean;
  isCustomScanning: boolean;
  scanStepIndex: number;
  scanStage: string;
  scanProgress: Record<string, unknown>;
  customPlans: CustomPlanSummary[];
  customScanInput: string;
  customRunResult: CustomRunResult | null;
  customRunProgress: CustomRunProgress | null;
  aiChatMessages: AiChatMessage[];
  aiChatConversationId: string;
  aiChatLoading: boolean;
  mailDetailOpen: boolean;
  /** 当前打开详情的 message id；soft prune 时保留，避免同步误删导致退出详情/附件预览 */
  mailDetailMessageId: string;
  customTraceOpen: boolean;
  settingsOpen: boolean;
  settingsFocusRequest: number;
  inboxSettings: InboxSettings;
  inboxSettingsEtag: string;
  inboxSettingsLoading: boolean;
  inboxSettingsError: string;
  sourcesOpen: boolean;
  historyOpen: boolean;
  memoryOpen: boolean;
  originalOpen: boolean;
  scanPlanOpen: boolean;
  contactMemories: ContactMemorySummary[];
  selectedMemory: ContactMemoryFile | null;
  selectedMemoryKey: string;
  memoryLoading: boolean;
  memoryError: string;
  scanPlan: ScanPlan | null;
  configMailbox: string;
  selectedCard: FrontendCard | null;
  lastOpenedCardKey: string;
  selectedCardDetail: CardDetailPayload | null;
  threadSummaryById: Record<string, Record<string, unknown>>;
  draftById: Record<string, string>;
  draftPreferencesById: Record<string, DraftPreferences>;
  replyIntentById: Record<string, DraftReplyIntent>;
  gapAnswersByCard: Record<string, Record<string, string>>;
  askGapAnswers: Record<string, Record<string, string>>;
  askDraftsByKey: Record<string, string>;
  revisionById: Record<string, string>;
  replyModeById: Record<string, string>;
  threadContextExpanded: Record<string, boolean>;
  expandedDetails: Record<string, boolean>;
  snoozeMenuCardId: string;
  snoozeReasonsKey: string;
  expandAllConfigs: boolean;
  statusByCardId: Record<string, string>;
  lowerPriorityOpen: boolean;
  minimized: boolean;
  resultFilter: ResultFilter;
  llmProvider: LlmProvider;
  llmStatus: LlmStatus;
  gmailApiStatus: GmailApiStatus;
  storageProvider: StorageProvider;
  generatingDraft: boolean;
  draftDots: string;
  summarizingThread: boolean;
  cleanupReadState: Record<string, { read: boolean; readMsgIndices: number[] }>;
  cleanupBundle: CleanupMessage[] | null;
  markingReadIds: Record<string, boolean>;
  attachmentDownloads: Record<string, "preparing" | "error" | "ready">;
  gmailAuthStatus: GmailAuthStatus;
  gmailErrorPopup: GmailErrorPopup | null;
  inboxMessages: InboxMessage[];
  inboxSnapshotMessages: InboxMessage[];
  inboxDraftMessages: InboxMessage[];
  inboxSnapshotLoading: boolean;
  inboxSnapshotComplete: boolean;
  inboxLoading: boolean;
  inboxError: string;
  inboxUpdatedAt: string;
  askItemActions: Record<string, { read?: boolean; trashed?: boolean; replied?: boolean; sending?: boolean }>;
  askEditDraft: Record<string, string>;
  askHistory: AskHistoryEntry[];
  askHistoryExpanded: Record<number, boolean>;
}

export interface GmailErrorPopup {
  mailbox: string;
  message: string;
}

export interface AskHistoryEntry {
  query: string;
  result: CustomRunResult;
  timestamp: string;
  conversationId?: string;
  kind?: "chat" | "scan";
  messages?: AiChatMessage[];
  /** 页面刷新后由用户手动继续轮询的 AI turn。 */
  pendingRun?: {
    runId: string;
    question: string;
  };
}

/** 整理建议确认卡片（propose_inbox_actions，须用户确认后才 mutation） */
export interface ProposedInboxActionItem {
  mailbox: string;
  message_id: string;
  thread_id: string;
  subject?: string;
  default_selected?: boolean;
}

export interface ProposedInboxActions {
  step_index: number;
  step_title: string;
  rationale: string;
  primary_action: "mark_done" | "archive" | "trash" | string;
  allowed_actions: string[];
  items: ProposedInboxActionItem[];
  requires_user_confirmation: boolean;
  /** 确认本批后的追问文案（对标 example/6.png） */
  followup_after_apply?: string;
  /** Skip 本批后的追问文案 */
  followup_after_skip?: string;
  /** 用户点「暂不」后仍展示的收尾建议文案 */
  followup_after_dismiss?: string;
  /** 用户点「继续」时注入的下一轮 user 文本 */
  continue_prompt?: string;
  language?: "zh" | "en" | string;
  recommendation_groups?: Array<{
    title: string;
    subjects?: string[];
    items?: ProposedInboxActionItem[];
  }>;
}

export interface SavedPrompt {
  id: string;
  title: string;
  body: string;
  created_at?: string;
  updated_at?: string;
}

export interface AiMemoryItem {
  id: string;
  text: string;
  source?: string;
  created_at?: string;
  updated_at?: string;
}

export interface AiChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  kind?: "chat" | "mail_context" | "scan" | "clarify" | "status" | "error" | "stopped" | "draft" | "propose" | "memory";
  result?: CustomRunResult | null;
  pending?: boolean;
  /** AI Thinking 开始时间；结果消息保留该值以展示冻结的思考耗时。 */
  thinkingStartedAt?: string;
  artifact?: DraftReplyArtifact | ComposeDraftArtifact | SendPlanArtifact | null;
  /** 批量写稿：一条消息内多份 draft_reply（按封隔离） */
  artifacts?: DraftReplyArtifact[];
  proposedActions?: ProposedInboxActions | null;
  replyGaps?: ReplyGaps;
  mailContext?: AiMailContextRef;
  mailSummaryLink?: AskMailLink;
  fallbackUsed?: boolean;
  sourcePrompt?: string;
  assistantFollowupText?: string;
  clarification?: AiClarificationPayload;
  /** 本轮真实 cache 检索使用的本地 query；有值才展示可点 chip。 */
  scanQuery?: string;
  /** 检索数据源；当前固定 cache。 */
  scanSource?: "cache" | string;
  /** 本轮确认 Evidence 的线程标题，仅用于 AI 详情入口的按钮文案。 */
  threadReferenceLabels?: Record<string, string>;
}

/** 后端 clarify 文案提示；阶段 C 起不再用前端强制 kind 分流 */
export interface AiClarificationAction {
  id: string;
  label: string;
}

export interface AiClarificationPayload {
  /** 澄清场景；search_field 需要把选项作为原问题的检索字段重新提交。 */
  kind?: "search_field" | string;
  original_input: string;
  question: string;
  actions: AiClarificationAction[];
  freeform_enabled: boolean;
  status: "pending" | "resolved" | "dismissed";
  resolved_action?: string;
}

/**
 * 用户显式选定的任务范围（澄清弹层或侧栏 starter 按钮）。
 * 后端收到后跳过 Router Sampling，直接执行对应白名单工具。
 */
export type AiRoutingIntent = "inbox" | "current_thread" | "compose" | "chat" | "organize";

/** 收件箱多选线程，写入 ui_context.selected_threads */
export interface AiSelectedThreadRef {
  mailbox: string;
  message_id: string;
  thread_id: string;
  subject?: string;
}

/** AI 侧栏提交时附带的只读收件箱列表状态。 */
export interface AiInboxListContext {
  mailbox_view: string;
  inbox_group: string;
  search_input: string;
  active_search: string;
  todo_message_ids: string[];
  done_message_ids: string[];
  snoozed_message_ids: string[];
  custom_category?: {
    id: string;
    name: string;
    query: string;
    bundling_behavior: "default" | "by_sender" | "none";
  };
}

export interface SendAiMessageOptions {
  currentMailContext?: AiMailContextRef | null;
  prompt?: string;
  clarificationMessageId?: string;
  baseMessages?: AiChatMessage[];
  retryUserMessage?: AiChatMessage;
  /** 选中 Saved prompt 后注入 Router（作为 user 文本前缀） */
  savedPromptId?: string;
  /** 恢复已提交的 AI turn，只查询既有 run，不重新提交请求。 */
  resumeRunId?: string;
  /** 收件箱勾选线程（批量 draft / outreach） */
  selectedThreads?: AiSelectedThreadRef[];
  /** Router 无法判断时由用户明确选择的范围。 */
  routingIntent?: AiRoutingIntent;
  /** 字段澄清卡确认的邮件检索字段；必须透传给 Evidence，禁止再次由模型推断。 */
  searchField?: "subject" | "body" | "participants" | "date";
  /** 仅供 Agent 继续上一轮澄清所需的上下文，不写入用户可见对话消息。 */
  agentPrompt?: string;
  /** 侧栏当前列表范围，仅作为 Host Agent 的只读事实。 */
  inboxListContext?: AiInboxListContext;
}

