import { useCallback, useMemo, useRef, useState } from "react";
import { MailAgentClient } from "../api/mailAgentClient";
import { makeCustomRunProgress, scanProgressLabel, scanStageLabel, stageToStep } from "../features/brief/runHelpers";
import { connectRuntime } from "../runtime/runtimeLoader";
import type { ActiveCardsPayload, AppState, CleanupMessage, CustomRunResultItem, FrontendCard, GmailErrorPopup, MailboxInfo, RunStatus, ScanState } from "../types/mail";
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

function normalizedMailbox(mailbox: string | undefined): string {
  return String(mailbox || "").trim().toLowerCase();
}

function cardMailbox(card: FrontendCard | null | undefined, fallback = ""): string {
  return normalizedMailbox(card?.details?.mailbox || fallback);
}

function cardUiKey(card: FrontendCard, fallbackMailbox = ""): string {
  return `${cardMailbox(card, fallbackMailbox)}::${card.id}`;
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
  return cards.filter((card) => card.status !== "dismissed" && (card.priority || "" as string) !== "low" && card.priority !== "ignore").length;
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

function findCard(cards: FrontendCard[], keyOrId: string): FrontendCard | undefined {
  return cards.find((card) => card.uiKey === keyOrId) || cards.find((card) => card.id === keyOrId);
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
  setDrawer(drawer: "sources" | "history" | "memory" | "scanPlan", open: boolean): void;
  minimize(value: boolean): void;
  checkGmailAuth(mailboxOverride?: string): Promise<{ authorized: boolean; source: string }>;
  checkAnyGmailAuth(): Promise<{ authorized: boolean; source: string }>;
  closeGmailErrorPopup(): void;
  loadMailboxes(): Promise<void>;
  setMailboxSelected(mailbox: string, selected: boolean): Promise<void>;
  setBriefMailboxFilter(mailboxes: string[]): void;
  loadActiveCards(): Promise<void>;
  loadRunHistory(): Promise<void>;
  loadContactMemories(): Promise<void>;
  openContactMemory(mailbox: string, contactEmail: string): Promise<void>;
  closeContactMemory(): void;
  deleteContactMemory(mailbox: string, contactEmail: string): Promise<void>;
  clearContactMemories(): Promise<void>;
  loadCustomPlans(): Promise<void>;
  loadScanPlan(): Promise<void>;
  saveScanPlanField(field: string, value: unknown): void;
  setConfigMailbox(mailbox: string): Promise<void>;
  startScan(reason?: string): Promise<void>;
  openCard(cardId: string): Promise<void>;
  summarizeSelectedThread(): Promise<void>;
  generateDraft(presetRevision?: string): Promise<void>;
  recordDecision(decision: string, cardId?: string): Promise<void>;
  replyNow(): Promise<void>;
  clearAllCards(): Promise<void>;
  markCleanupAsRead(cardId: string): Promise<void>;
  restoreCard(cardId: string, mailbox?: string): Promise<void>;
  snoozeCard(cardId: string, option: string, reasons?: string[]): Promise<void>;
  openSnoozeReasons(cardId: string): void;
  closeSnoozeReasons(): void;
  openSourcesWithConfig(): void;
  startCustomScan(): Promise<void>;
  reRunCustomPlan(planId: string): Promise<void>;
  deleteCustomPlan(planId: string): Promise<void>;
  clearCards(category: string): Promise<void>;
  clearHistory(): Promise<void>;
  resetAllData(): Promise<void>;
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

  const loadActiveCards = useCallback(async (storageOverride?: string, mailboxOverride?: string) => {
    const provider = storageOverride ?? state.storageProvider;
    const mailbox = mailboxOverride ?? "all";
    const PAGE_SIZE = 50;
    try {
      const allCards: FrontendCard[] = [];
      let offset = 0;
      let hasMore = true;
      let scanState: ActiveCardsPayload["scan_state"];
      const cleanupItems: NonNullable<ActiveCardsPayload["cleanup_bundle"]> = [];
      const MAX_PAGES = 20;
      const CLEANUP_PAGE = 100;
      let cleanupOffset = 0;
      let cleanupHasMore = true;
      while (hasMore && offset < MAX_PAGES * PAGE_SIZE) {
        const isFirstCardPage = offset === 0;
        const payload = await client.loadActiveCards(
          mailbox, provider, offset, PAGE_SIZE,
          isFirstCardPage, cleanupOffset, isFirstCardPage ? CLEANUP_PAGE : 0,
        );
        // Only first page includes cleanup; subsequent card pages skip it
        const gotCleanup = isFirstCardPage && payload.cleanup_bundle;
        allCards.push(...(payload.cards || []));
        hasMore = Boolean(payload.has_more);
        offset += PAGE_SIZE;
        if (payload.scan_state) scanState = payload.scan_state;
        if (gotCleanup) {
          cleanupItems.push(...payload.cleanup_bundle!);
          cleanupOffset += CLEANUP_PAGE;
          cleanupHasMore = Boolean(payload.cleanup_has_more);
        }
      }
      // Load remaining cleanup pages
      while (cleanupHasMore && cleanupOffset < MAX_PAGES * CLEANUP_PAGE) {
        const payload = await client.loadActiveCards(
          mailbox, provider, 0, 1,
          true, cleanupOffset, CLEANUP_PAGE,
        );
        if (payload.cleanup_bundle) {
          cleanupItems.push(...payload.cleanup_bundle);
          cleanupOffset += CLEANUP_PAGE;
          cleanupHasMore = Boolean(payload.cleanup_has_more);
        } else {
          break;
        }
      }
      const cleanupBundle = cleanupItems.length > 0 ? cleanupItems : undefined;
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
          cleanupBundle: cleanupBundle ?? s.cleanupBundle,
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
      setState((s) => ({ ...s, scanPlan: plan }));
    } catch {
      setState((s) => ({ ...s, scanPlan: { scan_window_days: 7, max_messages: 100, scan_categories: [] } }));
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
      setState((s) => ({ ...s, gmailAuthStatus: { checked: true, authorized: false, source: "error" } }));
      return { authorized: false, source: "error" };
    }
  }, [client, state.mailbox]);

  const loadMailboxes = useCallback(async (storageOverride?: string): Promise<{ mailboxes: MailboxInfo[]; selected: string[]; primary: string }> => {
    const provider = storageOverride ?? state.storageProvider;
    try {
      const payload = await client.listMailboxes(provider);
      const mailboxes = Array.isArray(payload.mailboxes) ? payload.mailboxes : [];
      const selected = (Array.isArray(payload.selected) && payload.selected.length
        ? payload.selected
        : mailboxes.filter((item) => item.selected !== false).map((item) => item.email)
      ).map(normalizedMailbox).filter(Boolean);
      const primary = selectedOrPrimary(selected, mailboxes[0]?.email || state.mailbox);
      setState((s) => ({
        ...s,
        mailboxes,
        selectedMailboxes: selected,
        briefMailboxFilter: selected,
        mailbox: primary || s.mailbox,
        gmailAuthStatus: {
          checked: true,
          authorized: mailboxes.length ? mailboxes.some((item) => item.authorized !== false) : s.gmailAuthStatus.authorized,
          source: mailboxes.find((item) => item.email === primary)?.auth_source || s.gmailAuthStatus.source,
        },
      }));
      return { mailboxes, selected, primary };
    } catch {
      // 注册表不可用时回退到原来的单邮箱行为。
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
      const runtime = await getRuntime();
      setState((s) => ({ ...s, runtime, loading: runtime.connected ? s.loading : false }));
      const mailboxState = await loadMailboxes();
      let currentMailbox = mailboxState.primary;
      if (!currentMailbox) {
        const mailbox = await discoverMailbox();
        currentMailbox = mailbox || state.mailbox;
      }
      const authResult = await client.checkAnyGmailAuth();
      const systemAuthorized = Boolean(authResult?.authorized);
      const authWarning = (authResult as Record<string, unknown> | null | undefined)?.warning as string | undefined;
      setState((s) => ({ ...s, gmailAuthStatus: { checked: true, authorized: systemAuthorized, source: authResult?.source || "none" } }));
      if (authWarning) showToast(`Auth notice: ${authWarning}`);
      if (runtime.connected) {
        if (!systemAuthorized) {
          setState((s) => ({ ...s, loading: false }));
          return;
        }
        await loadRunHistory();
        await loadCustomPlans();
        await loadScanPlan(currentMailbox);
        await loadActiveCards(undefined, "all");
      }
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      showToast(`Init failed: ${msg}`);
      setState((s) => ({ ...s, loading: false }));
    }
  }, [client, discoverMailbox, getRuntime, loadActiveCards, loadCustomPlans, loadMailboxes, loadRunHistory, loadScanPlan, showToast, state.mailbox]);

  const actions: AppActions = {
    showToast,
    closeDrawers() {
      setState((s) => ({ ...s, sourcesOpen: false, historyOpen: false, memoryOpen: false, originalOpen: false, scanPlanOpen: false, selectedCard: null, expandAllConfigs: false }));
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
        originalOpen: false,
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
    async setMailboxSelected(mailbox, selected) {
      try {
        const payload = await client.setMailboxSelected(mailbox, selected, state.storageProvider);
        const mailboxes = Array.isArray(payload.mailboxes) ? payload.mailboxes : state.mailboxes.map((item) => item.email === mailbox ? { ...item, selected } : item);
        const selectedMailboxes = (Array.isArray(payload.selected) ? payload.selected : mailboxes.filter((item) => item.selected !== false).map((item) => item.email)).map(normalizedMailbox).filter(Boolean);
        const primary = selectedOrPrimary(selectedMailboxes, state.mailbox);
        setState((s) => ({
          ...s,
          mailboxes,
          selectedMailboxes,
          briefMailboxFilter: selectedMailboxes,
          mailbox: primary || s.mailbox,
          cards: filterCardsByMailboxes(s.allCards, selectedMailboxes),
          actionCount: actionCount(filterCardsByMailboxes(s.allCards, selectedMailboxes)),
        }));
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
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
    saveScanPlanField(field, value) {
      setState((s) => ({ ...s, scanPlan: { ...(s.scanPlan || {}), [field]: value, updated_at: new Date().toISOString() } }));
      const targetMailbox = state.configMailbox || "";
      client.saveScanPlanField(targetMailbox, state.storageProvider, field, value).catch(() => {});
    },
    async setConfigMailbox(mailbox) {
      setState((s) => ({ ...s, configMailbox: mailbox }));
      await loadScanPlan(mailbox || undefined);
    },
    async startScan(reason = "manual") {
      if (!state.runtime.connected || state.isScanning) return;
      const mailboxesToScan = (state.selectedMailboxes.length ? state.selectedMailboxes : [state.mailbox]).map(normalizedMailbox).filter(Boolean);
      if (!mailboxesToScan.length) {
        showToast("Select at least one mailbox.");
        return;
      }
      setState((s) => ({ ...s, isScanning: true, scanError: "", scanStatus: "", scanStepIndex: 0, scanStage: "scan", scanProgress: {}, resultFilter: "all" }));
      const contactMemoryJobs: Array<Record<string, unknown>> = [];
      try {
        const failures: string[] = [];
        for (let index = 0; index < mailboxesToScan.length; index += 1) {
          const mailbox = mailboxesToScan[index];
          const runId = `bg_${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`;
          // 先创建可轮询的 run；真实扫描和 LLM 进度由 continue 调用写入。
          const started = await client.startBriefRun({
            user_request: requestForMode(state.strategyMode || DEFAULT_MODE),
            mailbox,
            mode: state.strategyMode,
            primary_count: 30,
            max_messages: 50,
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
                user_request: requestForMode(state.strategyMode || DEFAULT_MODE),
                mailbox,
                mode: state.strategyMode,
                primary_count: 30,
                max_messages: 50,
                ai_provider: state.llmProvider,
                storage_provider: state.storageProvider,
              });
              applyBriefStatus(result);
              collectWarnings(result);
              const nextCardsVersion = Number(result.cards_version || 0);
              if (Number(result.cards_added || 0) > 0 || nextCardsVersion > cardsVersion) {
                cardsVersion = nextCardsVersion;
                await loadActiveCards();
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
          await loadActiveCards();
          // 卡片刷新到界面后，只记录联系人记忆补写任务；扫描主流程结束后再后台续跑。
          contactMemoryJobs.push({
            mailbox,
            since: started.started_at || result.started_at || "",
            ai_provider: state.llmProvider,
            storage_provider: state.storageProvider,
          });
        }
        await loadActiveCards();
        await loadRunHistory();
        const statusText = failures.length
          ? `Scan complete with ${failures.length} issue${failures.length === 1 ? "" : "s"}.`
          : "Scan complete. Showing persisted attention cards.";
        setState((s) => ({ ...s, scanStatus: statusText, scanError: s.scanError || failures.join("\n") }));
        showToast(failures.length ? statusText : "Scan complete.");
        if (contactMemoryJobs.length) {
          window.setTimeout(() => {
            void runContactMemoryBackfill(contactMemoryJobs);
          }, 0);
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((s) => ({ ...s, scanError: message, scanStatus: "" }));
        // Show popup for Gmail connectivity errors so users know to re-authorize
        if (message.toLowerCase().includes("gmail connection failed")) {
          const lastMailbox = mailboxesToScan.length > 0 ? mailboxesToScan[mailboxesToScan.length - 1] : state.mailbox;
          const popup: GmailErrorPopup = { mailbox: normalizedMailbox(lastMailbox) || state.mailbox, message };
          setState((s) => ({ ...s, gmailErrorPopup: popup }));
        }
        showToast(message);
      } finally {
        setState((s) => ({ ...s, isScanning: false }));
      }
    },
    async openCard(cardId) {
      const card = findCard(state.cards, cardId);
      if (card && card.status && card.status !== "pending") return;
      if (!card) return;
      setState((s) => ({ ...s, selectedCard: card, selectedCardDetail: null, originalOpen: true, sourcesOpen: false, historyOpen: false, memoryOpen: false, snoozeMenuCardId: "" }));
      try {
        const detail = await client.getCardDetail(cardMailbox(card, state.mailbox), card.id, state.storageProvider);
        setState((s) => ({ ...s, selectedCardDetail: detail }));
      } catch {
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
    async generateDraft(presetRevision, userAnswers?: Record<string, string>) {
      if (!state.selectedCard || state.generatingDraft) return;
      const cardId = state.selectedCard.id;
      const key = state.selectedCard.uiKey || cardUiKey(state.selectedCard, state.mailbox);
      const currentDraft = state.draftById[key] || "";
      const revision = presetRevision || state.revisionById[key] || "";
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
        const result = await pollBackgroundRun(started.run_id);
        setState((s) => ({
          ...s,
          draftById: { ...s.draftById, [key]: resultToDraft(result, currentDraft) },
          revisionById: revision ? { ...s.revisionById, [key]: revision } : s.revisionById,
        }));
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      } finally {
        setState((s) => ({ ...s, generatingDraft: false, draftDots: "" }));
      }
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
        setState((s) => ({ ...s, cards: [], actionCount: 0, scanState: null, expandedDetails: {}, cleanupReadState: {}, markingReadIds: {} }));
        showToast("All cards cleared.");
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    },
    async markCleanupAsRead(cardId) {
      const card = findCard(state.cards, cardId);
      const key = card?.uiKey || (card ? cardUiKey(card, state.mailbox) : cardId);
      const cardMbox = cardMailbox(card, state.mailbox);
      const bundle = Array.isArray(state.cleanupBundle) && state.cleanupBundle.length > 0 ? state.cleanupBundle : (Array.isArray(card?.bundledMessages) ? card.bundledMessages : []);
      const messages = cardMbox ? bundle.filter((m) => normalizedMailbox(m.mailbox ?? "") === normalizedMailbox(cardMbox)) : bundle;
      const messageIds = messages.map((m) => m.message_id || m.id).filter(Boolean) as string[];
      if (!card || !messageIds.length) return;
      setState((s) => ({ ...s, markingReadIds: { ...s.markingReadIds, [key]: true }, cleanupReadState: { ...s.cleanupReadState, [key]: { read: true, readMsgIndices: messages.map((_m, i) => i) } } }));
      try {
        const result = await client.markCleanupRead({ mailbox: cardMailbox(card, state.mailbox), card_id: card.id, message_ids: messageIds, storage_provider: state.storageProvider });
        if (!result.ok) {
          showToast(result.gmail_error || "Failed to mark as read in Gmail.");
        }
        await loadRunHistory();
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      } finally {
        setState((s) => {
          const markingReadIds = { ...s.markingReadIds };
          delete markingReadIds[key];
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
        });
        const pollTimer = window.setInterval(() => {
          client.getRun(runId).then((status) => {
            setState((s) => ({
              ...s,
              customRunProgress: makeCustomRunProgress(s.customRunProgress!, status, { runId, question: userRequest }),
              scanStatus: scanStageLabel(status.stage, status.progress),
            }));
          }).catch(() => {});
        }, POLL_INTERVAL_MS);
        const started = await scanPromise;
        window.clearInterval(pollTimer);
        if (started.status === "failed" || started.error) {
          throw new Error(started.error || "Custom scan failed");
        }
        await loadActiveCards();
        await loadRunHistory();
        await loadCustomPlans();
        const result = buildCustomRunResult(runId, (started.result || {}) as Record<string, unknown>);
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
        });
        const pollTimer = window.setInterval(() => {
          client.getRun(runId).then((status) => {
            setState((s) => ({
              ...s,
              customRunProgress: makeCustomRunProgress(s.customRunProgress!, status, { runId, question }),
              scanStatus: scanStageLabel(status.stage, status.progress),
            }));
          }).catch(() => {});
        }, POLL_INTERVAL_MS);
        const started = await scanPromise;
        window.clearInterval(pollTimer);
        if (started.status === "failed" || started.error) {
          throw new Error(started.error || "Custom scan re-run failed");
        }
        await loadActiveCards();
        await loadRunHistory();
        await loadCustomPlans();
        const result = buildCustomRunResult(runId, (started.result || {}) as Record<string, unknown>);
        setState((s) => ({ ...s, scanStatus: "", askHistory: [{ query: question, result, timestamp: new Date().toISOString() }, ...s.askHistory], customRunProgress: null }));
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
          setState((s) => ({ ...s, askHistory: [] }));
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
          setState((s) => ({ ...s, cards: [], allCards: [], scanState: null, askHistory: [], customPlans: [], lowerPriorityOpen: false, expandedDetails: {}, cleanupReadState: {}, markingReadIds: {}, askItemActions: {}, askEditDraft: {}, askGapAnswers: {}, askDraftsByKey: {}, gapAnswersByCard: {}, threadSummaryById: {}, draftById: {}, replyModeById: {}, revisionById: {}, threadContextExpanded: {} }));
          showToast("All data reset. Ready for a fresh start.");
          window.location.reload();
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
      await (actions as any).generateDraft(undefined, answers);
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

  return { state, setState, actions, toast, initialize };
}
