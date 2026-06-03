import { useCallback, useMemo, useRef, useState } from "react";
import { MailAgentClient } from "../api/mailAgentClient";
import { makeCustomRunProgress, scanStageLabel, stageToStep } from "../features/brief/runHelpers";
import { connectRuntime } from "../runtime/runtimeLoader";
import type { AppState, FrontendCard, RunStatus } from "../types/mail";
import {
  CUSTOM_SCAN_MESSAGE_LIMIT,
  DEFAULT_MODE,
  POLL_INTERVAL_MS,
  POLL_LIMIT,
  requestForMode,
} from "./constants";
import { createInitialState } from "./state";

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
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

export interface AppActions {
  showToast(message: string): void;
  closeDrawers(): void;
  setView(view: "start" | "ask"): void;
  setInput(field: "customScanInput", value: string): void;
  setDraft(cardId: string, value: string): void;
  setRevision(cardId: string, value: string): void;
  setReplyMode(cardId: string, value: string): void;
  setResultFilter(value: AppState["resultFilter"]): void;
  toggleDetails(cardId: string): void;
  toggleLowerPriority(): void;
  toggleCustomTrace(): void;
  toggleThreadContext(cardId: string): void;
  toggleSnoozeMenu(cardId: string): void;
  setProvider(kind: "llm" | "storage", value: string): void;
  setDrawer(drawer: "sources" | "history" | "scanPlan", open: boolean): void;
  minimize(value: boolean): void;
  checkGmailAuth(mailboxOverride?: string): Promise<{ authorized: boolean; source: string }>;
  loadActiveCards(): Promise<void>;
  loadRunHistory(): Promise<void>;
  loadCustomPlans(): Promise<void>;
  loadScanPlan(): Promise<void>;
  saveScanPlanField(field: string, value: unknown): Promise<void>;
  startScan(reason?: string): Promise<void>;
  openCard(cardId: string): Promise<void>;
  summarizeSelectedThread(): Promise<void>;
  generateDraft(presetRevision?: string): Promise<void>;
  recordDecision(decision: string, cardId?: string): Promise<void>;
  replyNow(): Promise<void>;
  clearAllCards(): Promise<void>;
  markCleanupAsRead(cardId: string): Promise<void>;
  restoreCard(cardId: string): Promise<void>;
  snoozeCard(cardId: string, option: string): Promise<void>;
  startCustomScan(): Promise<void>;
  reRunCustomPlan(planId: string): Promise<void>;
  deleteCustomPlan(planId: string): Promise<void>;
  handleAskMarkRead(messageId: string): Promise<void>;
  handleAskTrash(messageId: string): Promise<void>;
  enterAskDraftEdit(key: string, draft: string): void;
  updateAskDraft(key: string, value: string): void;
  cancelAskDraft(key: string): void;
  sendAskDraft(key: string, threadId: string, to: string): Promise<void>;
  toggleAskHistory(idx: number): void;
  copyDraft(text: string): Promise<void>;
}

export function useAppController() {
  const [state, setState] = useState<AppState>(() => createInitialState());
  const [toast, setToast] = useState("");
  const toastTimer = useRef<number | null>(null);
  const runtimePromise = useRef<Promise<AppState["runtime"]> | null>(null);

  const getRuntime = useCallback(async () => {
    if (!runtimePromise.current) {
      runtimePromise.current = connectRuntime();
    }
    return runtimePromise.current;
  }, []);

  const client = useMemo(() => new MailAgentClient(getRuntime), [getRuntime]);

  const showToast = useCallback((message: string) => {
    setToast(message);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(""), 2200);
  }, []);

  const refreshStoredCardFields = useCallback((cards: FrontendCard[]) => {
    setState((s) => {
      const draftById = { ...s.draftById };
      const threadSummaryById = { ...s.threadSummaryById };
      const expandedDetails = { ...s.expandedDetails };
      for (const card of cards) {
        if (card.draft_reply && !draftById[card.id]) draftById[card.id] = card.draft_reply;
        if (card.thread_summary && !threadSummaryById[card.id]) {
          try {
            threadSummaryById[card.id] = JSON.parse(card.thread_summary);
          } catch {
          }
        }
        if (card.cardType === "cleanup_bundle" && !(card.id in expandedDetails)) expandedDetails[card.id] = true;
      }
      return { ...s, draftById, threadSummaryById, expandedDetails };
    });
  }, []);

  const loadActiveCards = useCallback(async (storageOverride?: string, mailboxOverride?: string) => {
    const provider = storageOverride ?? state.storageProvider;
    const mailbox = mailboxOverride ?? state.mailbox;
    try {
      const payload = await client.loadActiveCards(mailbox, provider);
      refreshStoredCardFields(payload.cards || []);
      setState((s) => ({ ...s, cards: payload.cards || [], actionCount: payload.action_count ?? 0, scanState: payload.scan_state || null, scanError: "", loading: false }));
    } catch (error) {
      setState((s) => ({ ...s, scanError: error instanceof Error ? error.message : String(error), cards: [], actionCount: 0, scanState: null, loading: false }));
    }
  }, [client, refreshStoredCardFields, state.mailbox, state.storageProvider]);

  const loadRunHistory = useCallback(async () => {
    try {
      const payload = await client.loadRunHistory();
      setState((s) => ({ ...s, history: Array.isArray(payload.history) ? payload.history : [] }));
    } catch {
      setState((s) => ({ ...s, history: [] }));
    }
  }, [client]);

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
      setState((s) => ({ ...s, scanPlan: plan }));
    } catch {
      setState((s) => ({ ...s, scanPlan: { time_range: "auto", max_messages: 50, schedule: "manual", include_newsletters: false, include_promotions: false } }));
    }
  }, [client, state.mailbox, state.storageProvider]);

  const checkGmailAuth = useCallback(async (mailboxOverride?: string): Promise<{ authorized: boolean; source: string }> => {
    const mailbox = mailboxOverride ?? state.mailbox;
    try {
      const result = await client.checkGmailAuth(mailbox);
      const status = { authorized: Boolean(result && result.authorized), source: (result && result.source) || "none" };
      setState((s) => ({ ...s, gmailAuthStatus: { checked: true, ...status } }));
      return status;
    } catch {
      setState((s) => ({ ...s, gmailAuthStatus: { checked: true, authorized: true, source: "unknown" } }));
      return { authorized: true, source: "unknown" };
    }
  }, [client, state.mailbox]);

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
    const runtime = await getRuntime();
    setState((s) => ({ ...s, runtime, loading: runtime.connected ? s.loading : false }));
    const mailbox = await discoverMailbox();
    const currentMailbox = mailbox || state.mailbox;
    const auth = await checkGmailAuth(currentMailbox);
    if (runtime.connected) {
      if (!auth.authorized) {
        setState((s) => ({ ...s, loading: false }));
        return;
      }
      await loadRunHistory();
      await loadCustomPlans();
      await loadScanPlan(currentMailbox);
      await loadActiveCards(undefined, currentMailbox);
    }
  }, [checkGmailAuth, client, discoverMailbox, getRuntime, loadActiveCards, loadCustomPlans, loadRunHistory, loadScanPlan, state.mailbox]);

  const actions: AppActions = {
    showToast,
    closeDrawers() {
      setState((s) => ({ ...s, sourcesOpen: false, historyOpen: false, originalOpen: false, scanPlanOpen: false, selectedCard: null }));
    },
    setView(view) {
      setState((s) => ({ ...s, view, sourcesOpen: false, historyOpen: false, originalOpen: false, scanPlanOpen: false, lowerPriorityOpen: view === "start" ? false : s.lowerPriorityOpen }));
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
    setReplyMode(cardId, value) {
      setState((s) => ({ ...s, replyModeById: { ...s.replyModeById, [cardId]: value } }));
    },
    setResultFilter(value) {
      setState((s) => ({ ...s, resultFilter: value }));
    },
    toggleDetails(cardId) {
      setState((s) => ({ ...s, expandedDetails: { ...s.expandedDetails, [cardId]: !s.expandedDetails[cardId] }, snoozeMenuCardId: "" }));
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
          void loadActiveCards(value);
          void loadRunHistory();
          void loadCustomPlans(value);
        }, 0);
      }
    },
    setDrawer(drawer, open) {
      setState((s) => ({
        ...s,
        sourcesOpen: drawer === "sources" ? open : false,
        historyOpen: drawer === "history" ? open : false,
        scanPlanOpen: drawer === "scanPlan" ? open : false,
        originalOpen: false,
      }));
      if (drawer === "scanPlan" && open) void loadScanPlan();
    },
    minimize(value) {
      setState((s) => ({ ...s, minimized: value }));
    },
    checkGmailAuth,
    loadActiveCards,
    loadRunHistory,
    loadCustomPlans,
    loadScanPlan,
    async saveScanPlanField(field, value) {
      try {
        await client.saveScanPlanField(state.mailbox, state.storageProvider, field, value);
        setState((s) => ({ ...s, scanPlan: { ...(s.scanPlan || {}), [field]: value, updated_at: new Date().toISOString() } }));
        showToast("Scan plan updated");
      } catch (error) {
        showToast("Failed: " + (error instanceof Error ? error.message : "unknown"));
      }
    },
    async startScan(reason = "manual") {
      if (!state.runtime.connected || state.isScanning) return;
      setState((s) => ({ ...s, isScanning: true, scanError: "", scanStepIndex: 0, scanStage: "", scanProgress: {}, resultFilter: "all" }));
      try {
        const started = await client.startBriefRun({
          user_request: requestForMode(state.strategyMode || DEFAULT_MODE),
          mailbox: state.mailbox,
          mode: state.strategyMode,
          primary_count: 30,
          max_messages: 50,
          ai_provider: state.llmProvider,
          storage_provider: state.storageProvider,
          reason,
        });
        if (!started.run_id) throw new Error(started.error || "start_mail_agent_run did not return a run id");
        for (let poll = 0; poll < POLL_LIMIT; poll += 1) {
          await sleep(POLL_INTERVAL_MS);
          const status = await client.getRun(started.run_id);
          setState((s) => ({
            ...s,
            scanStepIndex: stageToStep(status.stage || ""),
            scanStage: status.stage || "",
            scanProgress: status.progress || {},
          }));
          if (status.status === "done") break;
          if (status.status === "failed") throw new Error(status.error || "Mail agent scan failed");
          if (poll === POLL_LIMIT - 1) throw new Error("Mail agent scan timed out");
        }
        await loadActiveCards();
        await loadRunHistory();
        setState((s) => ({ ...s, scanStatus: "Scan complete. Showing persisted attention cards." }));
        showToast("Scan complete.");
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((s) => ({ ...s, scanError: message, scanStatus: "" }));
        showToast(message);
      } finally {
        setState((s) => ({ ...s, isScanning: false }));
      }
    },
    async openCard(cardId) {
      const card = state.cards.find((item) => item.id === cardId && (!item.status || item.status === "pending"));
      if (!card) return;
      setState((s) => ({ ...s, selectedCard: card, selectedCardDetail: null, originalOpen: true, sourcesOpen: false, historyOpen: false, snoozeMenuCardId: "" }));
      try {
        const detail = await client.getCardDetail(state.mailbox, cardId, state.storageProvider);
        setState((s) => ({ ...s, selectedCardDetail: detail }));
      } catch {
      }
    },
    async summarizeSelectedThread() {
      if (!state.selectedCard || state.summarizingThread) return;
      const cardId = state.selectedCard.id;
      setState((s) => ({ ...s, summarizingThread: true }));
      try {
        const started = await client.startSummarizeThread({ mailbox: state.mailbox, card_id: cardId, storage_provider: state.storageProvider, ai_provider: state.llmProvider });
        if (!started.run_id) throw new Error(started.error || "start_summarize_thread did not return a run id");
        const result = await pollBackgroundRun(started.run_id);
        const summary = (result.summary || {}) as Record<string, unknown>;
        setState((s) => ({ ...s, threadSummaryById: { ...s.threadSummaryById, [cardId]: summary } }));
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      } finally {
        setState((s) => ({ ...s, summarizingThread: false }));
      }
    },
    async generateDraft(presetRevision) {
      if (!state.selectedCard || state.generatingDraft) return;
      const cardId = state.selectedCard.id;
      const currentDraft = state.draftById[cardId] || "";
      const revision = presetRevision || state.revisionById[cardId] || "";
      setState((s) => ({ ...s, generatingDraft: true }));
      try {
        const started = await client.startGenerateDraft({
          mailbox: state.mailbox,
          card_id: cardId,
          reply_mode: state.replyModeById[cardId] || "reply_to_sender",
          current_draft: currentDraft,
          revision_input: revision,
          storage_provider: state.storageProvider,
          ai_provider: state.llmProvider,
        });
        if (!started.run_id) throw new Error(started.error || "start_generate_draft did not return a run id");
        const result = await pollBackgroundRun(started.run_id);
        setState((s) => ({
          ...s,
          draftById: { ...s.draftById, [cardId]: resultToDraft(result, currentDraft) },
          revisionById: revision ? { ...s.revisionById, [cardId]: revision } : s.revisionById,
        }));
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      } finally {
        setState((s) => ({ ...s, generatingDraft: false, draftDots: "" }));
      }
    },
    async recordDecision(decision, cardId) {
      const cid = cardId || state.selectedCard?.id;
      if (!cid) return;
      try {
        await client.recordCardDecision({ mailbox: state.mailbox, card_id: cid, decision, storage_provider: state.storageProvider });
        setState((s) => ({ ...s, originalOpen: false, selectedCard: null, expandedDetails: { ...s.expandedDetails, [cid]: false } }));
        await loadActiveCards();
        showToast("Card removed from this briefing.");
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async replyNow() {
      if (!state.selectedCard) return;
      const draft = state.draftById[state.selectedCard.id] || "";
      if (!draft.trim()) {
        showToast("Draft is empty. Generate a draft first.");
        return;
      }
      try {
        await client.replyNow({
          mailbox: state.mailbox,
          card_id: state.selectedCard.id,
          draft_body: draft,
          reply_mode: state.replyModeById[state.selectedCard.id] || "reply_to_sender",
          dry_run: false,
        });
        setState((s) => ({ ...s, originalOpen: false, selectedCard: null }));
        showToast("Reply sent successfully.");
        await loadActiveCards();
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async clearAllCards() {
      try {
        await client.clearActiveCards(state.mailbox, state.storageProvider);
        setState((s) => ({ ...s, cards: [], actionCount: 0, scanState: null, expandedDetails: {}, cleanupReadState: {}, markingReadIds: {} }));
        showToast("All cards cleared.");
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async markCleanupAsRead(cardId) {
      const card = state.cards.find((c) => c.id === cardId);
      const messages = Array.isArray(card?.bundledMessages) ? card.bundledMessages : [];
      const messageIds = messages.map((m) => m.message_id || m.id).filter(Boolean) as string[];
      if (!card || !messageIds.length) return;
      setState((s) => ({ ...s, markingReadIds: { ...s.markingReadIds, [cardId]: true }, cleanupReadState: { ...s.cleanupReadState, [cardId]: { read: true, readMsgIndices: messages.map((_m, i) => i) } } }));
      try {
        const result = await client.markCleanupRead({ mailbox: state.mailbox, card_id: cardId, message_ids: messageIds, storage_provider: state.storageProvider });
        if (result.ok) {
          setState((s) => ({ ...s, cards: s.cards.filter((c) => c.id !== cardId) }));
          showToast(`${messageIds.length} emails marked as read in Gmail.`);
        } else {
          showToast(result.gmail_error || "Failed to mark as read in Gmail.");
        }
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      } finally {
        setState((s) => {
          const markingReadIds = { ...s.markingReadIds };
          delete markingReadIds[cardId];
          return { ...s, markingReadIds };
        });
      }
    },
    async restoreCard(cardId) {
      try {
        await client.restoreCard(state.mailbox, cardId, state.storageProvider);
        await loadActiveCards();
        showToast("Card restored.");
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async snoozeCard(cardId, option) {
      const optionMap: Record<string, string> = { tomorrow: "tomorrow", "next-week": "next_week", "dont-prioritize": "dont_prioritize" };
      try {
        await client.recordSnooze({ mailbox: state.mailbox, card_id: cardId, snooze_option: optionMap[option] || option, storage_provider: state.storageProvider });
        setState((s) => ({ ...s, snoozeMenuCardId: "" }));
        await loadActiveCards();
        showToast(option === "dont-prioritize" ? "Preference saved." : "Card snoozed.");
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
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
        const started = await client.startCustomScan({
          user_request: userRequest,
          mailbox: state.mailbox,
          primary_count: CUSTOM_SCAN_MESSAGE_LIMIT,
          max_messages: CUSTOM_SCAN_MESSAGE_LIMIT,
          ai_provider: state.llmProvider,
          storage_provider: state.storageProvider,
        });
        if (!started.run_id) throw new Error(started.error || "start_custom_scan did not return a run id");
        let progress = makeCustomRunProgress(null, started, { runId: started.run_id, question: userRequest, startedAt: started.started_at || "" });
        setState((s) => ({ ...s, customRunProgress: progress }));
        for (let poll = 0; poll < POLL_LIMIT; poll += 1) {
          await sleep(POLL_INTERVAL_MS);
          const status = await client.getRun(started.run_id);
          progress = makeCustomRunProgress(progress, status, { runId: started.run_id, question: userRequest });
          setState((s) => ({ ...s, customRunProgress: progress, scanStatus: scanStageLabel(status.stage, status.progress) }));
          if (status.status === "done") break;
          if (status.status === "failed") throw new Error(status.error || "Custom scan failed");
          if (poll === POLL_LIMIT - 1) throw new Error("Custom scan timed out");
        }
        await loadActiveCards();
        await loadRunHistory();
        await loadCustomPlans();
        const doneStatus = await client.getRun(started.run_id);
        const result = buildCustomRunResult(started.run_id || "", doneStatus.result || {});
        setState((s) => ({
          ...s,
          scanStatus: "",
          customScanInput: "",
          askHistory: [{ query: userRequest, result, timestamp: new Date().toISOString() }, ...s.askHistory],
          customRunProgress: null,
        }));
        showToast("Custom scan complete.");
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((s) => ({ ...s, scanError: message, customRunProgress: s.customRunProgress ? { ...s.customRunProgress, status: "failed", stageKey: "failed" } : s.customRunProgress }));
        showToast(message);
      } finally {
        setState((s) => ({ ...s, isCustomScanning: false }));
      }
    },
    async reRunCustomPlan(planId) {
      if (state.isCustomScanning) return;
      const plan = state.customPlans.find((item) => item.plan_id === planId);
      setState((s) => ({
        ...s,
        isCustomScanning: true,
        scanError: "",
        scanStatus: "Re-running saved scan...",
        askItemActions: {},
        customRunProgress: { runId: "", question: plan?.user_request || "Re-run saved scan", status: "queued", stage: "planning_done", stageKey: "planning", progress: {}, partial: { plan: plan || {} }, startedAt: "" },
      }));
      try {
        const started = await client.reRunCustomScan({
          plan_id: planId,
          mailbox: state.mailbox,
          primary_count: CUSTOM_SCAN_MESSAGE_LIMIT,
          max_messages: CUSTOM_SCAN_MESSAGE_LIMIT,
          ai_provider: state.llmProvider,
          storage_provider: state.storageProvider,
        });
        if (!started.run_id) throw new Error(started.error || "re_run_custom_scan did not return a run id");
        let progress = makeCustomRunProgress(null, started, { runId: started.run_id, question: plan?.user_request || "Re-run saved scan" });
        for (let poll = 0; poll < POLL_LIMIT; poll += 1) {
          await sleep(POLL_INTERVAL_MS);
          const status = await client.getRun(started.run_id);
          progress = makeCustomRunProgress(progress, status, { runId: started.run_id, question: plan?.user_request || "Re-run saved scan" });
          setState((s) => ({ ...s, customRunProgress: progress, scanStatus: scanStageLabel(status.stage, status.progress) }));
          if (status.status === "done") break;
          if (status.status === "failed") throw new Error(status.error || "Custom scan re-run failed");
          if (poll === POLL_LIMIT - 1) throw new Error("Custom scan re-run timed out");
        }
        await loadActiveCards();
        await loadRunHistory();
        await loadCustomPlans();
        const doneStatus = await client.getRun(started.run_id);
        const result = buildCustomRunResult(started.run_id || "", doneStatus.result || {});
        const query = plan?.user_request || "Re-run saved scan";
        setState((s) => ({ ...s, scanStatus: "", askHistory: [{ query, result, timestamp: new Date().toISOString() }, ...s.askHistory], customRunProgress: null }));
        showToast("Re-run complete.");
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
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
    async handleAskMarkRead(messageId) {
      if (!messageId || state.askItemActions[messageId]?.read) return;
      setState((s) => ({ ...s, askItemActions: { ...s.askItemActions, [messageId]: { ...s.askItemActions[messageId], read: true } } }));
      try {
        const result = await client.markReadFromAsk(state.mailbox, [messageId]);
        if (!result.ok) throw new Error(result.error || "Failed to mark as read");
      } catch (error) {
        setState((s) => ({ ...s, askItemActions: { ...s.askItemActions, [messageId]: { ...s.askItemActions[messageId], read: false } } }));
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async handleAskTrash(messageId) {
      if (!messageId || state.askItemActions[messageId]?.trashed) return;
      setState((s) => ({ ...s, askItemActions: { ...s.askItemActions, [messageId]: { ...s.askItemActions[messageId], trashed: true } } }));
      try {
        const result = await client.trashFromAsk(state.mailbox, [messageId]);
        if (!result.ok) throw new Error(result.error || "Failed to trash email");
      } catch (error) {
        setState((s) => ({ ...s, askItemActions: { ...s.askItemActions, [messageId]: { ...s.askItemActions[messageId], trashed: false } } }));
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
    async sendAskDraft(key, threadId, to) {
      const draft = (state.askEditDraft[key] || "").trim();
      if (!draft) {
        showToast("Draft is empty.");
        return;
      }
      setState((s) => ({ ...s, askItemActions: { ...s.askItemActions, [key]: { ...s.askItemActions[key], sending: true } } }));
      try {
        const result = await client.replyFromAsk({ mailbox: state.mailbox, thread_id: threadId, to_addr: to, body: draft, reply_mode: "reply_to_sender", dry_run: false });
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
  };

  return { state, setState, actions, toast, initialize };
}
