import type { AppState } from "../types/mail";
import { DEFAULT_MODE, getSavedMailbox } from "./constants";

const AI_ASK_HISTORY_STORAGE_KEY = "anna-inbox:ai-ask-history:v1";
/** 侧栏会话历史仅保留 7 天；每条为独立 conversationId。 */
const AI_ASK_HISTORY_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const AI_ASK_HISTORY_MAX_ENTRIES = 30;

export function pruneAskHistory(
  history: AppState["askHistory"],
  nowMs = Date.now(),
): AppState["askHistory"] {
  if (!Array.isArray(history) || !history.length) return [];
  const cutoff = nowMs - AI_ASK_HISTORY_MAX_AGE_MS;
  return history
    .filter((entry) => {
      const ts = Date.parse(String(entry?.timestamp || ""));
      return Number.isFinite(ts) && ts >= cutoff;
    })
    .slice(0, AI_ASK_HISTORY_MAX_ENTRIES);
}

function loadSavedAskHistory(): AppState["askHistory"] {
  if (typeof window === "undefined") return [];
  try {
    const parsed = JSON.parse(window.localStorage.getItem(AI_ASK_HISTORY_STORAGE_KEY) || "[]");
    const pruned = pruneAskHistory(Array.isArray(parsed) ? parsed : []);
    // 启动时回写裁剪结果，避免过期会话长期占用 localStorage。
    if (pruned.length !== (Array.isArray(parsed) ? parsed.length : 0)) {
      try {
        window.localStorage.setItem(AI_ASK_HISTORY_STORAGE_KEY, JSON.stringify(pruned));
      } catch {
        /* ignore */
      }
    }
    return pruned;
  } catch {
    // 历史记录只是 UI 恢复能力，损坏时直接丢弃，避免阻塞 App 启动。
    return [];
  }
}

export function removeAskHistoryEntry(
  history: AppState["askHistory"],
  index: number,
) {
  if (index < 0 || index >= history.length) return history;
  return history.filter((_, itemIndex) => itemIndex !== index);
}

export function createInitialState(): AppState {
  const mailbox = getSavedMailbox();
  const savedAskHistory = loadSavedAskHistory();
  return {
    runtime: { connected: false, mode: "connecting" },
    view: "start",
    mailbox,
    mailboxes: [],
    selectedMailboxes: mailbox ? [mailbox] : [],
    briefMailboxFilter: mailbox ? [mailbox] : [],
    allCards: [],
    strategyMode: DEFAULT_MODE,
    loading: true,
    cards: [],
    actionCount: 0,
    scanState: null,
    history: [],
    scanStatus: "",
    scanError: "",
    pendingAction: "",
    isScanning: false,
    isPreparingScan: false,
    restoredCardIds: new Set<string>(),
    isCustomScanning: false,
    scanStepIndex: 0,
    scanStage: "",
    scanProgress: {},
    customPlans: [],
    customScanInput: "",
    customRunResult: null,
    customRunProgress: null,
    aiChatMessages: [],
    aiChatConversationId: "",
    aiChatLoading: false,
    mailDetailOpen: false,
    mailDetailMessageId: "",
    customTraceOpen: false,
    settingsOpen: false,
    settingsFocusRequest: 0,
    inboxSettings: { mailbox: "", display_range_days: 30, time_section_mode: "detailed", stars_enabled: true, stars_limit: 10, todos_enabled: true, todos_limit: 10, llm_status_poll_seconds: 60, auto_sync_seconds: 15, custom_categories: [] },
    inboxSettingsEtag: "",
    inboxSettingsLoading: false,
    inboxSettingsError: "",
    inboxWorkflowState: { todos: [], done: [], snoozed: [], snoozedUntil: {} },
    inboxWorkflowStateEtag: "",
    inboxWorkflowStateLoading: false,
    inboxWorkflowStateSaving: false,
    inboxWorkflowStateError: "",
    sourcesOpen: false,
    historyOpen: false,
    memoryOpen: false,
    originalOpen: false,
    scanPlanOpen: false,
    contactMemories: [],
    selectedMemory: null,
    selectedMemoryKey: "",
    memoryLoading: false,
    memoryError: "",
    scanPlan: null,
    configMailbox: "",
    selectedCard: null,
    lastOpenedCardKey: "",
    selectedCardDetail: null,
    threadSummaryById: {},
    draftById: {},
    draftPreferencesById: {},
    replyIntentById: {},
    revisionById: {},
    replyModeById: {},
    threadContextExpanded: {},
    expandedDetails: {},
    snoozeMenuCardId: "",
    snoozeReasonsKey: "",
    expandAllConfigs: false,
    statusByCardId: {},
    lowerPriorityOpen: false,
    minimized: false,
    resultFilter: "all",
    llmProvider: "anna-llm",
    llmStatus: { status: "unknown", checked: false },
    gmailApiStatus: { status: "unknown", checked: false },
    storageProvider: "local",
    generatingDraft: false,
    gapAnswersByCard: {} as Record<string, Record<string, string>>,
    askGapAnswers: {} as Record<string, Record<string, string>>,
    askDraftsByKey: {} as Record<string, string>,
    draftDots: "",
    summarizingThread: false,
    cleanupReadState: {},
    cleanupBundle: null,
    markingReadIds: {},
    attachmentDownloads: {},
    gmailAuthStatus: { checked: false, authorized: true },
    gmailErrorPopup: null,
    inboxMessages: [],
    inboxSnapshotMessages: [],
    inboxDraftMessages: [],
    inboxSnapshotLoading: false,
    inboxSnapshotComplete: false,
    inboxLoading: true,
    inboxError: "",
    inboxUpdatedAt: "",
    indexedSearchMessages: [],
    indexedSearchQuery: "",
    indexedSearchLoading: false,
    indexedSearchError: "",
    indexedSearchHasMore: false,
    indexedSearchNextOffset: 0,
    askItemActions: {},
    askEditDraft: {},
    askHistory: savedAskHistory,
    askHistoryExpanded: {},
  };
}
