export type RuntimeMode = "connecting" | "live" | "mock";
export type MainView = "start" | "ask";
export type CardStatus = "pending" | "snoozed" | "resolved" | "dismissed" | string;
export type InboxThreadStateOperation = "mark_read" | "mark_unread" | "star" | "unstar" | "mark_important" | "mark_not_important" | "trash" | "untrash";
export type ResultFilter = "all" | "reply" | "review" | "cleanup";
export type LlmProvider = "anna-llm" | "dashscope";
export type StorageProvider = "aps" | "local";
export type LlmStatusValue = "unknown" | "checking" | "connected" | "unavailable" | "error";
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

export interface MailboxInfo {
  email: string;
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
  snippet?: string | null;
  body_preview?: string | null;
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
  message?: InboxMessage & { body_text?: string | null };
}

export interface InboxFeedPayload {
  mailbox?: string;
  days?: number;
  category?: string;
  query?: string;
  count?: number;
  messages: InboxMessage[];
  updated_at?: string;
}

export interface ContactAvatarPayload {
  mailbox?: string;
  avatars: Record<string, string>;
  permission_required?: boolean;
  required_scope?: string;
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
}

export interface CustomRunResultSection {
  heading?: string;
  body?: string;
  items?: CustomRunResultItem[];
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
  trace?: Record<string, unknown>;
  planner_fallback?: boolean;
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
  overview: string;
  quick_replies: QuickReplySuggestion[];
  summary?: Record<string, unknown>;
  related_context?: string[];
  cached?: boolean;
  fallback_used?: boolean;
}

export interface AiMailContextRef {
  kind: "gmail_thread";
  mailbox: string;
  thread_id: string;
  anchor_message_id: string;
  latest_message_id: string;
}

export interface SubmitMailPromptRequest {
  visiblePrompt: string;
  context: AiMailContextRef;
  expectedArtifact?: "draft_reply";
  userAnswers?: Record<string, string>;
}

export interface DraftReplyArtifact {
  type: "draft_reply";
  mailbox: string;
  thread_id: string;
  body: string;
  source_prompt: string;
}

export interface MailPromptRunResult {
  mailbox: string;
  thread_id: string;
  anchor_message_id: string;
  latest_message_id: string;
  visible_prompt: string;
  assistant_text: string;
  artifact?: DraftReplyArtifact | null;
  reply_gaps?: ReplyGaps;
  fallback_used?: boolean;
}

export interface InboxThreadDraftPayload {
  mailbox: string;
  thread_id: string;
  exists: boolean;
  etag?: string;
  body: string;
  updated_at?: string;
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
  customTraceOpen: boolean;
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
}

export interface AiChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  kind?: "chat" | "scan" | "status" | "error" | "stopped";
  result?: CustomRunResult | null;
  pending?: boolean;
  artifact?: DraftReplyArtifact | null;
  replyGaps?: ReplyGaps;
  mailContext?: AiMailContextRef;
  fallbackUsed?: boolean;
  sourcePrompt?: string;
}

