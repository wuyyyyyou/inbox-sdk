import { useCallback, useMemo, useRef, useState } from "react";
import { MailAgentClient } from "../api/mailAgentClient";
import { makeCustomRunProgress, scanProgressLabel, scanStageLabel, stageToStep } from "../features/brief/runHelpers";
import { buildDraftPreferencesInstruction, resolveDraftPreferences } from "../features/handle/draftPreferences";
import { sortInboxMessagesDesc } from "../features/home/inboxMessageOrder";
import { connectRuntime } from "../runtime/runtimeLoader";
import { triggerBrowserDownload } from "../shared/browserDownload";
import type {
  ActiveCardsPayload,
  AiChatMessage,
  AppState,
  AskHistoryEntry,
  AttachmentDownloadPayload,
  CleanupMessage,
  CustomRunResult,
  CustomRunResultItem,
  DraftPreferenceField,
  DraftReplyGoal,
  FrontendCard,
  GmailErrorPopup,
  InboxFeedPayload,
  InboxMessage,
  InboxMessageDisplayBodyPayload,
  InboxThreadAssistPayload,
  InboxThreadDraftPayload,
  InboxThreadPagePayload,
  InboxThreadStateOperation,
  MailPromptRunResult,
  MailboxInfo,
  RunStatus,
  ScanPlan,
  ScanState,
  SendAiMessageOptions,
  SubmitMailPromptRequest,
  ThreadContextPayload,
} from "../types/mail";
import {
  CUSTOM_SCAN_MESSAGE_LIMIT,
  DEFAULT_MODE,
  POLL_INTERVAL_MS,
  POLL_LIMIT,
  requestForMode,
} from "./constants";
import { createInitialState, removeAskHistoryEntry } from "./state";
import { buildRevisionPrompt, decideAiRoute, resolveMailContext } from "./aiRoute";

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function abortableSleep(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("The request was aborted.", "AbortError"));
      return;
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("The request was aborted.", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}

function clampInt(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.round(parsed)));
}

function normalizeScanPlan(plan: ScanPlan | null | undefined): Required<Pick<ScanPlan, "scan_window_days" | "max_messages">> {
  return {
    scan_window_days: clampInt(plan?.scan_window_days, 7, 1, 90),
    max_messages: clampInt(plan?.max_messages, 100, 10, 500),
  };
}

function resultToDraft(result: Record<string, unknown> | undefined, fallback: string): string {
  const draft = result?.draft as { body?: string } | undefined;
  const revised = result?.revised as { body?: string } | undefined;
  return draft?.body || revised?.body || (result?.body as string | undefined) || fallback || "";
}

function buildCustomRunResult(runId: string, result: Record<string, unknown>) {
  const planner = result.planner_llm as { fallback_used?: boolean } | undefined;
  return {
    runId,
    planId: String(result.plan_id || ""),
    plan_title: String(result.plan_title || ""),
    plan_description: String(result.plan_description || ""),
    plan_gmail_queries: Array.isArray(result.plan_gmail_queries) ? result.plan_gmail_queries : [],
    plan_read_depth: String(result.plan_read_depth || ""),
    title: String(result.title || result.plan_title || ""),
    summary: String(result.summary || ""),
    sections: Array.isArray(result.sections) ? result.sections : [],
    trace: (result.trace as Record<string, unknown>) || {},
    planner_fallback: Boolean(planner?.fallback_used),
  };
}

function normalizedMailbox(mailbox: string | undefined): string {
  return String(mailbox || "").trim().toLowerCase();
}

function cardMailbox(card: FrontendCard | null | undefined, fallback = ""): string {
  return normalizedMailbox(card?.details?.mailbox || fallback);
}

function cardUiKey(card: FrontendCard, fallbackMailbox = ""): string {
  return `${cardMailbox(card, fallbackMailbox)}::${card.id}`;
}

function mergeThreadContext(existing: ThreadContextPayload | undefined, incoming: ThreadContextPayload | undefined): ThreadContextPayload | undefined {
  if (!existing) return incoming;
  if (!incoming) return existing;
  const seen = new Set<string>();
  const messages = [...(incoming.messages || []), ...(existing.messages || [])].filter((message) => {
    const key = message.message_id || `${message.date || ""}:${message.from || ""}:${message.subject || ""}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).sort((a, b) => {
    const at = Number(a.date || 0);
    const bt = Number(b.date || 0);
    if (Number.isFinite(at) && Number.isFinite(bt) && at !== bt) return at - bt;
    return String(a.date || "").localeCompare(String(b.date || ""));
  });
  return {
    ...existing,
    ...incoming,
    messages,
  };
}

function withCardKeys(cards: FrontendCard[], fallbackMailbox = ""): FrontendCard[] {
  return cards.map((card) => ({ ...card, uiKey: card.uiKey || cardUiKey(card, fallbackMailbox) }));
}

function filterCardsByMailboxes(cards: FrontendCard[], selected: string[]): FrontendCard[] {
  const allowed = new Set(selected.map(normalizedMailbox).filter(Boolean));
  if (!allowed.size) return [];
  return cards.filter((card) => allowed.has(cardMailbox(card)));
}

function actionCount(cards: FrontendCard[]): number {
  return cards.filter((card) => card.status !== "dismissed" && card.cardType !== "cleanup_bundle" && (card.userAction === "reply" || card.userAction === "review")).length;
}

function selectableMailboxes(selected: string[], fallback: string): string[] {
  const result = selected.map(normalizedMailbox).filter(Boolean);
  return result.length ? result : [normalizedMailbox(fallback)].filter(Boolean);
}

function activeBriefMailboxes(selected: string[], filter: string[], fallback: string): string[] {
  const selectable = selectableMailboxes(selected, fallback);
  const allowed = new Set(selectable);
  const scoped = filter.map(normalizedMailbox).filter((mailbox) => allowed.has(mailbox));
  return scoped.length ? scoped : selectable;
}

function selectedOrPrimary(mailboxes: string[], fallback: string): string {
  return normalizedMailbox(mailboxes[0] || fallback);
}

function patchInboxMessageList(messages: InboxFeedPayload["messages"], messageIds: string[], updater: (message: InboxFeedPayload["messages"][number]) => InboxFeedPayload["messages"][number] | null) {
  const targets = new Set(messageIds.map((value) => String(value).trim()).filter(Boolean));
  return messages
    .map((message) => {
      if (!targets.has(message.id)) return message;
      return updater(message);
    })
    .filter((message): message is InboxFeedPayload["messages"][number] => Boolean(message));
}

function patchInboxThreadDraftPreview(messages: InboxFeedPayload["messages"], mailbox: string, threadId: string, body: string) {
  const normalized = normalizedMailbox(mailbox);
  const preview = body.trim();
  if (!normalized || !threadId || !preview) return messages;
  return messages.map((message) => {
    if (normalizedMailbox(message.mailbox || normalized) !== normalized || message.thread_id !== threadId) return message;
    const labels = new Set((message.label_ids || []).map((label) => String(label)));
    labels.add("DRAFT");
    return {
      ...message,
      draft_body: preview,
      draft_local: true,
      body_preview: preview,
      label_ids: [...labels],
    };
  });
}

function clearInboxThreadDraftPreview(messages: InboxFeedPayload["messages"], mailbox: string, threadId: string) {
  const normalized = normalizedMailbox(mailbox);
  if (!normalized || !threadId) return messages;
  return messages.map((message) => {
    if (normalizedMailbox(message.mailbox || normalized) !== normalized || message.thread_id !== threadId) return message;
    const labels = (message.label_ids || []).filter((label) => String(label).toUpperCase() !== "DRAFT");
    return {
      ...message,
      draft_body: "",
      draft_local: false,
      label_ids: labels,
    };
  });
}

function upsertInboxThreadDraftPreview(
  messages: InboxMessage[],
  mailbox: string,
  threadId: string,
  body: string,
  source?: Record<string, unknown>,
) {
  const normalized = normalizedMailbox(mailbox);
  const preview = body.trim();
  if (!normalized || !threadId || !preview) return messages;
  const sourceMessage = (source || {}) as Partial<InboxMessage>;
  const existingIndex = messages.findIndex((message) =>
    normalizedMailbox(message.mailbox || normalized) === normalized
    && (message.thread_id || message.id) === threadId,
  );
  const existing = existingIndex >= 0 ? messages[existingIndex] : undefined;
  const labels = new Set([
    ...(existing?.label_ids || []),
    ...(sourceMessage.label_ids || []),
  ].map((label) => String(label)));
  labels.add("DRAFT");
  const nextMessage: InboxMessage = {
    ...existing,
    ...sourceMessage,
    id: String(sourceMessage.id || existing?.id || threadId),
    thread_id: threadId,
    mailbox: normalized,
    draft_body: preview,
    draft_local: true,
    body_preview: preview,
    snippet: preview,
    label_ids: [...labels],
  };
  if (existingIndex < 0) return [nextMessage, ...messages];
  return messages.map((message, index) => index === existingIndex ? nextMessage : message);
}

const AI_ASK_HISTORY_STORAGE_KEY = "anna-inbox:ai-ask-history:v1";
function createId(prefix: string) {
  return `${prefix}_${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`;
}

function prefersChinese(input: string) {
  return /[\u3400-\u9fff]/.test(input);
}

function scanPendingText(input: string) {
  return prefersChinese(input)
    ? "我会搜索你的邮箱，找出和这个问题最相关的邮件。"
    : "I'll search your inbox for emails that are relevant to this question.";
}

function chatPendingText(input: string) {
  return prefersChinese(input)
    ? "thinking"
    : "thinking";
}

function sanitizeToolError(error: unknown, input: string) {
  const raw = error instanceof Error ? error.message : String(error);
  if (raw.includes("executa process exited") || raw.includes("[tool_failed]")) {
    return prefersChinese(input)
      ? "邮箱扫描进程中断了。我已经保留了这次问题，你可以稍后重试。"
      : "The inbox scan was interrupted. I kept this question here so you can retry in a moment.";
  }
  return raw.replace(/^\[tool:[^\]]+\]\s*/i, "").trim();
}

function persistAskHistory(history: AskHistoryEntry[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(AI_ASK_HISTORY_STORAGE_KEY, JSON.stringify(history.slice(0, 30)));
  } catch {
    // 中文注释：localStorage 写入失败不影响主流程，最多只是刷新后不能恢复侧栏对话。
  }
}

function syntheticChatResult(messages: AiChatMessage[]): CustomRunResult {
  const firstUser = messages.find((message) => message.role === "user")?.content || "Chat with Anna";
  const lastAssistant = [...messages].reverse().find((message) => message.role === "assistant" && !message.pending)?.content || "";
  return {
    title: firstUser.slice(0, 80),
    summary: lastAssistant.slice(0, 220),
    sections: [],
  };
}

async function waitForCustomScanResult(
  client: MailAgentClient,
  runId: string,
  onStatus: (status: RunStatus) => void,
  signal?: AbortSignal,
): Promise<RunStatus> {
  // 中文注释：Ask 扫描可能超过单次工具调用预算；前端用 run_id 轮询，避免长时间阻塞导致 Executa 被 host 杀掉。
  for (let attempt = 0; attempt < POLL_LIMIT; attempt += 1) {
    if (signal) await abortableSleep(POLL_INTERVAL_MS, signal);
    else await sleep(POLL_INTERVAL_MS);
    const status = await client.getRun(runId);
    if (signal?.aborted) throw new DOMException("The request was aborted.", "AbortError");
    onStatus(status);
    if (status.status === "done" || status.status === "failed" || status.error) {
      return status;
    }
  }
  throw new Error("Custom scan timed out while waiting for the run to finish.");
}

async function waitForToolRunResult(
  client: MailAgentClient,
  runId: string,
  onStatus?: (status: RunStatus) => void,
  signal?: AbortSignal,
): Promise<RunStatus> {
  for (let attempt = 0; attempt < POLL_LIMIT; attempt += 1) {
    if (signal) await abortableSleep(POLL_INTERVAL_MS, signal);
    else await sleep(POLL_INTERVAL_MS);
    const status = await client.getRun(runId);
    if (signal?.aborted) throw new DOMException("The request was aborted.", "AbortError");
    onStatus?.(status);
    if (status.status === "done" || status.status === "failed" || status.error) {
      return status;
    }
  }
  throw new Error("Tool run timed out while waiting for the run to finish.");
}

function findCard(cards: FrontendCard[], keyOrId: string): FrontendCard | undefined {
  return cards.find((card) => card.uiKey === keyOrId) || cards.find((card) => card.id === keyOrId);
}

function removeCardFromList(cards: FrontendCard[], key: string, fallbackMailbox: string): FrontendCard[] {
  return cards.filter((card) => (card.uiKey || cardUiKey(card, fallbackMailbox)) !== key);
}

function scrollToPageTop() {
  const run = () => {
    const scroller = document.getElementById("appContent");
    if (scroller) {
      scroller.scrollTo({ top: 0, behavior: "auto" });
      return;
    }
    window.scrollTo({ top: 0, behavior: "auto" });
  };
  window.requestAnimationFrame(() => {
    run();
    window.requestAnimationFrame(run);
  });
}

function scrollToCard(key: string) {
  if (!key) return;
  const run = () => {
    const selector = `[data-card-key="${CSS.escape(key)}"]`;
    const el = document.querySelector<HTMLElement>(selector);
    if (!el) return;
    const scroller = document.getElementById("appContent");
    if (scroller) {
      const scrollerBox = scroller.getBoundingClientRect();
      const elBox = el.getBoundingClientRect();
      const targetTop = scroller.scrollTop + elBox.top - scrollerBox.top - Math.max(24, (scroller.clientHeight - elBox.height) / 2);
      scroller.scrollTo({ top: Math.max(0, targetTop), behavior: "smooth" });
    } else {
      el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    el.focus({ preventScroll: true });
  };
  window.requestAnimationFrame(() => {
    run();
    window.requestAnimationFrame(run);
  });
}

function base64ToBlobUrl(contentB64: string, mimeType: string) {
  const binary = window.atob(contentB64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return URL.createObjectURL(new Blob([bytes], { type: mimeType || "application/octet-stream" }));
}

type ToastOptions = {
  actionLabel?: string;
  onAction?: () => void;
  secondaryActionLabel?: string;
  onSecondaryAction?: () => void;
};

type InboxPageResult = { ok: boolean; count: number; hasMore: boolean; nextOffset: number };
type GmailInboxPageResult = { ok: boolean; count: number; hasMore: boolean; pageToken: string; pageOffset: number };

export interface AppActions {
  showToast(message: string, options?: ToastOptions): void;
  closeDrawers(): void;
  closeCardDetail(): void;
  setView(view: "start" | "ask"): void;
  setInput(field: "customScanInput", value: string): void;
  setDraft(cardId: string, value: string): void;
  setRevision(cardId: string, value: string): void;
  setDraftPreference(cardId: string, field: DraftPreferenceField, value: string): void;
  setReplyIntentGoal(cardId: string, value: DraftReplyGoal | ""): void;
  setReplyIntentText(cardId: string, value: string): void;
  setReplyMode(cardId: string, value: string): void;
  setResultFilter(value: AppState["resultFilter"]): void;
  toggleDetails(cardId: string): void;
  toggleLowerPriority(): void;
  toggleCustomTrace(): void;
  toggleThreadContext(cardId: string): void;
  toggleSnoozeMenu(cardId: string): void;
  setProvider(kind: "llm" | "storage", value: string): void;
  setDrawer(drawer: "sources" | "history" | "memory" | "scanPlan", open: boolean): void;
  minimize(value: boolean): void;
  checkGmailAuth(mailboxOverride?: string): Promise<{ authorized: boolean; source: string }>;
  checkAnyGmailAuth(): Promise<{ authorized: boolean; source: string }>;
  closeGmailErrorPopup(): void;
  loadMailboxes(): Promise<void>;
  switchMailbox(mailbox: string): Promise<void>;
  setBriefMailboxFilter(mailboxes: string[]): void;
  loadActiveCards(): Promise<void>;
  loadInboxEmails(category?: string, days?: number, force?: boolean): Promise<boolean>;
  refreshInboxEmails(category?: string, days?: number): Promise<InboxPageResult>;
  loadCachedInboxEmails(category?: string, days?: number, offset?: number, append?: boolean): Promise<InboxPageResult>;
  loadGmailInboxEmailsPage(category: string, days: number, pageToken: string, pageOffset: number, excludeMessageIds: string[]): Promise<GmailInboxPageResult>;
  resetInboxFeed(): void;
  preloadMailboxSnapshot(force?: boolean): Promise<boolean>;
  loadInboxEmailBody(messageId: string, mailbox?: string): Promise<string>;
  loadInboxThreadPage(mailbox: string, threadId: string, options?: {
    anchorMessageId?: string;
    beforeIndex?: number | null;
    limit?: number;
    includeDisplayBody?: boolean;
  }): Promise<InboxThreadPagePayload>;
  loadInboxMessageDisplayBody(mailbox: string, messageId: string): Promise<InboxMessageDisplayBodyPayload>;
  loadInboxThreadAssist(mailbox: string, threadId: string, latestMessageId: string, anchorMessageId?: string): Promise<InboxThreadAssistPayload>;
  getInboxThreadDraft(mailbox: string, threadId: string): Promise<InboxThreadDraftPayload>;
  listInboxThreadDrafts(mailbox: string, limit?: number): Promise<InboxFeedPayload>;
  saveInboxThreadDraft(mailbox: string, threadId: string, body: string, ifMatch?: string, message?: Record<string, unknown>): Promise<{ ok?: boolean; etag?: string; updated?: boolean }>;
  deleteInboxThreadDraft(mailbox: string, threadId: string): Promise<{ ok?: boolean }>;
  prepareInboxAttachmentAccess(mailbox: string, messageId: string, attachmentId: string, mode: "preview" | "download"): Promise<AttachmentDownloadPayload>;
  modifyInboxMessageLabels(mailbox: string, messageIds: string[], addLabelIds?: string[], removeLabelIds?: string[]): Promise<void>;
  updateInboxThreadState(mailbox: string, threadId: string, operation: InboxThreadStateOperation): Promise<void>;
  submitMailContextPrompt(request: SubmitMailPromptRequest): Promise<MailPromptRunResult | null>;
  sendInboxThreadReply(args: { mailbox: string; threadId: string; to: string; body: string; replyMode?: string; dryRun?: boolean }): Promise<{ ok?: boolean; dry_run?: boolean; error?: string }>;
  loadContactAvatars(emails: string[], mailbox?: string): Promise<{
    avatars: Record<string, string>;
    permissionRequired: boolean;
    serviceDisabled: boolean;
    activationUrl: string;
  }>;
  setInboxStarred(messageId: string, starred: boolean): Promise<void>;
  markInboxRead(messageId: string): Promise<void>;
  trashInboxMessage(messageId: string): Promise<void>;
  loadRunHistory(): Promise<void>;
  loadSelectedEmailBody(): Promise<void>;
  loadMoreThreadContext(): Promise<void>;
  downloadAttachment(attachmentId: string): Promise<void>;
  loadContactMemories(): Promise<void>;
  openContactMemory(mailbox: string, contactEmail: string): Promise<void>;
  closeContactMemory(): void;
  deleteContactMemory(mailbox: string, contactEmail: string): Promise<void>;
  clearContactMemories(): Promise<void>;
  loadCustomPlans(): Promise<void>;
  loadScanPlan(): Promise<void>;
  saveScanPlanField(field: string, value: unknown): Promise<void>;
  setConfigMailbox(mailbox: string): Promise<void>;
  startScan(reason?: string, mailboxOverride?: string): Promise<void>;
  openCard(cardId: string): Promise<void>;
  summarizeSelectedThread(): Promise<void>;
  generateDraft(userAnswers?: Record<string, string>, options?: { ignoreReplyIntent?: boolean }): Promise<void>;
  stopDraftGeneration(): void;
  clearDraft(cardId?: string): void;
  markCardRead(cardId?: string): Promise<void>;
  recordDecision(decision: string, cardId?: string): Promise<void>;
  replyNow(): Promise<void>;
  clearAllCards(): Promise<void>;
  markCleanupAsRead(cardId: string, messageId?: string, messageIndex?: number): Promise<void>;
  restoreCard(cardId: string, mailbox?: string): Promise<void>;
  snoozeCard(cardId: string, option: string, reasons?: string[]): Promise<void>;
  openSnoozeReasons(cardId: string): void;
  closeSnoozeReasons(): void;
  openSourcesWithConfig(): void;
  sendAiChatMessage(options?: SendAiMessageOptions): Promise<void>;
  dismissAiClarification(messageId: string): void;
  stopAiGeneration(): void;
  startNewAiConversation(): void;
  openAiConversation(index: number): void;
  deleteAiConversation(index: number): void;
  startCustomScan(): Promise<void>;
  reRunCustomPlan(planId: string): Promise<void>;
  deleteCustomPlan(planId: string): Promise<void>;
  clearCards(category: string): Promise<void>;
  clearHistory(): Promise<void>;
  resetAllData(): Promise<void>;
  resetMailboxScanHistory(mailbox: string): Promise<void>;
  resetAndStartScan(mailbox: string): Promise<void>;
  deleteMailboxData(mailbox: string): Promise<void>;
  handleAskMarkRead(actionKey: string, messageId: string, mailbox?: string): Promise<void>;
  handleAskTrash(actionKey: string, messageId: string, mailbox?: string): Promise<void>;
  enterAskDraftEdit(key: string, draft: string): void;
  updateAskDraft(key: string, value: string): void;
  cancelAskDraft(key: string): void;
  sendAskDraft(key: string, threadId: string, to: string, mailbox?: string, draftOverride?: string): Promise<void>;
  toggleAskHistory(idx: number): void;
  setGapAnswers(cardKey: string, answers: Record<string, string>): void;
  setAskGapAnswers(actionKey: string, answers: Record<string, string>): void;
  generateDraftWithAnswers(answers: Record<string, string>): Promise<void>;
  generateAskDraftWithAnswers(actionKey: string, item: CustomRunResultItem, answers: Record<string, string>, mailbox?: string): Promise<void>;
  copyDraft(text: string): Promise<void>;
}

export function useAppController() {
  const [state, setState] = useState<AppState>(() => createInitialState());
  const [toast, setToast] = useState<({ message: string } & ToastOptions) | null>(null);
  const [accountSwitchNotice, setAccountSwitchNotice] = useState<{ email: string; avatarUrl?: string } | null>(null);
  const [accountSwitchNoticeVisible, setAccountSwitchNoticeVisible] = useState(false);
  const toastTimer = useRef<number | null>(null);
  const accountSwitchTimer = useRef<number | null>(null);
  const accountSwitchDismissTimer = useRef<number | null>(null);
  const runtimePromise = useRef<Promise<AppState["runtime"]> | null>(null);
  const draftGenerationRun = useRef<{ runId: string; cancelled: boolean } | null>(null);
  const aiGenerationRun = useRef<{ runId: string; cancelled: boolean; controller: AbortController } | null>(null);
  const inboxFeedCache = useRef(new Map<string, { payload: InboxFeedPayload; loadedAt: number }>());
  const inboxRequestSequence = useRef(0);
  const snapshotRequestMailbox = useRef("");
  const snapshotPromise = useRef<Promise<boolean> | null>(null);

  const getRuntime = useCallback(async () => {
    if (!runtimePromise.current) {
      runtimePromise.current = connectRuntime();
    }
    return runtimePromise.current;
  }, []);

  const client = useMemo(() => new MailAgentClient(getRuntime), [getRuntime]);

  const showToast = useCallback((message: string, options?: ToastOptions) => {
    setToast({ message, ...options });
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3200);
  }, []);

  const dismissToast = useCallback(() => {
    if (toastTimer.current) {
      window.clearTimeout(toastTimer.current);
      toastTimer.current = null;
    }
    setToast(null);
  }, []);

  const applyInboxSnapshotPayload = useCallback((payload: InboxFeedPayload, options: { error?: string; append?: boolean } = {}) => {
    const pageMessages = Array.isArray(payload.messages) ? payload.messages : [];
    setState((s) => {
      const snapshotMessages = options.append
        ? sortInboxMessagesDesc(
          [...new Map([...s.inboxSnapshotMessages, ...pageMessages].map((message) => [message.id, message])).values()],
        )
        : sortInboxMessagesDesc(pageMessages);
      return {
        ...s,
        inboxMessages: snapshotMessages.filter((message) => (message.label_ids || []).includes("INBOX")),
        inboxSnapshotMessages: snapshotMessages,
        inboxUpdatedAt: String(payload.updated_at || s.inboxUpdatedAt || ""),
        inboxLoading: false,
        inboxError: options.error || "",
        inboxSnapshotComplete: true,
      };
    });
  }, []);

  const preloadMailboxSnapshot = useCallback(async (mailboxOverride?: string, days = 7, force = false) => {
    const mailbox = normalizedMailbox(mailboxOverride || state.selectedMailboxes[0] || state.mailbox);
    if (!mailbox || mailbox === "all") return false;
    if (!force && snapshotPromise.current && snapshotRequestMailbox.current === mailbox) return snapshotPromise.current;
    snapshotRequestMailbox.current = mailbox;
    const run = (async () => {
      const startedAt = performance.now();
      setState((s) => ({ ...s, inboxSnapshotLoading: true }));
      try {
        const cached = await client.listCachedEmails(mailbox, days, 100, "inbox", 0);
        if (snapshotRequestMailbox.current !== mailbox) return false;
        applyInboxSnapshotPayload(cached);
        setState((s) => ({ ...s, inboxSnapshotLoading: false }));
        const messageCount = Array.isArray(cached.messages) ? cached.messages.length : 0;
        console.info(`[inbox-startup] cache-only mailbox=${mailbox} messages=${messageCount} elapsed_ms=${Math.round(performance.now() - startedAt)}`);
        return messageCount > 0;
      } catch {
        if (snapshotRequestMailbox.current !== mailbox) return false;
        setState((s) => ({
          ...s,
          inboxSnapshotLoading: false,
          inboxLoading: false,
          inboxSnapshotComplete: true,
          inboxError: "",
        }));
        return false;
      }
    })();
    snapshotPromise.current = run.finally(() => {
      if (snapshotRequestMailbox.current === mailbox) snapshotPromise.current = null;
    });
    return snapshotPromise.current;
  }, [applyInboxSnapshotPayload, client, state.mailbox, state.selectedMailboxes]);

  const completeAiChat = useCallback(async (messages: AiChatMessage[], signal: AbortSignal): Promise<string> => {
    if (signal.aborted) throw new DOMException("The request was aborted.", "AbortError");
    const runtime = await getRuntime();
    if (signal.aborted) throw new DOMException("The request was aborted.", "AbortError");
    const llmPayload = {
      messages: messages.slice(-10).map((message) => ({
        role: message.role,
        content: { type: "text", text: message.content },
      })),
      systemPrompt: [
        "You are Anna, a concise and helpful inbox assistant.",
        "Answer in the same language as the user's latest message.",
        "For greetings, capability questions, and ordinary chat, answer naturally without claiming that you scanned email.",
        "If the user asks for inbox-specific work, tell them you can search the inbox when they ask a concrete mail task.",
      ].join("\n"),
      maxTokens: 500,
      temperature: 0.4,
      metadata: { tool: "ai_sidebar_chat" },
    };
    try {
      const result = runtime.client?.llm && typeof runtime.client.llm.complete === "function"
        ? await runtime.client.llm.complete(llmPayload, { timeoutMs: 60_000, signal })
        : await runtime.client?.call?.("llm", "complete", llmPayload, { timeout: 60_000, timeoutMs: 60_000, signal });
      const content = result && typeof result === "object" ? (result as { content?: { text?: unknown }; text?: unknown }).content : null;
      const text = content && typeof content === "object"
        ? String((content as { text?: unknown }).text || "")
        : String((result as { text?: unknown } | undefined)?.text || "");
      return text.trim() || "你好，我在。你可以直接和我聊天，也可以让我帮你查找、整理或总结邮件。";
    } catch (error) {
      if (signal.aborted || isAbortError(error)) throw error;
      // 中文注释：普通聊天依赖 Anna Host LLM；失败时不应该退化成邮箱扫描，避免再次打扰用户邮箱。
      const latestUser = [...messages].reverse().find((message) => message.role === "user")?.content || "";
      return prefersChinese(latestUser)
        ? "你好，我在。现在普通聊天模型暂时不可用，但你仍然可以让我帮你查找、整理或总结邮件。"
        : "Hi, I'm here. The chat model is temporarily unavailable, but you can still ask me to find, organize, or summarize email.";
    }
  }, [getRuntime]);

  const upsertAiConversationHistory = useCallback((
    conversationId: string,
    messages: AiChatMessage[],
    options: { kind: "chat" | "scan"; query: string; result?: CustomRunResult },
  ) => {
    setState((s) => {
      const result = options.result || syntheticChatResult(messages);
      const timestamp = new Date().toISOString();
      const entry: AskHistoryEntry = {
        conversationId,
        kind: options.kind,
        query: options.query,
        result,
        timestamp,
        messages,
      };
      const nextHistory = [entry, ...s.askHistory.filter((item) => item.conversationId !== conversationId)].slice(0, 30);
      persistAskHistory(nextHistory);
      // 中文注释：Ask history 是“会话索引”；点击历史恢复 messages 后，用户可以继续在同一 conversationId 里追问。
      return { ...s, askHistory: nextHistory, aiChatMessages: messages, aiChatConversationId: conversationId };
    });
  }, []);

  const refreshStoredCardFields = useCallback((cards: FrontendCard[]) => {
    setState((s) => {
      const draftById = { ...s.draftById };
      const threadSummaryById = { ...s.threadSummaryById };
      const expandedDetails = { ...s.expandedDetails };
      for (const card of cards) {
        const key = card.uiKey || cardUiKey(card, s.mailbox);
        if (card.draft_reply && !draftById[key]) draftById[key] = card.draft_reply;
        if (card.thread_summary && !threadSummaryById[key]) {
          try {
            threadSummaryById[key] = JSON.parse(card.thread_summary);
          } catch {
          }
        }
        if (card.cardType === "cleanup_bundle" && !(key in expandedDetails)) expandedDetails[key] = true;
      }
      return { ...s, draftById, threadSummaryById, expandedDetails };
    });
  }, []);

  const showAccountSwitchNotice = useCallback((email: string, avatarUrl?: string) => {
    if (accountSwitchDismissTimer.current) window.clearTimeout(accountSwitchDismissTimer.current);
    setAccountSwitchNotice({ email, avatarUrl });
    setAccountSwitchNoticeVisible(false);
    window.setTimeout(() => setAccountSwitchNoticeVisible(true), 0);
    if (accountSwitchTimer.current) window.clearTimeout(accountSwitchTimer.current);
    accountSwitchTimer.current = window.setTimeout(() => {
      setAccountSwitchNoticeVisible(false);
      accountSwitchDismissTimer.current = window.setTimeout(() => {
        setAccountSwitchNotice(null);
        accountSwitchDismissTimer.current = null;
      }, 220);
    }, 3200);
  }, []);

  const closeAccountSwitchNotice = useCallback(() => {
    if (accountSwitchTimer.current) window.clearTimeout(accountSwitchTimer.current);
    accountSwitchTimer.current = null;
    setAccountSwitchNoticeVisible(false);
    if (accountSwitchDismissTimer.current) window.clearTimeout(accountSwitchDismissTimer.current);
    accountSwitchDismissTimer.current = window.setTimeout(() => {
      setAccountSwitchNotice(null);
      accountSwitchDismissTimer.current = null;
    }, 220);
  }, []);

  const loadInboxEmails = useCallback(async (mailboxOverride?: string, category = "inbox", days = 7, force = false) => {
    const mailbox = normalizedMailbox(mailboxOverride || state.selectedMailboxes[0] || state.mailbox);
    const requestId = ++inboxRequestSequence.current;
    if (!mailbox || mailbox === "all") {
      setState((s) => ({ ...s, inboxMessages: [], inboxLoading: false, inboxError: "Connect a Gmail mailbox to load your inbox." }));
      return false;
    }
    const cacheKey = `${mailbox}|${category}|${days}`;
    const cached = inboxFeedCache.current.get(cacheKey);
    if (cached) {
      setState((s) => ({
        ...s,
        inboxMessages: cached.payload.messages,
        inboxUpdatedAt: String(cached.payload.updated_at || ""),
        inboxLoading: false,
        inboxError: "",
      }));
      if (!force && Date.now() - cached.loadedAt < 60_000) return true;
    }
    setState((s) => ({ ...s, inboxLoading: true, inboxError: "" }));
    try {
      const payload = await client.listInboxEmails(mailbox, days, 100, category);
      inboxFeedCache.current.set(cacheKey, { payload, loadedAt: Date.now() });
      if (requestId !== inboxRequestSequence.current) return false;
      setState((s) => ({
        ...s,
        inboxMessages: Array.isArray(payload.messages) ? payload.messages : [],
        inboxUpdatedAt: String(payload.updated_at || ""),
        inboxLoading: false,
        inboxError: "",
      }));
      return true;
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      console.error("[loadInboxEmails] failed:", detail, error);
      if (requestId !== inboxRequestSequence.current) return false;
      setState((s) => ({ ...s, inboxLoading: false, inboxError: detail }));
      return false;
    }
    return false;
  }, [client, state.mailbox, state.selectedMailboxes]);

  const refreshInboxEmails = useCallback(async (category = "inbox", days = 7): Promise<InboxPageResult> => {
    const mailbox = normalizedMailbox(state.selectedMailboxes[0] || state.mailbox);
    if (!mailbox || mailbox === "all") {
      setState((s) => ({ ...s, inboxMessages: [], inboxSnapshotMessages: [], inboxLoading: false, inboxSnapshotLoading: false, inboxError: "Connect a Gmail mailbox to load your inbox." }));
      return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
    }

    const requestKey = `${mailbox}#refresh:${++inboxRequestSequence.current}`;
    snapshotRequestMailbox.current = requestKey;
    snapshotPromise.current = null;
    for (const key of inboxFeedCache.current.keys()) {
      if (key.startsWith(`${mailbox}|`)) inboxFeedCache.current.delete(key);
    }
    setState((s) => ({
      ...s,
      inboxLoading: s.inboxSnapshotMessages.length || s.inboxMessages.length ? false : true,
      inboxSnapshotLoading: true,
      inboxSnapshotComplete: false,
      inboxError: "",
    }));

    try {
      const payload = await client.listInboxEmails(mailbox, days, 100, category, false);
      if (snapshotRequestMailbox.current !== requestKey) return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
      inboxFeedCache.current.set(`${mailbox}|${category}|${days}`, { payload, loadedAt: Date.now() });
      applyInboxSnapshotPayload(payload);
      const count = Array.isArray(payload.messages) ? payload.messages.length : 0;
      showToast(days > 7
        ? `Inbox synced for the last ${days} days. ${count} messages loaded.`
        : `Inbox refreshed. ${count} messages loaded.`);
      return { ok: true, count, hasMore: count >= 100, nextOffset: count };
    } catch (error) {
      if (snapshotRequestMailbox.current !== requestKey) return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
      const detail = error instanceof Error ? error.message : String(error);
      console.error("[refreshInboxEmails] failed:", detail, error);
      setState((s) => ({
        ...s,
        inboxLoading: false,
        inboxSnapshotComplete: true,
        inboxError: detail,
      }));
      showToast(days > 7 ? `Failed to sync the last ${days} days. ${detail}` : detail);
      return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
    } finally {
      if (snapshotRequestMailbox.current === requestKey) {
        setState((s) => ({ ...s, inboxSnapshotLoading: false }));
      }
    }
    return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
  }, [applyInboxSnapshotPayload, client, state.mailbox, state.selectedMailboxes]);

  const loadActiveCards = useCallback(async (storageOverride?: string, mailboxOverride?: string, options: { timeoutMs?: number } = {}) => {
    const provider = storageOverride ?? state.storageProvider;
    const mailbox = mailboxOverride ?? "all";
    const timeoutMs = options.timeoutMs ?? 55_000;
    const PAGE_SIZE = 50;
    try {
      const allCards: FrontendCard[] = [];
      let offset = 0;
      let hasMore = true;
      let scanState: ActiveCardsPayload["scan_state"];
      const MAX_PAGES = 20;
      while (hasMore && offset < MAX_PAGES * PAGE_SIZE) {
        const payload = await client.loadActiveCards(
          mailbox, provider, offset, PAGE_SIZE,
          timeoutMs,
        );
        allCards.push(...(payload.cards || []));
        hasMore = Boolean(payload.has_more);
        offset += PAGE_SIZE;
        if (payload.scan_state) scanState = payload.scan_state;
      }
      const keyedCards = withCardKeys(allCards, mailbox === "all" ? state.mailbox : mailbox);
      refreshStoredCardFields(keyedCards);
      setState((s) => {
        const selected = activeBriefMailboxes(s.selectedMailboxes, s.briefMailboxFilter, s.mailbox);
        const visible = filterCardsByMailboxes(keyedCards, selected);
        return {
          ...s,
          allCards: keyedCards,
          cards: visible,
          briefMailboxFilter: selected,
          actionCount: actionCount(visible),
          scanState: scanState || s.scanState,
          cleanupBundle: null,
          scanError: "",
          loading: false,
        };
      });
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      console.error("[loadActiveCards] failed:", detail, error);
      setState((s) => ({ ...s, scanError: detail, loading: false }));
      // Preserve existing cards — they may have come from the run result
      // when the Executa process is no longer reachable.
    }
  }, [client, refreshStoredCardFields, state.mailbox, state.storageProvider]);

  const loadCleanupBundle = useCallback(async (mailboxOverride = "all") => {
    const provider = state.storageProvider;
    const PAGE_SIZE = 100;
    const MAX_PAGES = 20;
    const items: CleanupMessage[] = [];
    let offset = 0;
    let hasMore = true;
    try {
      while (hasMore && offset < PAGE_SIZE * MAX_PAGES) {
        const payload = await client.loadCleanupBundlePage(mailboxOverride, provider, offset, PAGE_SIZE, 55_000);
        if (payload.error) {
          console.warn("[loadCleanupBundle] partial failure:", payload.error);
          break;
        }
        items.push(...(payload.items || []));
        hasMore = Boolean(payload.has_more);
        offset += PAGE_SIZE;
      }
      setState((s) => ({ ...s, cleanupBundle: items.length ? items : null }));
    } catch (error) {
      console.error("[loadCleanupBundle] failed:", error);
    }
  }, [client, state.storageProvider]);

  const loadRunHistory = useCallback(async () => {
    try {
      const payload = await client.loadRunHistory();
      setState((s) => ({ ...s, history: Array.isArray(payload.history) ? payload.history : [] }));
    } catch {
      setState((s) => ({ ...s, history: [] }));
    }
  }, [client]);

  const memoryMailboxes = useCallback((): string[] => {
    return selectableMailboxes(state.selectedMailboxes, state.mailbox);
  }, [state.mailbox, state.selectedMailboxes]);

  const loadContactMemories = useCallback(async () => {
    const mailboxes = memoryMailboxes();
    setState((s) => ({ ...s, memoryLoading: true, memoryError: "" }));
    try {
      const payload = await client.listContactMemories(mailboxes, state.storageProvider);
      const contacts = Array.isArray(payload.contacts) ? payload.contacts : [];
      setState((s) => {
        const selectedStillExists = contacts.some((item) => `${item.mailbox}::${item.contact_email}` === s.selectedMemoryKey);
        return {
          ...s,
          contactMemories: contacts,
          selectedMemory: selectedStillExists ? s.selectedMemory : null,
          selectedMemoryKey: selectedStillExists ? s.selectedMemoryKey : "",
          memoryLoading: false,
          memoryError: "",
        };
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState((s) => ({ ...s, memoryLoading: false, memoryError: message, contactMemories: [] }));
    }
  }, [client, memoryMailboxes, state.storageProvider]);

  const openContactMemory = useCallback(async (mailbox: string, contactEmail: string) => {
    const key = `${normalizedMailbox(mailbox)}::${normalizedMailbox(contactEmail)}`;
    setState((s) => ({ ...s, selectedMemoryKey: key, selectedMemory: null, memoryLoading: true, memoryError: "" }));
    try {
      const payload = await client.getContactMemory(mailbox, contactEmail, state.storageProvider);
      if (payload.error) throw new Error(payload.error);
      setState((s) => ({ ...s, selectedMemory: payload.memory || null, memoryLoading: false, memoryError: "" }));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState((s) => ({ ...s, selectedMemory: null, memoryLoading: false, memoryError: message }));
    }
  }, [client, state.storageProvider]);

  const closeContactMemory = useCallback(() => {
    setState((s) => ({ ...s, selectedMemory: null, selectedMemoryKey: "", memoryLoading: false, memoryError: "" }));
  }, []);

  const deleteContactMemory = useCallback(async (mailbox: string, contactEmail: string) => {
    try {
      const result = await client.deleteContactMemory(mailbox, contactEmail, state.storageProvider);
      if (result.error) throw new Error(result.error);
      setState((s) => ({ ...s, selectedMemory: null, selectedMemoryKey: "" }));
      await loadContactMemories();
      showToast("Memory deleted.");
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    }
  }, [client, loadContactMemories, showToast, state.storageProvider]);

  const clearContactMemories = useCallback(async () => {
    try {
      const result = await client.clearContactMemories(memoryMailboxes(), state.storageProvider);
      if (result.error) throw new Error(result.error);
      setState((s) => ({ ...s, selectedMemory: null, selectedMemoryKey: "", contactMemories: [] }));
      await loadContactMemories();
      showToast(`Cleared ${result.deleted || 0} memory file${result.deleted === 1 ? "" : "s"}.`);
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    }
  }, [client, loadContactMemories, memoryMailboxes, showToast, state.storageProvider]);

  const loadCustomPlans = useCallback(async (storageOverride?: string) => {
    const provider = storageOverride ?? state.storageProvider;
    try {
      const payload = await client.loadCustomPlans(provider);
      setState((s) => ({ ...s, customPlans: Array.isArray(payload.plans) ? payload.plans : [] }));
    } catch {
      setState((s) => ({ ...s, customPlans: [] }));
    }
  }, [client, state.storageProvider]);

  const loadScanPlan = useCallback(async (mailboxOverride?: string) => {
    const mailbox = mailboxOverride ?? state.mailbox;
    try {
      const plan = await client.loadScanPlan(mailbox, state.storageProvider);
      setState((s) => ({ ...s, scanPlan: { ...plan, ...normalizeScanPlan(plan) } }));
    } catch {
      setState((s) => ({ ...s, scanPlan: { scan_window_days: 7, max_messages: 100, scan_categories: [] } }));
    }
  }, [client, state.mailbox, state.storageProvider]);

  const loadScanPlanForRun = useCallback(async (mailbox: string): Promise<Required<Pick<ScanPlan, "scan_window_days" | "max_messages">>> => {
    const normalized = normalizedMailbox(mailbox);
    const visiblePlanMailbox = normalizedMailbox(state.configMailbox || state.mailbox);
    if (normalized && normalized === visiblePlanMailbox && state.scanPlan) {
      return normalizeScanPlan(state.scanPlan);
    }
    try {
      return normalizeScanPlan(await client.loadScanPlan(mailbox, state.storageProvider));
    } catch {
      return normalizeScanPlan(null);
    }
  }, [client, state.configMailbox, state.mailbox, state.scanPlan, state.storageProvider]);

  const checkGmailAuth = useCallback(async (mailboxOverride?: string): Promise<{ authorized: boolean; source: string }> => {
    const mailbox = mailboxOverride ?? state.mailbox;
    try {
      const result = await client.checkGmailAuth(mailbox);
      const status = { authorized: Boolean(result && result.authorized), source: (result && result.source) || "none" };
      setState((s) => ({ ...s, gmailAuthStatus: { checked: true, ...status } }));
      return status;
    } catch {
      setState((s) => ({ ...s, gmailAuthStatus: { checked: true, authorized: false, source: "error" } }));
      return { authorized: false, source: "error" };
    }
  }, [client, state.mailbox]);

  const refreshSamplingStatus = useCallback(async (): Promise<AppState["llmStatus"]> => {
    setState((s) => ({ ...s, llmStatus: { ...s.llmStatus, status: "checking", message: "Checking Anna LLM..." } }));
    try {
      const result = await client.checkSamplingStatus();
      const status = result.ok === false
        ? (result.status === "error" ? "error" : "unavailable")
        : (result.status || "connected");
      const next: AppState["llmStatus"] = {
        status: status === "connected" ? "connected" : status === "error" ? "error" : "unavailable",
        checked: true,
        message: result.message || (status === "connected" ? "Anna LLM sampling is connected." : "Anna LLM sampling is unavailable."),
        elapsed_ms: result.elapsed_ms,
      };
      setState((s) => ({ ...s, llmStatus: next }));
      return next;
    } catch (error) {
      const next: AppState["llmStatus"] = {
        status: "error",
        checked: true,
        message: error instanceof Error ? error.message : String(error),
      };
      setState((s) => ({ ...s, llmStatus: next }));
      return next;
    }
  }, [client]);

  const ensureSamplingAvailable = useCallback(async (): Promise<boolean> => {
    if (state.llmProvider !== "anna-llm") return true;
    const status = await refreshSamplingStatus();
    if (status.status === "connected") return true;
    const message = "Anna LLM is unavailable. Please enable sampling permission for this Executa app, then try again.";
    showToast(message);
    setState((s) => ({ ...s, scanError: status.message ? `${message}\n${status.message}` : message }));
    return false;
  }, [refreshSamplingStatus, showToast, state.llmProvider]);

  const resolveScanRequest = useCallback(async (mailboxOverride?: string): Promise<{ mailboxesToScan: string[]; scanMode: string } | null> => {
    if (!state.runtime.connected || state.isScanning || state.isPreparingScan) return null;
    const mailboxesToScan = (mailboxOverride ? [mailboxOverride] : (state.selectedMailboxes.length ? state.selectedMailboxes : [state.mailbox])).map(normalizedMailbox).filter(Boolean);
    if (!mailboxesToScan.length) {
      showToast("Select at least one mailbox.");
      return null;
    }
    if (!(await ensureSamplingAvailable())) return null;
    return {
      mailboxesToScan,
      scanMode: state.strategyMode || DEFAULT_MODE,
    };
  }, [ensureSamplingAvailable, showToast, state.isPreparingScan, state.isScanning, state.mailbox, state.runtime.connected, state.selectedMailboxes, state.strategyMode]);

  const runBriefScan = useCallback(async (scanRequest: { mailboxesToScan: string[]; scanMode: string }, reason = "manual") => {
    const { mailboxesToScan, scanMode } = scanRequest;
    setState((s) => ({
      ...s,
      isPreparingScan: false,
      isScanning: true,
      scanError: "",
      scanStatus: "",
      scanStepIndex: 0,
      scanStage: "scan",
      scanProgress: {},
      resultFilter: "all",
    }));
    const contactMemoryJobs: Array<Record<string, unknown>> = [];
    try {
      const failures: string[] = [];
      for (let index = 0; index < mailboxesToScan.length; index += 1) {
        const mailbox = mailboxesToScan[index];
        const runScanPlan = await loadScanPlanForRun(mailbox);
        const runId = `bg_${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`;
        // 先创建可轮询的 run；真实扫描和 LLM 进度由 continue 调用写入。
        const started = await client.startBriefRun({
          user_request: requestForMode(scanMode),
          mailbox,
          mode: scanMode,
          primary_count: runScanPlan.max_messages,
          max_messages: runScanPlan.max_messages,
          scan_window_days: runScanPlan.scan_window_days,
          ai_provider: state.llmProvider,
          storage_provider: state.storageProvider,
          reason,
          run_id: runId,
        });
        const applyBriefStatus = (status: RunStatus) => {
          setState((s) => ({
            ...s,
            scanStepIndex: stageToStep(status.stage || ""),
            scanStage: status.stage || "",
            scanProgress: status.progress || {},
            scanStatus: scanProgressLabel(status.stage, status.progress) || scanStageLabel(status.stage, status.progress),
          }));
        };
        const refreshBriefStatus = async () => {
          try {
            applyBriefStatus(await client.getRun(runId));
          } catch {
          }
        };
        await refreshBriefStatus();
        // 轮询同一个 run_id，真实进度由 continue_mail_agent_run 所在 invoke 写入。
        const pollTimer = window.setInterval(() => {
          void refreshBriefStatus();
        }, 1500);
        let result: RunStatus = started;
        let cardsVersion = 0;
        const collectWarnings = (status: RunStatus) => {
          const ws = status.warnings;
          if (!ws || !ws.length) return;
          for (const w of ws) {
            const detail = w.detail || {};
            const errs = Array.isArray(detail.errors) ? detail.errors : [];
            const apsErrs = Array.isArray(detail.aps_cache_errors) ? detail.aps_cache_errors : [];
            for (const e of [...errs, ...apsErrs.map((e: string) => `[APS cache] ${e}`)]) {
              if (e && !failures.includes(e)) failures.push(`${mailbox}: ${e}`);
            }
          }
        };
        try {
          for (let step = 0; step < POLL_LIMIT; step += 1) {
            // 每次 continue 都是短 invoke，只推进 Brief 状态机的一小段。
            result = await client.continueBriefRun({
              run_id: runId,
              user_request: requestForMode(scanMode),
              mailbox,
              mode: scanMode,
              primary_count: runScanPlan.max_messages,
              max_messages: runScanPlan.max_messages,
              scan_window_days: runScanPlan.scan_window_days,
              ai_provider: state.llmProvider,
              storage_provider: state.storageProvider,
            });
            applyBriefStatus(result);
            collectWarnings(result);
            const nextCardsVersion = Number(result.cards_version || 0);
            if (Number(result.cards_added || 0) > 0 || nextCardsVersion > cardsVersion) {
              cardsVersion = nextCardsVersion;
              void loadActiveCards(undefined, "all", { timeoutMs: 55_000 });
            }
            if (result.status === "done" || result.status === "failed" || result.needs_continue === false) {
              break;
            }
          }
        } finally {
          window.clearInterval(pollTimer);
          await refreshBriefStatus();
        }
        if (result.status === "failed" || result.error) {
          failures.push(`${mailbox}: ${result.error || "Scan failed"}`);
          continue;
        }
        if (result.status !== "done") {
          failures.push(`${mailbox}: Scan paused before completion`);
        }
        void loadActiveCards(undefined, "all", { timeoutMs: 55_000 });
        // 卡片刷新到界面后，只记录联系人记忆补写任务；扫描主流程结束后再后台续跑。
        contactMemoryJobs.push({
          mailbox,
          since: started.started_at || result.started_at || "",
          ai_provider: state.llmProvider,
          storage_provider: state.storageProvider,
        });
      }
      const statusText = failures.length
        ? `Scan complete with ${failures.length} issue${failures.length === 1 ? "" : "s"}.`
        : "Scan complete. Showing persisted attention cards.";
      setState((s) => ({ ...s, scanStatus: statusText, scanError: s.scanError || failures.join("\n") }));
      showToast(failures.length ? statusText : "Scan complete.");
      window.setTimeout(() => {
        void loadActiveCards(undefined, "all", { timeoutMs: 55_000 });
        void loadRunHistory();
        if (contactMemoryJobs.length) {
          void runContactMemoryBackfill(contactMemoryJobs);
        }
      }, 0);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState((s) => ({ ...s, scanError: message, scanStatus: "", isPreparingScan: false }));
      // Show popup for Gmail connectivity errors so users know to re-authorize
      if (message.toLowerCase().includes("gmail connection failed")) {
        const lastMailbox = mailboxesToScan.length > 0 ? mailboxesToScan[mailboxesToScan.length - 1] : state.mailbox;
        const popup: GmailErrorPopup = { mailbox: normalizedMailbox(lastMailbox) || state.mailbox, message };
        setState((s) => ({ ...s, gmailErrorPopup: popup }));
      }
      showToast(message);
    } finally {
      setState((s) => ({ ...s, isPreparingScan: false, isScanning: false }));
    }
  }, [client, loadActiveCards, loadRunHistory, loadScanPlanForRun, showToast, state.llmProvider, state.mailbox, state.storageProvider]);

  const loadMailboxes = useCallback(async (storageOverride?: string): Promise<{ mailboxes: MailboxInfo[]; selected: string[]; primary: string }> => {
    const provider = storageOverride ?? state.storageProvider;
    try {
      const payload = await client.listMailboxes(provider);
      const mailboxes = Array.isArray(payload.mailboxes) ? payload.mailboxes : [];
      const selectedCandidates = (Array.isArray(payload.selected) && payload.selected.length
        ? payload.selected
        : mailboxes.filter((item) => item.selected !== false).map((item) => item.email)
      ).map(normalizedMailbox).filter(Boolean)
        .filter((email) => mailboxes.find((m) => m.email === email)?.authorized !== false);
      const primary = selectedOrPrimary(selectedCandidates, mailboxes.find((item) => item.authorized !== false)?.email || state.mailbox);
      const selected = primary ? [primary] : [];
      const normalizedMailboxes = mailboxes.map((item) => ({ ...item, selected: normalizedMailbox(item.email) === primary }));
      if (selectedCandidates.length !== selected.length || selectedCandidates[0] !== primary) {
        for (const item of mailboxes) {
          const shouldSelect = normalizedMailbox(item.email) === primary;
          if (Boolean(item.selected) === shouldSelect) continue;
          try {
            await client.setMailboxSelected(item.email, shouldSelect, provider);
          } catch {
            // Keep the UI single-account even if registry cleanup is temporarily unavailable.
          }
        }
      }
      setState((s) => ({
        ...s,
        mailboxes: normalizedMailboxes,
        selectedMailboxes: selected,
        briefMailboxFilter: selected,
        mailbox: primary || s.mailbox,
        gmailAuthStatus: {
          checked: true,
          authorized: mailboxes.length ? mailboxes.some((item) => item.authorized !== false) : s.gmailAuthStatus.authorized,
          source: mailboxes.find((item) => item.email === primary)?.auth_source || s.gmailAuthStatus.source,
        },
      }));
      return { mailboxes: normalizedMailboxes, selected, primary };
    } catch {
      // 注册表不可用时回退到原来的单邮箱行为。
      return { mailboxes: [], selected: state.mailbox ? [state.mailbox] : [], primary: state.mailbox };
    }
  }, [client, state.mailbox, state.storageProvider]);

  const loadMailboxRegistry = useCallback(async (storageOverride?: string): Promise<{ mailboxes: MailboxInfo[]; selected: string[]; primary: string }> => {
    const provider = storageOverride ?? state.storageProvider;
    try {
      const payload = await client.getMailboxRegistry(provider);
      const mailboxes = Array.isArray(payload.mailboxes) ? payload.mailboxes : [];
      const selectedCandidates = (Array.isArray(payload.selected) && payload.selected.length
        ? payload.selected
        : mailboxes.filter((item) => item.selected !== false).map((item) => item.email)
      ).map(normalizedMailbox).filter(Boolean)
        .filter((email) => mailboxes.find((m) => m.email === email)?.authorized !== false);
      const primary = selectedOrPrimary(selectedCandidates, mailboxes.find((item) => item.authorized !== false)?.email || state.mailbox);
      const selected = primary ? [primary] : [];
      const normalizedMailboxes = mailboxes.map((item) => ({ ...item, selected: normalizedMailbox(item.email) === primary }));
      setState((s) => ({
        ...s,
        mailboxes: normalizedMailboxes.length ? normalizedMailboxes : s.mailboxes,
        selectedMailboxes: selected.length ? selected : s.selectedMailboxes,
        briefMailboxFilter: selected.length ? selected : s.briefMailboxFilter,
        mailbox: primary || s.mailbox,
      }));
      return { mailboxes: normalizedMailboxes, selected, primary };
    } catch {
      return { mailboxes: [], selected: state.mailbox ? [state.mailbox] : [], primary: state.mailbox };
    }
  }, [client, state.mailbox, state.storageProvider]);

  const pollBackgroundRun = useCallback(async (runId: string) => {
    for (let poll = 0; poll < 80; poll += 1) {
      await sleep(POLL_INTERVAL_MS);
      const status = await client.getRun(runId);
      if (status.status === "done") return status.result || {};
      if (status.status === "failed") throw new Error(status.error || "Background task failed");
      if (poll === 79) throw new Error("Background task timed out after 200s");
    }
    return {};
  }, [client]);

  const pollDraftRun = useCallback(async (draftRun: { runId: string; cancelled: boolean }) => {
    for (let poll = 0; poll < 80; poll += 1) {
      if (draftRun.cancelled) return null;
      await sleep(POLL_INTERVAL_MS);
      if (draftRun.cancelled) return null;
      const status = await client.getRun(draftRun.runId);
      if (draftRun.cancelled) return null;
      if (status.status === "done") return status.result || {};
      if (status.status === "failed") throw new Error(status.error || "Background task failed");
      if (poll === 79) throw new Error("Background task timed out after 200s");
    }
    return null;
  }, [client]);

  const runContactMemoryBackfill = useCallback(async (jobs: Array<Record<string, unknown>>) => {
    for (const job of jobs) {
      try {
        const started = await client.startContactMemoryRun(job);
        if (!started.run_id) continue;
        let status: RunStatus = started;
        for (let step = 0; step < 120; step += 1) {
          if (status.status === "done" || status.status === "failed" || status.needs_continue === false) break;
          status = await client.continueContactMemoryRun({ ...job, run_id: started.run_id, batch_limit: 1 });
          if (status.status === "done" || status.status === "failed" || status.needs_continue === false) break;
          await sleep(300);
        }
        if (status.status === "failed" && status.error) {
          console.warn("[contactMemoryBackfill] failed:", status.error);
        }
      } catch (error) {
        console.warn("[contactMemoryBackfill] failed:", error);
      }
    }
    await loadContactMemories().catch(() => {});
  }, [client, loadContactMemories]);

  const discoverMailbox = useCallback(async (): Promise<string> => {
    try {
      const result = await client.getAuthorizedMailbox();
      if (result?.mailbox) {
        const email = String(result.mailbox);
        setState((s) => ({ ...s, mailbox: email }));
        return email;
      }
    } catch {
      // keep current mailbox
    }
    return "";
  }, [client]);

  const initialize = useCallback(async () => {
    try {
      const startedAt = performance.now();
      const runtime = await getRuntime();
      setState((s) => ({ ...s, runtime, loading: runtime.connected ? s.loading : false }));
      if (runtime.connected) {
        void refreshSamplingStatus();
        const bootMailbox = normalizedMailbox(state.mailbox);
        let inboxAvailable = bootMailbox ? await preloadMailboxSnapshot(bootMailbox, 7) : false;
        const mailboxState = await loadMailboxRegistry();
        let currentMailbox = mailboxState.primary || bootMailbox;
        if (!currentMailbox) {
          const mailbox = await discoverMailbox();
          currentMailbox = mailbox || state.mailbox;
        }
        if (currentMailbox && currentMailbox !== bootMailbox) {
          inboxAvailable = await preloadMailboxSnapshot(currentMailbox, 7, true);
        }
        void loadMailboxes().then((discoveredState) => {
          const discoveredPrimary = normalizedMailbox(discoveredState.primary);
          if (!discoveredPrimary || discoveredPrimary === currentMailbox) return;
          void preloadMailboxSnapshot(discoveredPrimary, 7, true);
          void loadScanPlan(discoveredPrimary);
        }).catch(() => undefined);
        const authResult = await client.checkAnyGmailAuth();
        const systemAuthorized = Boolean(authResult?.authorized);
        const authWarning = (authResult as Record<string, unknown> | null | undefined)?.warning as string | undefined;
        setState((s) => ({ ...s, gmailAuthStatus: { checked: true, authorized: systemAuthorized, source: authResult?.source || "none" } }));
        if (authWarning) showToast(`Auth notice: ${authWarning}`);
        if (!systemAuthorized) {
          setState((s) => ({
            ...s,
            loading: false,
            inboxLoading: false,
            inboxError: inboxAvailable ? "" : "Connect Gmail to load the last 7 days of email.",
          }));
          return;
        }
        await Promise.all([
          loadRunHistory(),
          loadCustomPlans(),
          loadScanPlan(currentMailbox),
          loadActiveCards(undefined, "all"),
        ]);
        console.info(`[inbox-startup] initialize elapsed_ms=${Math.round(performance.now() - startedAt)} mailbox=${currentMailbox || ""}`);
      }
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      showToast(`Init failed: ${msg}`);
      setState((s) => ({ ...s, loading: false, inboxLoading: false, inboxError: msg }));
    }
  }, [client, discoverMailbox, getRuntime, loadActiveCards, loadCustomPlans, loadMailboxRegistry, loadMailboxes, loadRunHistory, loadScanPlan, preloadMailboxSnapshot, refreshSamplingStatus, showToast, state.mailbox]);

  const actions: AppActions = {
    showToast,
    closeDrawers() {
      setState((s) => ({ ...s, sourcesOpen: false, historyOpen: false, memoryOpen: false, scanPlanOpen: false, expandAllConfigs: false }));
    },
    closeCardDetail() {
      const key = state.selectedCard?.uiKey || state.lastOpenedCardKey;
      setState((s) => ({ ...s, sourcesOpen: false, historyOpen: false, memoryOpen: false, originalOpen: false, scanPlanOpen: false, selectedCard: null, expandAllConfigs: false }));
      scrollToCard(key);
    },
    setView(view) {
      setState((s) => ({ ...s, view, sourcesOpen: false, historyOpen: false, memoryOpen: false, originalOpen: false, scanPlanOpen: false, lowerPriorityOpen: view === "start" ? false : s.lowerPriorityOpen }));
      if (view === "ask") void loadCustomPlans();
    },
    setInput(field, value) {
      setState((s) => ({ ...s, [field]: value }));
    },
    setDraft(cardId, value) {
      setState((s) => ({ ...s, draftById: { ...s.draftById, [cardId]: value } }));
    },
    setRevision(cardId, value) {
      setState((s) => ({ ...s, revisionById: { ...s.revisionById, [cardId]: value } }));
    },
    setDraftPreference(cardId, field, value) {
      setState((s) => ({
        ...s,
        draftPreferencesById: {
          ...s.draftPreferencesById,
          [cardId]: {
            ...resolveDraftPreferences(s.draftPreferencesById[cardId]),
            [field]: value,
          },
        },
      }));
    },
    setReplyIntentGoal(cardId, value) {
      setState((s) => ({
        ...s,
        replyIntentById: {
          ...s.replyIntentById,
          [cardId]: {
            ...s.replyIntentById[cardId],
            goal: value || undefined,
          },
        },
      }));
    },
    setReplyIntentText(cardId, value) {
      setState((s) => ({
        ...s,
        replyIntentById: {
          ...s.replyIntentById,
          [cardId]: {
            ...s.replyIntentById[cardId],
            userTake: value,
          },
        },
      }));
    },
    setReplyMode(cardId, value) {
      setState((s) => ({ ...s, replyModeById: { ...s.replyModeById, [cardId]: value } }));
    },
    setResultFilter(value) {
      setState((s) => ({ ...s, resultFilter: value }));
      if (value === "cleanup") void loadCleanupBundle("all");
    },
    toggleDetails(cardId) {
      const card = findCard(state.cards, cardId);
      const key = card?.uiKey || cardId;
      const willExpand = !state.expandedDetails[key];
      setState((s) => ({ ...s, expandedDetails: { ...s.expandedDetails, [key]: !s.expandedDetails[key] }, snoozeMenuCardId: "" }));
      if (willExpand && card?.cardType === "cleanup_bundle") void loadCleanupBundle("all");
    },
    toggleLowerPriority() {
      setState((s) => ({ ...s, lowerPriorityOpen: !s.lowerPriorityOpen }));
    },
    toggleCustomTrace() {
      setState((s) => ({ ...s, customTraceOpen: !s.customTraceOpen }));
    },
    toggleThreadContext(cardId) {
      setState((s) => ({ ...s, threadContextExpanded: { ...s.threadContextExpanded, [cardId]: !s.threadContextExpanded[cardId] } }));
    },
    toggleSnoozeMenu(cardId) {
      setState((s) => ({ ...s, snoozeMenuCardId: s.snoozeMenuCardId === cardId ? "" : cardId }));
    },
    setProvider(kind, value) {
      if (kind === "llm" && (value === "dashscope" || value === "anna-llm")) {
        setState((s) => ({ ...s, llmProvider: value }));
        showToast(value === "dashscope" ? "LLM: DashScope" : "LLM: Anna sampling");
      }
      if (kind === "storage" && (value === "aps" || value === "local")) {
        setState((s) => ({ ...s, storageProvider: value, customPlans: [] }));
        showToast(value === "aps" ? "Storage: APS" : "Storage: local");
        setTimeout(() => {
          void loadMailboxes(value);
          void loadActiveCards(value);
          void loadRunHistory();
          void loadCustomPlans(value);
          if (state.memoryOpen) void loadContactMemories();
        }, 0);
      }
    },
    setDrawer(drawer, open) {
      setState((s) => ({
        ...s,
        sourcesOpen: drawer === "sources" ? open : false,
        historyOpen: drawer === "history" ? open : false,
        memoryOpen: drawer === "memory" ? open : false,
        scanPlanOpen: drawer === "scanPlan" ? open : false,
      }));
      if (drawer === "scanPlan" && open) void loadScanPlan();
      if (drawer === "memory" && open) void loadContactMemories();
    },
    minimize(value) {
      setState((s) => ({ ...s, minimized: value }));
    },
    checkGmailAuth,
    async checkAnyGmailAuth() {
      try {
        const result = await client.checkAnyGmailAuth();
        const status = { authorized: Boolean(result && result.authorized), source: (result && result.source) || "none" };
        setState((s) => ({ ...s, gmailAuthStatus: { checked: true, ...status } }));
        return status;
      } catch {
        setState((s) => ({ ...s, gmailAuthStatus: { checked: true, authorized: false, source: "error" } }));
        return { authorized: false, source: "error" };
      }
    },
    async loadMailboxes() {
      await loadMailboxes();
    },
    async switchMailbox(mailbox) {
      const primary = normalizedMailbox(mailbox);
      const previousMailbox = normalizedMailbox(state.mailbox);
      if (!primary || primary === normalizedMailbox(state.mailbox)) {
        setState((s) => ({ ...s, selectedMailboxes: primary ? [primary] : s.selectedMailboxes, briefMailboxFilter: primary ? [primary] : s.briefMailboxFilter }));
        return;
      }
      snapshotRequestMailbox.current = primary;
      const target = state.mailboxes.find((item) => normalizedMailbox(item.email) === primary);
      showAccountSwitchNotice(primary, target?.avatar_url);
      setState((s) => {
        const visibleCards = filterCardsByMailboxes(s.allCards, [primary]);
        return {
          ...s,
          mailbox: primary,
          mailboxes: s.mailboxes.map((item) => ({ ...item, selected: normalizedMailbox(item.email) === primary })),
          selectedMailboxes: [primary],
          briefMailboxFilter: [primary],
          configMailbox: s.configMailbox ? primary : "",
          cards: visibleCards,
          actionCount: actionCount(visibleCards),
          inboxLoading: true,
          inboxError: "",
          inboxMessages: [],
          inboxSnapshotMessages: [],
          inboxDraftMessages: [],
          inboxSnapshotComplete: false,
        };
      });
      try {
        for (const item of state.mailboxes) {
          if (normalizedMailbox(item.email) === primary || item.selected === false) continue;
          await client.setMailboxSelected(item.email, false, state.storageProvider);
        }
        const payload = await client.setMailboxSelected(primary, true, state.storageProvider);
        const returnedMailboxes = Array.isArray(payload.mailboxes) ? payload.mailboxes : state.mailboxes;
        const mailboxes = returnedMailboxes.map((item) => ({ ...item, selected: normalizedMailbox(item.email) === primary }));
        const visibleCards = filterCardsByMailboxes(state.allCards, [primary]);
        setState((s) => ({
          ...s,
          mailboxes,
          selectedMailboxes: [primary],
          briefMailboxFilter: [primary],
          mailbox: primary,
          configMailbox: s.configMailbox ? primary : "",
          cards: visibleCards,
          actionCount: actionCount(visibleCards),
          inboxLoading: true,
        }));
        await preloadMailboxSnapshot(primary, 7, true);
        await loadScanPlan(primary);
      } catch (error) {
        closeAccountSwitchNotice();
        const message = error instanceof Error ? error.message : String(error);
        const authFailed = /401|invalid credentials|expired|revoked|unauthenticated/i.test(message);
        setState((s) => {
          const rollbackCards = previousMailbox ? filterCardsByMailboxes(s.allCards, [previousMailbox]) : [];
          return {
            ...s,
            mailbox: previousMailbox || s.mailbox,
            selectedMailboxes: previousMailbox ? [previousMailbox] : [],
            briefMailboxFilter: previousMailbox ? [previousMailbox] : [],
            cards: rollbackCards,
            actionCount: actionCount(rollbackCards),
            inboxLoading: false,
            inboxError: message,
            mailboxes: s.mailboxes.map((item) => ({
              ...item,
              selected: normalizedMailbox(item.email) === previousMailbox,
              ...(normalizedMailbox(item.email) === primary && authFailed ? { authorized: false, last_error: "Reconnect Gmail to continue." } : {}),
            })),
          };
        });
        if (previousMailbox && previousMailbox !== primary) {
          try {
            await client.setMailboxSelected(primary, false, state.storageProvider);
            await client.setMailboxSelected(previousMailbox, true, state.storageProvider);
            await loadInboxEmails(previousMailbox);
          } catch {
            // Keep the original error visible if rollback also fails.
          }
        }
        showToast(authFailed ? "This Gmail authorization expired. Reconnect the account and try again." : message);
      }
    },
    setBriefMailboxFilter(mailboxes) {
      setState((s) => {
        const selected = activeBriefMailboxes(s.selectedMailboxes, mailboxes, s.mailbox);
        const visible = filterCardsByMailboxes(s.allCards, selected);
        const selectedCardVisible = s.selectedCard ? filterCardsByMailboxes([s.selectedCard], selected).length > 0 : false;
        return {
          ...s,
          briefMailboxFilter: selected,
          cards: visible,
          actionCount: actionCount(visible),
          selectedCard: selectedCardVisible ? s.selectedCard : null,
          originalOpen: selectedCardVisible ? s.originalOpen : false,
        };
      });
    },
    loadActiveCards,
    async loadInboxEmails(category = "inbox", days = 7, force = false) {
      if (force && (category === "inbox" || category === "all")) {
        return preloadMailboxSnapshot(undefined, days, true);
      }
      return loadInboxEmails(undefined, category, days, force);
    },
    refreshInboxEmails,
    async loadCachedInboxEmails(category = "inbox", days = 7, offset = 0, append = false): Promise<InboxPageResult> {
      const mailbox = normalizedMailbox(state.selectedMailboxes[0] || state.mailbox);
      if (!mailbox || mailbox === "all") return { ok: false, count: 0, hasMore: false, nextOffset: offset };
      const requestId = ++inboxRequestSequence.current;
      setState((s) => ({
        ...s,
        inboxLoading: append || s.inboxSnapshotMessages.length || s.inboxMessages.length ? false : true,
        inboxSnapshotLoading: true,
        inboxError: "",
      }));
      try {
        const cached = await client.listCachedEmails(mailbox, days, 100, category, offset);
        if (requestId !== inboxRequestSequence.current) {
          return { ok: false, count: 0, hasMore: false, nextOffset: offset };
        }
        applyInboxSnapshotPayload(cached, { append });
        const count = Array.isArray(cached.messages) ? cached.messages.length : 0;
        return {
          ok: true,
          count,
          hasMore: Boolean(cached.has_more),
          nextOffset: Number(cached.next_offset ?? offset + count),
        };
      } catch (error) {
        if (requestId !== inboxRequestSequence.current) {
          return { ok: false, count: 0, hasMore: false, nextOffset: offset };
        }
        const detail = error instanceof Error ? error.message : String(error);
        setState((s) => ({
          ...s,
          inboxSnapshotLoading: false,
          inboxLoading: false,
          inboxSnapshotComplete: true,
          inboxError: detail,
        }));
        return { ok: false, count: 0, hasMore: false, nextOffset: offset };
      } finally {
        if (requestId === inboxRequestSequence.current) {
          setState((s) => ({ ...s, inboxSnapshotLoading: false }));
        }
      }
    },
    async loadGmailInboxEmailsPage(category, days, pageToken, pageOffset, excludeMessageIds): Promise<GmailInboxPageResult> {
      const mailbox = normalizedMailbox(state.selectedMailboxes[0] || state.mailbox);
      if (!mailbox || mailbox === "all") {
        return { ok: false, count: 0, hasMore: false, pageToken, pageOffset };
      }
      const requestId = ++inboxRequestSequence.current;
      setState((s) => ({ ...s, inboxSnapshotLoading: true, inboxError: "" }));
      try {
        const page = await client.listGmailEmailsPage(
          mailbox,
          days,
          100,
          category,
          pageToken,
          pageOffset,
          excludeMessageIds,
        );
        if (requestId !== inboxRequestSequence.current) {
          return { ok: false, count: 0, hasMore: false, pageToken, pageOffset };
        }
        applyInboxSnapshotPayload(page, { append: true });
        const count = Array.isArray(page.messages) ? page.messages.length : 0;
        return {
          ok: true,
          count,
          hasMore: Boolean(page.has_more),
          pageToken: String(page.page_token || ""),
          pageOffset: Number(page.page_offset || 0),
        };
      } catch (error) {
        if (requestId !== inboxRequestSequence.current) {
          return { ok: false, count: 0, hasMore: false, pageToken, pageOffset };
        }
        const detail = error instanceof Error ? error.message : String(error);
        setState((s) => ({
          ...s,
          inboxSnapshotLoading: false,
          inboxLoading: false,
          inboxSnapshotComplete: true,
          inboxError: detail,
        }));
        return { ok: false, count: 0, hasMore: false, pageToken, pageOffset };
      } finally {
        if (requestId === inboxRequestSequence.current) {
          setState((s) => ({ ...s, inboxSnapshotLoading: false }));
        }
      }
    },
    resetInboxFeed() {
      inboxRequestSequence.current += 1;
      snapshotRequestMailbox.current = "";
      snapshotPromise.current = null;
      inboxFeedCache.current.clear();
      setState((s) => ({
        ...s,
        inboxMessages: [],
        inboxSnapshotMessages: [],
        inboxUpdatedAt: "",
        inboxLoading: false,
        inboxSnapshotLoading: false,
        inboxSnapshotComplete: true,
        inboxError: "",
      }));
    },
    preloadMailboxSnapshot: (force = false) => preloadMailboxSnapshot(undefined, 7, force),
    async loadInboxEmailBody(messageId, mailboxOverride) {
      const mailbox = normalizedMailbox(mailboxOverride || state.selectedMailboxes[0] || state.mailbox);
      if (!messageId || !mailbox) return "";
      const payload = await client.getInboxEmail(mailbox, messageId);
      return String(payload.message?.body_text || payload.message?.body_preview || payload.message?.snippet || "");
    },
    async loadInboxThreadPage(mailbox, threadId, options = {}) {
      return client.getInboxThreadPage(normalizedMailbox(mailbox), threadId, options);
    },
    async loadInboxMessageDisplayBody(mailbox, messageId) {
      return client.getInboxMessageDisplayBody(normalizedMailbox(mailbox), messageId);
    },
    async loadInboxThreadAssist(mailbox, threadId, latestMessageId, anchorMessageId) {
      const started = await client.startInboxThreadAssist({
        mailbox: normalizedMailbox(mailbox),
        thread_id: threadId,
        latest_message_id: latestMessageId,
        anchor_message_id: anchorMessageId,
        ai_provider: state.llmProvider,
        storage_provider: state.storageProvider,
      });
      if (started.status === "done" && started.result) {
        return started.result as unknown as InboxThreadAssistPayload;
      }
      if (!started.run_id) {
        throw new Error(started.error || "Inbox thread assist did not return a run id.");
      }
      const completed = await waitForToolRunResult(client, started.run_id);
      if (completed.status === "failed" || completed.error) {
        throw new Error(completed.error || "Inbox thread assist failed.");
      }
      return (completed.result || {}) as unknown as InboxThreadAssistPayload;
    },
    async getInboxThreadDraft(mailbox, threadId) {
      return client.getInboxThreadDraft(normalizedMailbox(mailbox), threadId);
    },
    async listInboxThreadDrafts(mailbox, limit = 100) {
      const normalized = normalizedMailbox(mailbox);
      const payload = await client.listInboxThreadDrafts(normalized, limit);
      setState((s) => ({
        ...s,
        inboxDraftMessages: Array.isArray(payload.messages) ? payload.messages : [],
        inboxUpdatedAt: String(payload.updated_at || ""),
        inboxLoading: false,
        inboxSnapshotLoading: false,
        inboxSnapshotComplete: true,
        inboxError: "",
      }));
      return payload;
    },
    async saveInboxThreadDraft(mailbox, threadId, body, ifMatch, message) {
      const normalized = normalizedMailbox(mailbox);
      const result = await client.saveInboxThreadDraft(normalized, threadId, body, ifMatch, message);
      if (body.trim()) {
        setState((s) => ({
          ...s,
          inboxMessages: patchInboxThreadDraftPreview(s.inboxMessages, normalized, threadId, body),
          inboxSnapshotMessages: patchInboxThreadDraftPreview(s.inboxSnapshotMessages, normalized, threadId, body),
          inboxDraftMessages: upsertInboxThreadDraftPreview(s.inboxDraftMessages, normalized, threadId, body, message),
        }));
      }
      return result;
    },
    async deleteInboxThreadDraft(mailbox, threadId) {
      const normalized = normalizedMailbox(mailbox);
      const result = await client.deleteInboxThreadDraft(normalized, threadId);
      setState((s) => ({
        ...s,
        inboxMessages: clearInboxThreadDraftPreview(s.inboxMessages, normalized, threadId),
        inboxSnapshotMessages: clearInboxThreadDraftPreview(s.inboxSnapshotMessages, normalized, threadId),
        inboxDraftMessages: s.inboxDraftMessages.filter((message) =>
          normalizedMailbox(message.mailbox || normalized) !== normalized
          || (message.thread_id || message.id) !== threadId,
        ),
      }));
      return result;
    },
    async prepareInboxAttachmentAccess(mailbox, messageId, attachmentId, mode) {
      return client.prepareInboxAttachmentAccess(normalizedMailbox(mailbox), messageId, attachmentId, mode);
    },
    async modifyInboxMessageLabels(mailbox, messageIds, addLabelIds = [], removeLabelIds = []) {
      const normalized = normalizedMailbox(mailbox);
      if (!normalized || !messageIds.length) return;
      const prevInboxMessages = state.inboxMessages;
      const prevSnapshotMessages = state.inboxSnapshotMessages;
      const addSet = new Set(addLabelIds.map((label) => label.toUpperCase()));
      const removeSet = new Set(removeLabelIds.map((label) => label.toUpperCase()));
      const patchMessage = (message: InboxFeedPayload["messages"][number]) => {
        const labels = new Set((message.label_ids || []).map((label) => label.toUpperCase()));
        addSet.forEach((label) => labels.add(label));
        removeSet.forEach((label) => labels.delete(label));
        return {
          ...message,
          label_ids: [...labels],
          unread: labels.has("UNREAD"),
          important: labels.has("IMPORTANT"),
        };
      };
      setState((s) => ({
        ...s,
        inboxMessages: patchInboxMessageList(s.inboxMessages, messageIds, patchMessage),
        inboxSnapshotMessages: patchInboxMessageList(s.inboxSnapshotMessages, messageIds, patchMessage),
      }));
      try {
        const result = await client.modifyMessageLabels(normalized, messageIds, addLabelIds, removeLabelIds);
        if (!result.ok) throw new Error(result.error || "Failed to modify message labels");
      } catch (error) {
        setState((s) => ({ ...s, inboxMessages: prevInboxMessages, inboxSnapshotMessages: prevSnapshotMessages }));
        throw error;
      }
    },
    async updateInboxThreadState(mailbox, threadId, operation) {
      const normalized = normalizedMailbox(mailbox);
      if (!normalized || !threadId) return;
      const result = await client.updateInboxThreadState(normalized, threadId, operation);
      if (!result.ok) throw new Error(result.error || "Failed to update thread");

      const patchMessage = (message: InboxFeedPayload["messages"][number]) => {
        if ((message.thread_id || message.id) !== threadId) return message;
        const labels = new Set((message.label_ids || []).map((label) => label.toUpperCase()));
        if (operation === "mark_read") labels.delete("UNREAD");
        if (operation === "mark_unread") labels.add("UNREAD");
        if (operation === "star") labels.add("STARRED");
        if (operation === "unstar") labels.delete("STARRED");
        if (operation === "mark_important") labels.add("IMPORTANT");
        if (operation === "mark_not_important") labels.delete("IMPORTANT");
        if (operation === "trash") {
          labels.delete("INBOX");
          labels.add("TRASH");
        }
        if (operation === "untrash") {
          labels.delete("TRASH");
        }
        return {
          ...message,
          label_ids: [...labels],
          unread: labels.has("UNREAD"),
          important: labels.has("IMPORTANT"),
          starred: labels.has("STARRED"),
        };
      };
      setState((s) => ({
        ...s,
        inboxMessages: s.inboxMessages.map(patchMessage),
        inboxSnapshotMessages: s.inboxSnapshotMessages.map(patchMessage),
      }));
    },
    async submitMailContextPrompt(request) {
      const context = request.context;
      if (!context?.mailbox || !context.thread_id || !request.visiblePrompt.trim() || aiGenerationRun.current) return null;
      const generationRun = {
        runId: createId("generation"),
        cancelled: false,
        controller: new AbortController(),
      };
      aiGenerationRun.current = generationRun;
      const isCurrentGeneration = () => aiGenerationRun.current === generationRun && !generationRun.cancelled;
      const conversationId = request.forceNewConversation ? createId("chat") : state.aiChatConversationId || createId("chat");
      const userMessage: AiChatMessage = {
        id: createId("msg"),
        role: "user",
        content: request.visiblePrompt,
        timestamp: new Date().toISOString(),
        kind: "mail_context",
        mailContext: context,
        sourcePrompt: request.visiblePrompt,
      };
      const baseMessages = request.baseMessages
        ?? (!request.forceNewConversation && state.aiChatConversationId === conversationId ? state.aiChatMessages : []);
      const messagesWithUser = [...baseMessages, userMessage];
      const pendingMessage: AiChatMessage = {
        id: createId("msg"),
        role: "assistant",
        content: "Updating the draft...",
        timestamp: new Date().toISOString(),
        kind: "status",
        pending: true,
        mailContext: context,
        sourcePrompt: request.visiblePrompt,
      };
      setState((s) => ({
        ...s,
        customScanInput: "",
        aiChatConversationId: conversationId,
        aiChatMessages: [...messagesWithUser, pendingMessage],
        aiChatLoading: true,
      }));
      try {
        const started = await client.startInboxMailPrompt({
          mailbox: normalizedMailbox(context.mailbox),
          thread_id: context.thread_id,
          anchor_message_id: context.anchor_message_id,
          latest_message_id: context.latest_message_id,
          visible_prompt: buildRevisionPrompt(request.visiblePrompt, request.draftToRevise),
          expected_artifact: request.expectedArtifact || "draft_reply",
          user_answers: request.userAnswers,
          ai_provider: state.llmProvider,
          storage_provider: state.storageProvider,
        });
        if (!isCurrentGeneration()) return null;
        const completed = started.status === "done" && started.result
          ? started
          : await waitForToolRunResult(client, started.run_id || "", undefined, generationRun.controller.signal);
        if (!isCurrentGeneration()) return null;
        if (completed.status === "failed" || completed.error) {
          throw new Error(completed.error || "Mail prompt failed");
        }
        const payload = (completed.result || {}) as unknown as MailPromptRunResult;
        const finalMessages: AiChatMessage[] = [
          ...messagesWithUser,
          {
            id: createId("msg"),
            role: "assistant",
            content: payload.assistant_text || "I reviewed the thread.",
            timestamp: new Date().toISOString(),
            kind: "mail_context",
            artifact: payload.artifact
              ? { ...payload.artifact, source_prompt: request.visiblePrompt }
              : null,
            replyGaps: payload.reply_gaps,
            mailContext: context,
            fallbackUsed: Boolean(payload.fallback_used),
            sourcePrompt: request.visiblePrompt,
            assistantFollowupText: payload.assistant_followup_text,
          },
        ];
        upsertAiConversationHistory(conversationId, finalMessages, { kind: "chat", query: request.visiblePrompt });
        return payload;
      } catch (error) {
        if (!isCurrentGeneration() || isAbortError(error)) return null;
        const message = sanitizeToolError(error, request.visiblePrompt) || "Anna couldn't finish that reply.";
        const failedMessages: AiChatMessage[] = [
          ...messagesWithUser,
          {
            ...pendingMessage,
            pending: false,
            kind: "error",
            content: message,
            timestamp: new Date().toISOString(),
          },
        ];
        upsertAiConversationHistory(conversationId, failedMessages, { kind: "chat", query: request.visiblePrompt });
        showToast(message);
        return null;
      } finally {
        if (aiGenerationRun.current === generationRun) {
          aiGenerationRun.current = null;
          setState((s) => ({ ...s, aiChatLoading: false }));
        }
      }
    },
    async sendInboxThreadReply({ mailbox, threadId, to, body, replyMode = "reply_to_sender", dryRun = false }) {
      return client.replyFromAsk({
        mailbox: normalizedMailbox(mailbox),
        thread_id: threadId,
        to_addr: to,
        body,
        reply_mode: replyMode,
        dry_run: dryRun,
      });
    },
    async loadContactAvatars(emails, mailboxOverride) {
      const mailbox = normalizedMailbox(mailboxOverride || state.selectedMailboxes[0] || state.mailbox);
      if (!mailbox || !emails.length) return { avatars: {}, permissionRequired: false, serviceDisabled: false, activationUrl: "" };
      const payload = await client.resolveContactAvatars(mailbox, emails);
      return {
        avatars: payload.avatars || {},
        permissionRequired: Boolean(payload.permission_required),
        serviceDisabled: Boolean(payload.service_disabled),
        activationUrl: String(payload.activation_url || ""),
      };
    },
    async setInboxStarred(messageId, starred) {
      const message = state.inboxSnapshotMessages.find((item) => item.id === messageId)
        || state.inboxMessages.find((item) => item.id === messageId);
      const mailbox = normalizedMailbox(message?.mailbox || state.selectedMailboxes[0] || state.mailbox);
      if (!mailbox || !messageId) return;
      const patchMessage = (item: typeof message, nextStarred: boolean) => {
        if (!item || item.id !== messageId) return item;
        const labels = new Set(item.label_ids || []);
        if (nextStarred) labels.add("STARRED"); else labels.delete("STARRED");
        return { ...item, starred: nextStarred, label_ids: [...labels] };
      };
      setState((s) => ({
        ...s,
        inboxMessages: s.inboxMessages.map((item) => patchMessage(item, starred) || item),
        inboxSnapshotMessages: s.inboxSnapshotMessages.map((item) => patchMessage(item, starred) || item),
      }));
      try {
        const result = await client.setMessageStarred(mailbox, messageId, starred);
        if (!result.ok) throw new Error(result.error || "Failed to update star");
      } catch (error) {
        setState((s) => ({
          ...s,
          inboxMessages: s.inboxMessages.map((item) => patchMessage(item, !starred) || item),
          inboxSnapshotMessages: s.inboxSnapshotMessages.map((item) => patchMessage(item, !starred) || item),
        }));
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async markInboxRead(messageId) {
      const message = state.inboxMessages.find((item) => item.id === messageId);
      const mailbox = normalizedMailbox(message?.mailbox || state.selectedMailboxes[0] || state.mailbox);
      if (!messageId || !mailbox) return;
      try {
        const result = await client.markReadFromAsk(mailbox, [messageId]);
        if (!result.ok) throw new Error(result.error || "Failed to mark email as read");
        setState((s) => ({
          ...s,
          inboxMessages: s.inboxMessages.map((item) => item.id === messageId
            ? { ...item, unread: false, label_ids: (item.label_ids || []).filter((label) => label !== "UNREAD") }
            : item),
          inboxSnapshotMessages: s.inboxSnapshotMessages.map((item) => item.id === messageId
            ? { ...item, unread: false, label_ids: (item.label_ids || []).filter((label) => label !== "UNREAD") }
            : item),
        }));
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async trashInboxMessage(messageId) {
      const message = state.inboxMessages.find((item) => item.id === messageId);
      const mailbox = normalizedMailbox(message?.mailbox || state.mailbox);
      if (!messageId || !mailbox) return;
      try {
        const result = await client.trashFromAsk(mailbox, [messageId]);
        if (!result.ok) throw new Error(result.error || "Failed to move email to trash");
        setState((s) => ({
          ...s,
          inboxMessages: s.inboxMessages.filter((item) => item.id !== messageId),
          inboxSnapshotMessages: s.inboxSnapshotMessages.map((item) => item.id === messageId
            ? { ...item, label_ids: [...new Set([...(item.label_ids || []).filter((label) => label !== "INBOX"), "TRASH"])] }
            : item),
        }));
        showToast("Moved to trash.");
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    loadRunHistory,
    loadContactMemories,
    openContactMemory,
    closeContactMemory,
    deleteContactMemory,
    clearContactMemories,
    loadCustomPlans,
    loadScanPlan,
    closeGmailErrorPopup() {
      setState((s) => ({ ...s, gmailErrorPopup: null }));
    },
    async saveScanPlanField(field, value) {
      const sanitizedValue = field === "scan_window_days"
        ? clampInt(value, 7, 1, 90)
        : field === "max_messages"
          ? clampInt(value, 100, 10, 500)
          : value;
      setState((s) => ({ ...s, scanPlan: { ...(s.scanPlan || {}), [field]: sanitizedValue, updated_at: new Date().toISOString() } }));
      const targetMailbox = normalizedMailbox(state.configMailbox || state.mailbox);
      if (!targetMailbox) return;
      try {
        await client.saveScanPlanField(targetMailbox, state.storageProvider, field, sanitizedValue);
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async setConfigMailbox(mailbox) {
      setState((s) => ({ ...s, configMailbox: mailbox }));
      await loadScanPlan(mailbox || undefined);
    },
    async startScan(reason = "manual", mailboxOverride?: string) {
      const scanRequest = await resolveScanRequest(mailboxOverride);
      if (!scanRequest) return;
      await runBriefScan(scanRequest, reason);
    },
    async openCard(cardId) {
      const card = findCard(state.cards, cardId);
      if (card && card.status && card.status !== "pending") return;
      if (!card) return;
      const key = card.uiKey || cardUiKey(card, state.mailbox);
      setState((s) => ({
        ...s,
        selectedCard: card,
        lastOpenedCardKey: key,
        selectedCardDetail: null,
        originalOpen: true,
        sourcesOpen: false,
        historyOpen: false,
        memoryOpen: false,
        snoozeMenuCardId: "",
        pendingAction: `body:${key}`,
        threadContextExpanded: { ...s.threadContextExpanded, [`${key}_body`]: true },
      }));
      scrollToPageTop();
      try {
        const detail = await client.getCardDetail(cardMailbox(card, state.mailbox), card.id, state.storageProvider, true);
        setState((s) => {
          const selectedKey = s.selectedCard ? s.selectedCard.uiKey || cardUiKey(s.selectedCard, s.mailbox) : "";
          if (selectedKey !== key) return s;
          if (!s.selectedCardDetail?.body_loaded) {
            return { ...s, selectedCardDetail: detail };
          }
          return {
            ...s,
            selectedCardDetail: {
              ...detail,
              latest_body: s.selectedCardDetail.latest_body,
              latest_body_html: s.selectedCardDetail.latest_body_html,
              body_loaded: true,
              thread_context: detail.thread_context || s.selectedCardDetail.thread_context,
              contact_context: detail.contact_context || s.selectedCardDetail.contact_context,
            },
          };
        });
      } catch {
      } finally {
        setState((s) => ({ ...s, pendingAction: s.pendingAction === `body:${key}` ? "" : s.pendingAction }));
      }
    },
    async loadSelectedEmailBody() {
      if (!state.selectedCard) return;
      const card = state.selectedCard;
      const key = card.uiKey || cardUiKey(card, state.mailbox);
      if (state.pendingAction === `body:${key}`) return;
      if (state.selectedCardDetail?.body_loaded) {
        setState((s) => ({ ...s, threadContextExpanded: { ...s.threadContextExpanded, [`${key}_body`]: true } }));
        return;
      }
      setState((s) => ({ ...s, pendingAction: `body:${key}` }));
      try {
        const detail = await client.getCardDetail(cardMailbox(card, state.mailbox), card.id, state.storageProvider, true);
        setState((s) => {
          const selectedKey = s.selectedCard ? s.selectedCard.uiKey || cardUiKey(s.selectedCard, s.mailbox) : "";
          if (selectedKey !== key) return s;
          return {
            ...s,
            selectedCardDetail: {
              ...(s.selectedCardDetail || {}),
              ...detail,
              thread_context: s.selectedCardDetail?.thread_context || detail.thread_context,
              contact_context: detail.contact_context || s.selectedCardDetail?.contact_context,
              body_loaded: true,
            },
            threadContextExpanded: { ...s.threadContextExpanded, [`${key}_body`]: true },
          };
        });
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      } finally {
        setState((s) => ({ ...s, pendingAction: s.pendingAction === `body:${key}` ? "" : s.pendingAction }));
      }
    },
    async loadMoreThreadContext() {
      if (!state.selectedCard || !state.selectedCardDetail?.thread_context?.has_more_messages) return;
      const card = state.selectedCard;
      const key = card.uiKey || cardUiKey(card, state.mailbox);
      const actionKey = `thread:${key}`;
      if (state.pendingAction === actionKey) return;
      let beforeIndex = state.selectedCardDetail.thread_context.next_before_index;
      setState((s) => ({ ...s, pendingAction: actionKey }));
      try {
        let merged = state.selectedCardDetail.thread_context;
        const MAX_PAGES = 20;
        for (let pageIndex = 0; pageIndex < MAX_PAGES && beforeIndex !== null && beforeIndex !== undefined; pageIndex += 1) {
          const page = await client.getThreadContextPage(cardMailbox(card, state.mailbox), card.id, state.storageProvider, beforeIndex, 50);
          if (!page) break;
          merged = mergeThreadContext(merged, page) || page;
          if (!page.has_more_messages) break;
          beforeIndex = page.next_before_index;
        }
        setState((s) => {
          const selectedKey = s.selectedCard ? s.selectedCard.uiKey || cardUiKey(s.selectedCard, s.mailbox) : "";
          if (selectedKey !== key || !s.selectedCardDetail) return s;
          return {
            ...s,
            selectedCardDetail: {
              ...s.selectedCardDetail,
              thread_context: {
                ...(merged || s.selectedCardDetail.thread_context),
                has_more_messages: Boolean(merged?.has_more_messages && merged.next_before_index !== null && merged.next_before_index !== undefined),
              },
            },
          };
        });
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      } finally {
        setState((s) => ({ ...s, pendingAction: s.pendingAction === actionKey ? "" : s.pendingAction }));
      }
    },
    async downloadAttachment(attachmentId) {
      if (!state.selectedCard || !attachmentId) return;
      const card = state.selectedCard;
      const key = card.uiKey || cardUiKey(card, state.mailbox);
      const stateKey = `${key}::${attachmentId}`;
      if (state.attachmentDownloads[stateKey] === "preparing") return;
      setState((s) => ({ ...s, attachmentDownloads: { ...s.attachmentDownloads, [stateKey]: "preparing" } }));
      showToast("Preparing download...");
      try {
        const result = await client.prepareAttachmentDownload(cardMailbox(card, state.mailbox), card.id, attachmentId, state.storageProvider);
        if (!result.ok) {
          throw new Error(result.error || "Attachment download is unavailable in this runtime.");
        }
        if (result.download_url) {
          triggerBrowserDownload(result.download_url, result.filename || "attachment");
        } else if (result.content_b64) {
          const blobUrl = base64ToBlobUrl(result.content_b64, result.mime_type || "application/octet-stream");
          try {
            triggerBrowserDownload(blobUrl, result.filename || "attachment");
          } finally {
            window.setTimeout(() => URL.revokeObjectURL(blobUrl), 30_000);
          }
        } else {
          throw new Error(result.error || "Attachment download did not return a usable file.");
        }
        setState((s) => ({ ...s, attachmentDownloads: { ...s.attachmentDownloads, [stateKey]: "ready" } }));
      } catch (error) {
        setState((s) => ({ ...s, attachmentDownloads: { ...s.attachmentDownloads, [stateKey]: "error" } }));
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async summarizeSelectedThread() {
      if (!state.selectedCard || state.summarizingThread) return;
      const cardId = state.selectedCard.id;
      const key = state.selectedCard.uiKey || cardUiKey(state.selectedCard, state.mailbox);
      setState((s) => ({ ...s, summarizingThread: true }));
      try {
        const started = await client.startSummarizeThread({ mailbox: cardMailbox(state.selectedCard, state.mailbox), card_id: cardId, storage_provider: state.storageProvider, ai_provider: state.llmProvider });
        if (!started.run_id) throw new Error(started.error || "start_summarize_thread did not return a run id");
        const result = await pollBackgroundRun(started.run_id);
        const summary = (result.summary || {}) as Record<string, unknown>;
        setState((s) => ({
          ...s,
          selectedCardDetail: result.contact_context && s.selectedCardDetail ? { ...s.selectedCardDetail, contact_context: result.contact_context as Record<string, unknown> } : s.selectedCardDetail,
          threadSummaryById: { ...s.threadSummaryById, [key]: summary },
        }));
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      } finally {
        setState((s) => ({ ...s, summarizingThread: false }));
      }
    },
    async generateDraft(userAnswers?: Record<string, string>, options?: { ignoreReplyIntent?: boolean }) {
      if (!state.selectedCard || state.generatingDraft) return;
      const cardId = state.selectedCard.id;
      const key = state.selectedCard.uiKey || cardUiKey(state.selectedCard, state.mailbox);
      const currentDraft = state.draftById[key] || "";
      const hasExistingDraft = Boolean(currentDraft.trim());
      const rawRevision = hasExistingDraft ? (state.revisionById[key] || "") : "";
      const preferences = resolveDraftPreferences(state.draftPreferencesById[key]);
      const replyIntent = options?.ignoreReplyIntent ? undefined : state.replyIntentById[key];
      const revision = buildDraftPreferencesInstruction(preferences, rawRevision, {
        replyGoal: hasExistingDraft ? "" : replyIntent?.goal,
        userTake: hasExistingDraft ? "" : replyIntent?.userTake,
        hasExistingDraft,
      });
      let draftRun: { runId: string; cancelled: boolean } | null = null;
      setState((s) => ({ ...s, generatingDraft: true }));
      try {
        const started = await client.startGenerateDraft({
          mailbox: cardMailbox(state.selectedCard, state.mailbox),
          card_id: cardId,
          reply_mode: state.replyModeById[key] || "reply_to_sender",
          current_draft: currentDraft,
          revision_input: revision,
          user_answers: userAnswers || undefined,
          storage_provider: state.storageProvider,
          ai_provider: state.llmProvider,
        });
        if (!started.run_id) throw new Error(started.error || "start_generate_draft did not return a run id");
        draftRun = { runId: started.run_id, cancelled: false };
        draftGenerationRun.current = draftRun;
        const result = await pollDraftRun(draftRun);
        if (!result || draftRun.cancelled) return;
        setState((s) => ({
          ...s,
          draftById: { ...s.draftById, [key]: resultToDraft(result, currentDraft) },
        }));
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      } finally {
        if (draftRun && draftGenerationRun.current?.runId === draftRun.runId) {
          draftGenerationRun.current = null;
          setState((s) => ({ ...s, generatingDraft: false, draftDots: "" }));
        }
      }
    },
    stopDraftGeneration() {
      if (!draftGenerationRun.current) return;
      draftGenerationRun.current.cancelled = true;
      setState((s) => ({ ...s, generatingDraft: false, draftDots: "" }));
      showToast("Draft generation stopped.");
    },
    clearDraft(cardId) {
      const card = cardId ? findCard(state.allCards, cardId) || findCard(state.cards, cardId) : state.selectedCard;
      if (!card) return;
      const key = card.uiKey || cardUiKey(card, state.mailbox);
      const clearDraftReply = (item: FrontendCard): FrontendCard => (
        (item.uiKey || cardUiKey(item, state.mailbox)) === key
          ? { ...item, draft_reply: "" }
          : item
      );
      setState((s) => ({
        ...s,
        draftById: { ...s.draftById, [key]: "" },
        revisionById: { ...s.revisionById, [key]: "" },
        allCards: s.allCards.map(clearDraftReply),
        cards: s.cards.map(clearDraftReply),
        selectedCard: s.selectedCard && (s.selectedCard.uiKey || cardUiKey(s.selectedCard, s.mailbox)) === key
          ? { ...s.selectedCard, draft_reply: "" }
          : s.selectedCard,
      }));
    },
    async markCardRead(cardId) {
      const card = cardId ? findCard(state.cards, cardId) : state.selectedCard;
      const cid = card?.id;
      const key = card?.uiKey || (card ? cardUiKey(card, state.mailbox) : "");
      if (!card || !cid || state.pendingAction) return;
      setState((s) => {
        const nextAllCards = removeCardFromList(s.allCards, key, s.mailbox);
        const nextCards = removeCardFromList(s.cards, key, s.mailbox);
        return {
          ...s,
          allCards: nextAllCards,
          cards: nextCards,
          actionCount: actionCount(nextCards),
          originalOpen: false,
          selectedCard: null,
          lastOpenedCardKey: key,
          expandedDetails: { ...s.expandedDetails, [key]: false },
          statusByCardId: { ...s.statusByCardId, [key]: "Read" },
        };
      });
      showToast("Card removed from this briefing.");
      void client.markCardRead({
        mailbox: cardMailbox(card, state.mailbox),
        card_id: cid,
      }).then(() => loadRunHistory())
        .catch((error) => {
          showToast(error instanceof Error ? error.message : String(error));
        });
    },
    async recordDecision(decision, cardId) {
      const card = cardId ? findCard(state.cards, cardId) : state.selectedCard;
      const cid = card?.id;
      const key = card?.uiKey || (card ? cardUiKey(card, state.mailbox) : "");
      if (!card || !cid || state.pendingAction) return;
      setState((s) => ({ ...s, pendingAction: `decision:${cid}` }));
      try {
        await client.recordCardDecision({ mailbox: cardMailbox(card, state.mailbox), card_id: cid, decision, storage_provider: state.storageProvider });
        setState((s) => ({ ...s, originalOpen: false, selectedCard: null, expandedDetails: { ...s.expandedDetails, [key]: false } }));
        await loadActiveCards();
        await loadRunHistory();
        showToast("Card removed from this briefing.");
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      } finally {
        setState((s) => ({ ...s, pendingAction: "" }));
      }
    },
    async replyNow() {
      if (!state.selectedCard || state.pendingAction) return;
      const key = state.selectedCard.uiKey || cardUiKey(state.selectedCard, state.mailbox);
      const draft = state.draftById[key] || "";
      if (!draft.trim()) {
        showToast("Draft is empty. Generate a draft first.");
        return;
      }
      setState((s) => ({ ...s, pendingAction: `reply:${state.selectedCard!.id}` }));
      try {
        await client.replyNow({
          mailbox: cardMailbox(state.selectedCard, state.mailbox),
          card_id: state.selectedCard.id,
          draft_body: draft,
          reply_mode: state.replyModeById[key] || "reply_to_sender",
          dry_run: false,
        });
        setState((s) => ({ ...s, originalOpen: false, selectedCard: null }));
        showToast("Reply sent successfully.");
        await loadActiveCards();
        await loadRunHistory();
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      } finally {
        setState((s) => ({ ...s, pendingAction: "" }));
      }
    },
    async clearAllCards() {
      try {
        const selected = state.selectedMailboxes.length ? state.selectedMailboxes : [state.mailbox];
        for (const mailbox of selected.map(normalizedMailbox).filter(Boolean)) {
          await client.clearActiveCards(mailbox, state.storageProvider);
        }
        setState((s) => ({ ...s, cards: [], actionCount: 0, scanState: null, expandedDetails: {}, cleanupReadState: {}, markingReadIds: {}, attachmentDownloads: {} }));
        showToast("All cards cleared.");
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async markCleanupAsRead(cardId, messageId, messageIndex) {
      const card = findCard(state.cards, cardId);
      if (!card) return;
      const key = card?.uiKey || (card ? cardUiKey(card, state.mailbox) : cardId);
      const targetKey = messageId ? `${key}:${messageId}` : key;
      const cardMbox = cardMailbox(card, state.mailbox);
      let bundle = Array.isArray(state.cleanupBundle) && state.cleanupBundle.length > 0 ? state.cleanupBundle : (Array.isArray(card?.bundledMessages) ? card.bundledMessages : []);
      const expectedCount = card.bundledCount || bundle.length;
      try {
        if (expectedCount > bundle.length) {
          const loaded: CleanupMessage[] = [];
          let offset = 0;
          let hasMore = true;
          while (hasMore && offset < 2000) {
            const payload = await client.loadCleanupBundlePage(cardMbox || "all", state.storageProvider, offset, 100, 55_000);
            loaded.push(...(payload.items || []));
            hasMore = Boolean(payload.has_more);
            offset += 100;
          }
          if (loaded.length) {
            bundle = loaded;
            setState((s) => ({ ...s, cleanupBundle: loaded }));
          }
        }
        const messages = cardMbox ? bundle.filter((m) => normalizedMailbox(m.mailbox ?? "") === normalizedMailbox(cardMbox)) : bundle;
        const selectedMessages = messageId ? messages.filter((m) => (m.message_id || m.id) === messageId) : messages;
        const messageIds = selectedMessages.map((m) => m.message_id || m.id).filter(Boolean) as string[];
        if (!messageIds.length) return;
        setState((s) => ({ ...s, markingReadIds: { ...s.markingReadIds, [targetKey]: true } }));
        const result = await client.markCleanupRead({ mailbox: cardMailbox(card, state.mailbox), card_id: card.id, message_ids: messageIds, storage_provider: state.storageProvider });
        if (!result.ok) {
          showToast(result.gmail_error || "Failed to mark as read in Gmail.");
          return;
        }
        setState((s) => {
          const existing = s.cleanupReadState[key] || { read: false, readMsgIndices: [] };
          const existingIndices = new Set(existing.readMsgIndices || []);
          if (messageId) {
            if (typeof messageIndex === "number") existingIndices.add(messageIndex);
          } else {
            messages.forEach((_m, i) => existingIndices.add(i));
          }
          const readMsgIndices = Array.from(existingIndices).sort((a, b) => a - b);
          return {
            ...s,
            cleanupReadState: {
              ...s.cleanupReadState,
              [key]: { read: readMsgIndices.length >= messages.length, readMsgIndices },
            },
          };
        });
        await loadRunHistory();
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      } finally {
        setState((s) => {
          const markingReadIds = { ...s.markingReadIds };
          delete markingReadIds[targetKey];
          return { ...s, markingReadIds };
        });
      }
    },
    async restoreCard(cardId, mailboxOverride) {
      const card = findCard(state.allCards, cardId) || findCard(state.cards, cardId);
      const cid = card?.id || cardId;
      setState((s) => { const next = new Set(s.restoredCardIds); next.add(cid); return { ...s, restoredCardIds: next }; });
      try {
        await client.restoreCard(card ? cardMailbox(card, mailboxOverride || state.mailbox) : (mailboxOverride || state.mailbox), cid, state.storageProvider);
        await loadActiveCards();
        await loadRunHistory();
        showToast("Card restored.");
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async snoozeCard(cardId, option, reasons?: string[]) {
      const optionMap: Record<string, string> = { tomorrow: "tomorrow", "next-week": "next_week", "dont-prioritize": "dont_prioritize" };
      const card = findCard(state.cards, cardId);
      if (!card) return;
      try {
        await client.recordSnooze({ mailbox: cardMailbox(card, state.mailbox), card_id: card.id, snooze_option: optionMap[option] || option, reasons, storage_provider: state.storageProvider });
        setState((s) => ({ ...s, snoozeMenuCardId: "", snoozeReasonsKey: "" }));
        await loadActiveCards();
        await loadRunHistory();
        showToast(option === "dont-prioritize" ? "Preference saved." : "Card snoozed.");
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    openSnoozeReasons(cardId: string) {
      setState((s) => ({ ...s, snoozeReasonsKey: cardId, snoozeMenuCardId: "" }));
    },
    closeSnoozeReasons() {
      setState((s) => ({ ...s, snoozeReasonsKey: "" }));
    },
    openSourcesWithConfig() {
      const mailboxes = state.mailboxes.length ? state.mailboxes : (state.mailbox ? [{ email: state.mailbox }] : []);
      const first = mailboxes[0]?.email || state.mailbox || "";
      setState((s) => ({ ...s, sourcesOpen: true }));
      if (first) void actions.setConfigMailbox(first);
    },
    stopAiGeneration() {
      const run = aiGenerationRun.current;
      if (!run) return;
      run.cancelled = true;
      run.controller.abort();
      aiGenerationRun.current = null;
      setState((s) => {
        const stoppedAt = new Date().toISOString();
        const messages = s.aiChatMessages.map((message) => message.pending
          ? { ...message, pending: false, kind: "stopped" as const, content: "Generation stopped.", timestamp: stoppedAt }
          : message);
        const lastUser = [...messages].reverse().find((message) => message.role === "user");
        const conversationId = s.aiChatConversationId;
        if (!lastUser || !conversationId) {
          return { ...s, aiChatMessages: messages, aiChatLoading: false, isCustomScanning: false, scanStatus: "", customRunProgress: null };
        }
        const kind = lastUser.kind === "scan" ? "scan" as const : "chat" as const;
        const timestamp = stoppedAt;
        const entry: AskHistoryEntry = {
          conversationId,
          kind,
          query: lastUser.content,
          result: syntheticChatResult(messages),
          timestamp,
          messages,
        };
        const nextHistory = [entry, ...s.askHistory.filter((item) => item.conversationId !== conversationId)].slice(0, 30);
        persistAskHistory(nextHistory);
        return {
          ...s,
          aiChatMessages: messages,
          askHistory: nextHistory,
          aiChatLoading: false,
          isCustomScanning: false,
          scanStatus: "",
          customRunProgress: null,
        };
      });
    },
    startNewAiConversation() {
      const run = aiGenerationRun.current;
      if (run) {
        run.cancelled = true;
        run.controller.abort();
        aiGenerationRun.current = null;
      }
      setState((s) => ({
        ...s,
        customScanInput: "",
        aiChatMessages: [],
        aiChatConversationId: "",
        aiChatLoading: false,
        isCustomScanning: false,
        scanStatus: "",
        customRunProgress: null,
      }));
    },
    openAiConversation(index: number) {
      const entry = state.askHistory[index];
      if (!entry) return;
      const run = aiGenerationRun.current;
      if (run) {
        run.cancelled = true;
        run.controller.abort();
        aiGenerationRun.current = null;
      }
      setState((s) => ({
        ...s,
        aiChatConversationId: entry.conversationId || createId("chat"),
        aiChatMessages: entry.messages || [
          { id: createId("msg"), role: "user", content: entry.query || "Inbox question", timestamp: entry.timestamp },
          {
            id: createId("msg"),
            role: "assistant",
            content: entry.result.summary || entry.result.plan_description || "Anna finished scanning your inbox.",
            timestamp: entry.timestamp,
            kind: entry.kind || "scan",
            result: entry.result,
          },
        ],
        customScanInput: "",
        aiChatLoading: false,
        isCustomScanning: false,
        scanStatus: "",
        customRunProgress: null,
      }));
    },
    deleteAiConversation(index: number) {
      const entry = state.askHistory[index];
      if (!entry) return;
      const deletingCurrent = Boolean(
        entry.conversationId &&
        entry.conversationId === state.aiChatConversationId,
      );
      if (deletingCurrent) {
        const run = aiGenerationRun.current;
        if (run) {
          run.cancelled = true;
          run.controller.abort();
          aiGenerationRun.current = null;
        }
      }
      setState((s) => {
        const nextHistory = removeAskHistoryEntry(s.askHistory, index);
        persistAskHistory(nextHistory);
        return {
          ...s,
          askHistory: nextHistory,
          askHistoryExpanded: {},
          ...(deletingCurrent
            ? {
                customScanInput: "",
                aiChatMessages: [],
                aiChatConversationId: "",
                aiChatLoading: false,
                isCustomScanning: false,
                scanStatus: "",
                customRunProgress: null,
              }
            : {}),
        };
      });
    },
    dismissAiClarification(messageId) {
      setState((s) => ({
        ...s,
        aiChatMessages: s.aiChatMessages.map((message) =>
          message.id === messageId && message.clarification?.status === "pending"
            ? { ...message, clarification: { ...message.clarification, status: "dismissed" } }
            : message,
        ),
      }));
    },
    async sendAiChatMessage(options = {}) {
      const userRequest = String(options.prompt ?? state.customScanInput).trim();
      if (!userRequest || state.isCustomScanning || state.aiChatLoading || aiGenerationRun.current) return;
      const conversationId = state.aiChatConversationId || createId("chat");
      const unresolvedBase = state.aiChatConversationId === conversationId ? state.aiChatMessages : [];
      const baseMessages = options.clarificationMessageId
        ? unresolvedBase.map((message) =>
            message.id === options.clarificationMessageId && message.clarification
              ? {
                  ...message,
                  clarification: {
                    ...message.clarification,
                    status: "resolved" as const,
                    resolved_action: options.forcedKind,
                  },
                }
              : message,
          )
        : unresolvedBase;
      const decision = options.forcedKind
        ? { kind: options.forcedKind, reason: "clarification_action", confidence: "high" as const }
        : decideAiRoute(userRequest, {
            messages: baseMessages,
            currentMailContext: options.currentMailContext,
          });

      if (decision.kind === "clarify") {
        const chinese = prefersChinese(userRequest);
        const userMessage: AiChatMessage = {
          id: createId("msg"),
          role: "user",
          content: userRequest,
          timestamp: new Date().toISOString(),
          kind: "clarify",
        };
        const clarificationMessage: AiChatMessage = {
          id: createId("msg"),
          role: "assistant",
          content: chinese
            ? "你想让我修改当前草稿，还是搜索邮箱？"
            : "Do you want me to revise the current draft, or search your inbox?",
          timestamp: new Date().toISOString(),
          kind: "clarify",
          clarification: {
            original_input: userRequest,
            question: chinese
              ? "你想让我修改当前草稿，还是搜索邮箱？"
              : "Do you want me to revise the current draft, or search your inbox?",
            actions: [
              { id: "mail_context", label: chinese ? "修改当前草稿" : "Revise current draft" },
              { id: "scan", label: chinese ? "搜索邮箱" : "Search inbox" },
              { id: "chat", label: chinese ? "普通聊天" : "Just chat" },
            ],
            freeform_enabled: true,
            status: "pending",
          },
        };
        setState((s) => ({
          ...s,
          customScanInput: "",
          aiChatConversationId: conversationId,
          aiChatMessages: [...baseMessages, userMessage, clarificationMessage],
        }));
        return;
      }

      if (decision.kind === "mail_context") {
        const resolved = resolveMailContext(baseMessages, options.currentMailContext);
        if (!resolved) {
          const finalMessages: AiChatMessage[] = [
            ...baseMessages,
            {
              id: createId("msg"),
              role: "user",
              content: userRequest,
              timestamp: new Date().toISOString(),
              kind: "mail_context",
            },
            {
              id: createId("msg"),
              role: "assistant",
              content: prefersChinese(userRequest)
                ? "请先打开一封邮件，这样我才知道要修改哪一封草稿。"
                : "Open an email first so I know what to revise.",
              timestamp: new Date().toISOString(),
              kind: "clarify",
            },
          ];
          setState((s) => ({
            ...s,
            customScanInput: "",
            aiChatConversationId: conversationId,
            aiChatMessages: finalMessages,
          }));
          return;
        }
        await actions.submitMailContextPrompt({
          visiblePrompt: userRequest,
          context: resolved.context,
          expectedArtifact: "draft_reply",
          draftToRevise: resolved.draftToRevise,
          baseMessages,
        });
        return;
      }

      const generationRun = {
        runId: createId("generation"),
        cancelled: false,
        controller: new AbortController(),
      };
      aiGenerationRun.current = generationRun;
      const isCurrentGeneration = () => aiGenerationRun.current === generationRun && !generationRun.cancelled;
      const isChatRequest = decision.kind === "chat";
      const userMessage: AiChatMessage = {
        id: createId("msg"),
        role: "user",
        content: userRequest,
        timestamp: new Date().toISOString(),
        kind: isChatRequest ? "chat" : "scan",
      };
      const messagesWithUser = [...baseMessages, userMessage];
      const pendingMessage: AiChatMessage = {
        id: createId("msg"),
        role: "assistant",
        content: isChatRequest ? chatPendingText(userRequest) : scanPendingText(userRequest),
        timestamp: new Date().toISOString(),
        kind: "status",
        pending: true,
      };
      setState((s) => ({
        ...s,
        customScanInput: "",
        aiChatConversationId: conversationId,
        aiChatMessages: [...messagesWithUser, pendingMessage],
        aiChatLoading: true,
      }));

      if (isChatRequest) {
        try {
          const reply = await completeAiChat(messagesWithUser, generationRun.controller.signal);
          if (!isCurrentGeneration()) return;
          const finalMessages = [...messagesWithUser, {
            id: createId("msg"),
            role: "assistant" as const,
            content: reply,
            timestamp: new Date().toISOString(),
            kind: "chat" as const,
          }];
          upsertAiConversationHistory(conversationId, finalMessages, { kind: "chat", query: userRequest });
        } catch (error) {
          if (!isCurrentGeneration() || isAbortError(error)) return;
          const message = sanitizeToolError(error, userRequest) || "Anna couldn't finish that reply.";
          const failedMessages: AiChatMessage[] = [
            ...messagesWithUser,
            {
              ...pendingMessage,
              pending: false,
              kind: "error",
              content: message,
              timestamp: new Date().toISOString(),
            },
          ];
          upsertAiConversationHistory(conversationId, failedMessages, { kind: "chat", query: userRequest });
          showToast(message);
        } finally {
          if (aiGenerationRun.current === generationRun) {
            aiGenerationRun.current = null;
            setState((s) => ({ ...s, aiChatLoading: false }));
          }
        }
        return;
      }

      setState((s) => ({
        ...s,
        isCustomScanning: true,
        scanError: "",
        scanStatus: "Planning scan strategy...",
        askItemActions: {},
        customRunProgress: { runId: "", question: userRequest, status: "queued", stage: "planning", stageKey: "planning", progress: {}, partial: {}, startedAt: "" },
      }));
      try {
        const runId = `cs_${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`;
        const scanPromise = client.startCustomScan({
          user_request: userRequest,
          mailbox: selectedOrPrimary(state.selectedMailboxes, state.mailbox),
          primary_count: CUSTOM_SCAN_MESSAGE_LIMIT,
          max_messages: CUSTOM_SCAN_MESSAGE_LIMIT,
          ai_provider: state.llmProvider,
          storage_provider: state.storageProvider,
          run_id: runId,
          wait_timeout_seconds: 45,
        });
        const started = await scanPromise;
        if (!isCurrentGeneration()) return;
        if (started.status === "failed" || started.error) {
          throw new Error(started.error || "Custom scan failed");
        }
        const completed = started.status === "done" && started.result
          ? started
          : await waitForCustomScanResult(client, runId, (status) => {
            setState((s) => ({
              ...s,
              customRunProgress: makeCustomRunProgress(s.customRunProgress!, status, { runId, question: userRequest }),
              scanStatus: scanStageLabel(status.stage, status.progress),
            }));
          }, generationRun.controller.signal);
        if (!isCurrentGeneration()) return;
        if (completed.status === "failed" || completed.error) {
          throw new Error(completed.error || "Custom scan failed");
        }
        await loadActiveCards();
        if (!isCurrentGeneration()) return;
        await loadRunHistory();
        if (!isCurrentGeneration()) return;
        await loadCustomPlans();
        if (!isCurrentGeneration()) return;
        const result = buildCustomRunResult(runId, (completed.result || {}) as Record<string, unknown>);
        const finalMessages: AiChatMessage[] = [
          ...messagesWithUser,
          {
            ...pendingMessage,
            pending: false,
            kind: "scan",
            content: result.summary || result.plan_description || "Anna finished scanning your inbox.",
            result,
            timestamp: new Date().toISOString(),
          },
        ];
        upsertAiConversationHistory(conversationId, finalMessages, { kind: "scan", query: userRequest, result });
        setState((s) => ({ ...s, scanStatus: "", customRunProgress: null }));
        showToast("Custom scan complete.");
      } catch (error) {
        if (!isCurrentGeneration() || isAbortError(error)) return;
        const message = sanitizeToolError(error, userRequest);
        const failedMessages: AiChatMessage[] = [
          ...messagesWithUser,
          {
            ...pendingMessage,
            pending: false,
            kind: "error",
            content: message,
            timestamp: new Date().toISOString(),
          },
        ];
        upsertAiConversationHistory(conversationId, failedMessages, { kind: "chat", query: userRequest });
        setState((s) => ({ ...s, scanError: message, customRunProgress: s.customRunProgress ? { ...s.customRunProgress, status: "failed", stageKey: "failed" } : s.customRunProgress }));
        showToast(message);
      } finally {
        if (aiGenerationRun.current === generationRun) {
          aiGenerationRun.current = null;
          setState((s) => ({ ...s, isCustomScanning: false, aiChatLoading: false }));
        }
      }
    },
    async startCustomScan() {
      const userRequest = state.customScanInput.trim();
      if (!userRequest || state.isCustomScanning) return;
      setState((s) => ({
        ...s,
        isCustomScanning: true,
        scanError: "",
        scanStatus: "Planning scan strategy...",
        askItemActions: {},
        customRunProgress: { runId: "", question: userRequest, status: "queued", stage: "planning", stageKey: "planning", progress: {}, partial: {}, startedAt: "" },
      }));
      try {
        const runId = `cs_${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`;
        const scanPromise = client.startCustomScan({
          user_request: userRequest,
          mailbox: selectedOrPrimary(state.selectedMailboxes, state.mailbox),
          primary_count: CUSTOM_SCAN_MESSAGE_LIMIT,
          max_messages: CUSTOM_SCAN_MESSAGE_LIMIT,
          ai_provider: state.llmProvider,
          storage_provider: state.storageProvider,
          run_id: runId,
          wait_timeout_seconds: 45,
        });
        const started = await scanPromise;
        if (started.status === "failed" || started.error) {
          throw new Error(started.error || "Custom scan failed");
        }
        const completed = started.status === "done" && started.result
          ? started
          : await waitForCustomScanResult(client, runId, (status) => {
            setState((s) => ({
              ...s,
              customRunProgress: makeCustomRunProgress(s.customRunProgress!, status, { runId, question: userRequest }),
              scanStatus: scanStageLabel(status.stage, status.progress),
            }));
          });
        if (completed.status === "failed" || completed.error) {
          throw new Error(completed.error || "Custom scan failed");
        }
        await loadActiveCards();
        await loadRunHistory();
        await loadCustomPlans();
        const result = buildCustomRunResult(runId, (completed.result || {}) as Record<string, unknown>);
        setState((s) => {
          const nextHistory = [{ query: userRequest, result, timestamp: new Date().toISOString(), kind: "scan" as const }, ...s.askHistory].slice(0, 30);
          persistAskHistory(nextHistory);
          return {
            ...s,
            scanStatus: "",
            customScanInput: "",
            askHistory: nextHistory,
            customRunProgress: null,
          };
        });
        showToast("Custom scan complete.");
      } catch (error) {
        const message = sanitizeToolError(error, userRequest);
        setState((s) => ({ ...s, scanError: message, customRunProgress: s.customRunProgress ? { ...s.customRunProgress, status: "failed", stageKey: "failed" } : s.customRunProgress }));
        showToast(message);
      } finally {
        setState((s) => ({ ...s, isCustomScanning: false }));
      }
    },
    async reRunCustomPlan(planId) {
      if (state.isCustomScanning) return;
      const plan = state.customPlans.find((item) => item.plan_id === planId);
      const question = plan?.user_request || "Re-run saved scan";
      setState((s) => ({
        ...s,
        isCustomScanning: true,
        scanError: "",
        scanStatus: "Re-running saved scan...",
        askItemActions: {},
        customRunProgress: { runId: "", question, status: "queued", stage: "planning_done", stageKey: "planning", progress: {}, partial: { plan: plan || {} }, startedAt: "" },
      }));
      try {
        const runId = `rr_${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`;
        const scanPromise = client.reRunCustomScan({
          plan_id: planId,
          mailbox: selectedOrPrimary(state.selectedMailboxes, state.mailbox),
          primary_count: CUSTOM_SCAN_MESSAGE_LIMIT,
          max_messages: CUSTOM_SCAN_MESSAGE_LIMIT,
          ai_provider: state.llmProvider,
          storage_provider: state.storageProvider,
          run_id: runId,
          wait_timeout_seconds: 45,
        });
        const started = await scanPromise;
        if (started.status === "failed" || started.error) {
          throw new Error(started.error || "Custom scan re-run failed");
        }
        const completed = started.status === "done" && started.result
          ? started
          : await waitForCustomScanResult(client, runId, (status) => {
            setState((s) => ({
              ...s,
              customRunProgress: makeCustomRunProgress(s.customRunProgress!, status, { runId, question }),
              scanStatus: scanStageLabel(status.stage, status.progress),
            }));
          });
        if (completed.status === "failed" || completed.error) {
          throw new Error(completed.error || "Custom scan re-run failed");
        }
        await loadActiveCards();
        await loadRunHistory();
        await loadCustomPlans();
        const result = buildCustomRunResult(runId, (completed.result || {}) as Record<string, unknown>);
        setState((s) => {
          const nextHistory = [{ query: question, result, timestamp: new Date().toISOString(), kind: "scan" as const }, ...s.askHistory].slice(0, 30);
          persistAskHistory(nextHistory);
          return { ...s, scanStatus: "", askHistory: nextHistory, customRunProgress: null };
        });
        showToast("Re-run complete.");
      } catch (error) {
        const message = sanitizeToolError(error, question);
        setState((s) => ({ ...s, scanError: message }));
        showToast(message);
      } finally {
        setState((s) => ({ ...s, isCustomScanning: false }));
      }
    },
    async deleteCustomPlan(planId) {
      try {
        await client.deleteCustomPlan(planId, state.storageProvider);
        setState((s) => ({ ...s, customPlans: s.customPlans.filter((p) => p.plan_id !== planId) }));
        showToast("Plan deleted.");
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async clearCards(category: string) {
      const mailbox = state.selectedMailboxes[0] || state.mailbox;
      if (!mailbox) return;
      try {
        const result = await client.clearCards(mailbox, category);
        if (result.ok) {
          showToast(`${result.removed} card${result.removed !== 1 ? "s" : ""} cleared from ${category}.`);
          await loadActiveCards();
        }
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async clearHistory() {
      try {
        const result = await client.clearHistory();
        if (result.ok) {
          persistAskHistory([]);
          setState((s) => ({ ...s, askHistory: [], aiChatMessages: [], aiChatConversationId: "" }));
          showToast("History cleared.");
        }
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async resetAllData() {
      try {
        const result = await client.resetAllData();
        if (result.ok) {
          persistAskHistory([]);
          setState((s) => ({ ...s, cards: [], allCards: [], scanState: null, askHistory: [], aiChatMessages: [], aiChatConversationId: "", customPlans: [], lowerPriorityOpen: false, expandedDetails: {}, cleanupReadState: {}, markingReadIds: {}, attachmentDownloads: {}, askItemActions: {}, askEditDraft: {}, askGapAnswers: {}, askDraftsByKey: {}, gapAnswersByCard: {}, threadSummaryById: {}, draftById: {}, draftPreferencesById: {}, replyIntentById: {}, replyModeById: {}, revisionById: {}, threadContextExpanded: {} }));
          showToast("All data reset. Ready for a fresh start.");
          window.location.reload();
        }
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async resetMailboxScanHistory(mailbox) {
      try {
        const normalized = normalizedMailbox(mailbox);
        if (!normalized) return;
        const result = await client.resetMailboxScanHistory(normalized, state.storageProvider);
        if (result.ok) {
          setState((s) => {
            const allCards = s.allCards.filter((card) => cardMailbox(card, s.mailbox) !== normalized);
            const visible = filterCardsByMailboxes(allCards, activeBriefMailboxes(s.selectedMailboxes, s.briefMailboxFilter, s.mailbox));
            return {
              ...s,
              allCards,
              cards: visible,
              actionCount: actionCount(visible),
              scanState: null,
              selectedCard: cardMailbox(s.selectedCard, s.mailbox) === normalized ? null : s.selectedCard,
              selectedCardDetail: cardMailbox(s.selectedCard, s.mailbox) === normalized ? null : s.selectedCardDetail,
              expandedDetails: {},
              cleanupBundle: null,
              cleanupReadState: {},
              markingReadIds: {},
              attachmentDownloads: {},
            };
          });
          await loadMailboxes();
          await loadActiveCards(undefined, "all");
          await loadRunHistory();
          const deleted = result.deleted || {};
          showToast(`Reset ${deleted.keys || 0} scan record${deleted.keys === 1 ? "" : "s"} for ${normalized}.`);
        }
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async resetAndStartScan(mailbox) {
      const normalized = normalizedMailbox(mailbox);
      if (!normalized) return;
      const scanRequest = await resolveScanRequest(normalized);
      if (!scanRequest) return;
      setState((s) => ({
        ...s,
        isPreparingScan: true,
        isScanning: false,
        scanError: "",
        scanStatus: "Preparing fresh scan...",
        scanStepIndex: 0,
        scanStage: "scan",
        scanProgress: {},
        resultFilter: "all",
      }));
      try {
        await actions.resetMailboxScanHistory(normalized);
      } catch (error) {
        setState((s) => ({ ...s, isPreparingScan: false }));
        throw error;
      }
      await runBriefScan(scanRequest, "reset");
    },
    async deleteMailboxData(mailbox) {
      try {
        const normalized = normalizedMailbox(mailbox);
        const result = await client.deleteMailboxData(normalized);
        if (result.ok) {
          const deleted = result.deleted || {};
          const remainingMailboxes = state.mailboxes.filter((m) => normalizedMailbox(m.email) !== normalized);
          const remainingSelected = state.selectedMailboxes.filter((m) => normalizedMailbox(m) !== normalized);
          const nextMailbox = state.mailbox === normalized ? "all" : state.mailbox;
          setState((s) => ({
            ...s,
            mailboxes: remainingMailboxes,
            selectedMailboxes: remainingSelected,
            briefMailboxFilter: remainingSelected,
            mailbox: nextMailbox,
            cards: nextMailbox === "all" ? s.cards : filterCardsByMailboxes(s.allCards, remainingSelected),
            allCards: nextMailbox === "all" ? s.allCards : s.allCards.filter((c) => normalizedMailbox(cardMailbox(c, s.mailbox)) !== normalized),
            actionCount: actionCount(nextMailbox === "all" ? s.cards : filterCardsByMailboxes(s.allCards, remainingSelected)),
          }));
          showToast(`Deleted ${deleted.keys || 0} records, ${deleted.cache_keys || 0} cached emails, ${deleted.history_entries || 0} history entries for ${normalized}.`);
          await loadActiveCards();
        }
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async handleAskMarkRead(actionKey, messageId, mailboxOverride) {
      if (!messageId || state.askItemActions[actionKey]?.read) return;
      setState((s) => ({ ...s, askItemActions: { ...s.askItemActions, [actionKey]: { ...s.askItemActions[actionKey], read: true } } }));
      try {
        const result = await client.markReadFromAsk(selectedOrPrimary([mailboxOverride || ""], state.mailbox), [messageId]);
        if (!result.ok) throw new Error(result.error || "Failed to mark as read");
      } catch (error) {
        setState((s) => ({ ...s, askItemActions: { ...s.askItemActions, [actionKey]: { ...s.askItemActions[actionKey], read: false } } }));
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async handleAskTrash(actionKey, messageId, mailboxOverride) {
      if (!messageId || state.askItemActions[actionKey]?.trashed) return;
      setState((s) => ({ ...s, askItemActions: { ...s.askItemActions, [actionKey]: { ...s.askItemActions[actionKey], trashed: true } } }));
      try {
        const result = await client.trashFromAsk(selectedOrPrimary([mailboxOverride || ""], state.mailbox), [messageId]);
        if (!result.ok) throw new Error(result.error || "Failed to trash email");
      } catch (error) {
        setState((s) => ({ ...s, askItemActions: { ...s.askItemActions, [actionKey]: { ...s.askItemActions[actionKey], trashed: false } } }));
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    enterAskDraftEdit(key, draft) {
      setState((s) => ({ ...s, askEditDraft: { ...s.askEditDraft, [key]: draft } }));
    },
    updateAskDraft(key, value) {
      setState((s) => ({ ...s, askEditDraft: { ...s.askEditDraft, [key]: value } }));
    },
    cancelAskDraft(key) {
      setState((s) => {
        const askEditDraft = { ...s.askEditDraft };
        delete askEditDraft[key];
        return { ...s, askEditDraft };
      });
    },
    async sendAskDraft(key, threadId, to, mailboxOverride, draftOverride) {
      const draft = ((draftOverride ?? state.askEditDraft[key]) || "").trim();
      if (!draft) {
        showToast("Draft is empty.");
        return;
      }
      setState((s) => ({ ...s, askItemActions: { ...s.askItemActions, [key]: { ...s.askItemActions[key], sending: true } } }));
      try {
        const result = await client.replyFromAsk({ mailbox: selectedOrPrimary([mailboxOverride || ""], state.mailbox), thread_id: threadId, to_addr: to, body: draft, reply_mode: "reply_to_sender", dry_run: false });
        if (result.ok && !result.dry_run) {
          setState((s) => {
            const askEditDraft = { ...s.askEditDraft };
            delete askEditDraft[key];
            return { ...s, askEditDraft, askItemActions: { ...s.askItemActions, [key]: { ...s.askItemActions[key], sending: false, replied: true } } };
          });
          showToast("Reply sent.");
        } else {
          throw new Error(result.error || "Failed to send reply.");
        }
      } catch (error) {
        setState((s) => ({ ...s, askItemActions: { ...s.askItemActions, [key]: { ...s.askItemActions[key], sending: false } } }));
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    toggleAskHistory(idx) {
      setState((s) => ({ ...s, askHistoryExpanded: { ...s.askHistoryExpanded, [idx]: !s.askHistoryExpanded[idx] } }));
    },
    async copyDraft(text) {
      try {
        await navigator.clipboard.writeText(text);
        showToast("Draft copied");
      } catch {
        showToast("Copy failed");
      }
    },
    setGapAnswers(cardKey, answers) {
      setState((s) => ({ ...s, gapAnswersByCard: { ...s.gapAnswersByCard, [cardKey]: answers } }));
    },
    async generateDraftWithAnswers(answers) {
      await (actions as any).generateDraft(answers);
    },
    setAskGapAnswers(actionKey, answers) {
      setState((s) => ({ ...s, askGapAnswers: { ...s.askGapAnswers, [actionKey]: answers } }));
    },
    async generateAskDraftWithAnswers(actionKey, item, answers, mailboxOverride) {
      const mailbox = mailboxOverride || state.selectedMailboxes[0] || state.mailbox;
      setState((s) => ({ ...s, askItemActions: { ...s.askItemActions, [actionKey]: { ...s.askItemActions[actionKey], sending: true } } }));
      try {
        const result = await client.generateAskDraft({
          mailbox,
          message_id: item.message_id || "",
          thread_id: item.thread_id || "",
          from_addr: item.from || "",
          subject: item.subject || "",
          user_answers: answers,
          ai_provider: state.llmProvider,
        });
        const draftBody = result.body || result.note || "";
        setState((s) => ({
          ...s,
          askDraftsByKey: { ...s.askDraftsByKey, [actionKey]: draftBody },
          askEditDraft: { ...s.askEditDraft, [actionKey]: draftBody },
          askItemActions: { ...s.askItemActions, [actionKey]: { ...s.askItemActions[actionKey], sending: false } },
        }));
        if (result.fallback_used) {
          showToast("Draft generation fell back — result may be incomplete.");
        }
      } catch (error) {
        setState((s) => ({ ...s, askItemActions: { ...s.askItemActions, [actionKey]: { ...s.askItemActions[actionKey], sending: false } } }));
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
  };

  return { state, setState, actions, toast, dismissToast, accountSwitchNotice, accountSwitchNoticeVisible, closeAccountSwitchNotice, initialize };
}
