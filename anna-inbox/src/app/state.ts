import type { AppState } from "../types/mail";
import { DEFAULT_MODE, getSavedMailbox } from "./constants";

const AI_ASK_HISTORY_STORAGE_KEY = "anna-inbox:ai-ask-history:v1";

function loadSavedAskHistory(): AppState["askHistory"] {
  if (typeof window === "undefined") return [];
  try {
    const parsed = JSON.parse(window.localStorage.getItem(AI_ASK_HISTORY_STORAGE_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.slice(0, 30) : [];
  } catch {
    // 中文注释：历史记录只是 UI 恢复能力，损坏时直接丢弃，避免阻塞 App 启动。
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
    customTraceOpen: false,
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
    askItemActions: {},
    askEditDraft: {},
    askHistory: savedAskHistory,
    askHistoryExpanded: {},
  };
}
