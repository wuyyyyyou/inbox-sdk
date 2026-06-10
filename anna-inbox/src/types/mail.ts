export type RuntimeMode = "connecting" | "live" | "mock";
export type MainView = "start" | "ask";
export type CardStatus = "pending" | "snoozed" | "resolved" | "dismissed" | string;
export type ResultFilter = "all" | "reply" | "review" | "cleanup";
export type LlmProvider = "anna-llm" | "dashscope";
export type StorageProvider = "aps" | "local";

export interface MailboxInfo {
  email: string;
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
  tools?: { invoke?: (args: ToolInvokeArgs, options?: { timeoutMs?: number }) => Promise<unknown> };
  window?: { set_title?: (args: { title: string }) => Promise<unknown> };
  call?: (ns: string, method: string, args?: unknown, options?: { timeout?: number; timeoutMs?: number }) => Promise<unknown>;
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
}

export interface CleanupMessage {
  id?: string;
  message_id?: string;
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
  action_count?: number;
  scan_state?: ScanState;
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
  started_at?: string;
  updated_at?: string;
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
  action?: string;            // "snooze" | "reply" | "handled_manually" | "no_action_needed" | "cleanup_read" | "restore"
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

export interface CardDetailPayload {
  card?: FrontendCard;
  thread_context?: Record<string, unknown>;
  contact_context?: Record<string, unknown>;
  latest_body?: string;
  latest_body_html?: string;
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
  isCustomScanning: boolean;
  scanStepIndex: number;
  scanStage: string;
  scanProgress: Record<string, unknown>;
  customPlans: CustomPlanSummary[];
  customScanInput: string;
  customRunResult: CustomRunResult | null;
  customRunProgress: CustomRunProgress | null;
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
  selectedCardDetail: CardDetailPayload | null;
  threadSummaryById: Record<string, Record<string, unknown>>;
  draftById: Record<string, string>;
  gapAnswersByCard: Record<string, Record<string, string>>;
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
  storageProvider: StorageProvider;
  generatingDraft: boolean;
  draftDots: string;
  summarizingThread: boolean;
  cleanupReadState: Record<string, { read: boolean; readMsgIndices: number[] }>;
  markingReadIds: Record<string, boolean>;
  gmailAuthStatus: GmailAuthStatus;
  samplingDebug: SamplingDebugInfo | null;
  samplingTestResult: TestSamplingResult | null;
  askItemActions: Record<string, { read?: boolean; trashed?: boolean; replied?: boolean; sending?: boolean }>;
  askEditDraft: Record<string, string>;
  askHistory: AskHistoryEntry[];
  askHistoryExpanded: Record<number, boolean>;
}

export interface AskHistoryEntry {
  query: string;
  result: CustomRunResult;
  timestamp: string;
}

export interface TestSamplingResult {
  ok: boolean;
  elapsed_ms?: number;
  test_req_id?: string;
  invoke_id?: string;
  model?: string;
  stop_reason?: string;
  content_type?: string;
  text?: string;
  usage?: Record<string, unknown>;
  error_code?: number | string;
  error_message?: string;
  error_data?: Record<string, unknown>;
}

export interface SamplingDebugInfo {
  initialized: boolean;
  protocol_version: string;
  sampling_enabled: boolean;
  sampling_disabled_reason: string;
  host_capabilities: string[];
  executa_manifest_host_capabilities: string[];
  executa_tool_id: string;
  executa_version: string;
  init_raw_params_keys: string[];
  call_count: number;
  success_count: number;
  error_count: number;
  pending_count: number;
  last_request_at: string;
  last_request: Record<string, unknown>;
  last_response_at: string;
  last_response: Record<string, unknown>;
  last_error: Record<string, unknown> | null;
  error_log: Record<string, unknown>[];
  pending_req_ids: string[];
}
