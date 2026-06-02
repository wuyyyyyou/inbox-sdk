"use strict";

import { AnnaAppRuntime } from "./anna-app-runtime-loader.js";

const TOOL_ID = "tool-zhaopy-inbox-tool-373sf2et";
const DEFAULT_MAILBOX = "hr@anna.partners";
const MAILBOX_STORAGE_KEY = "anna-inbox-mailbox";
const DEFAULT_MODE = "default_secretary";
const SCAN_INTERVAL_MS = 15 * 60 * 1000;
const POLL_INTERVAL_MS = 2500;
const POLL_LIMIT = 240;
const CUSTOM_SCAN_MESSAGE_LIMIT = 50;

const SCAN_STEPS = [
  { title: "Connecting mailbox", microcopy: "Connecting to your Gmail source and reading recent activity.", stagePrefix: "scan" },
  { title: "Processing messages", microcopy: "Filtering duplicates, deduplicating threads, and running first-pass classification.", stagePrefix: "storage_filter" },
  { title: "Reading context", microcopy: "Fetching thread context and message details for candidates.", stagePrefix: "read_context" },
  { title: "Evaluating items", microcopy: "LLM is reviewing each candidate and preparing judgments.", stagePrefix: "evaluate" },
  { title: "Preparing brief", microcopy: "Building action plan and saving cards to local storage.", stagePrefix: "plan" },
];

const CATEGORY_TABS = [
  { id: "all", label: "All" },
  { id: "reply", label: "Needs reply" },
  { id: "review", label: "Needs review" },
  { id: "cleanup", label: "Cleanup" },
];

const CATEGORY_NOTE = {
  reply: "People waiting for your response.",
  review: "No reply needed, but worth a quick look.",
  cleanup: "Low-signal mail Anna grouped away from your action queue.",
};

const ITEM_TYPE_CATEGORY = {
  reply_required: "reply",
  confirmation_required: "reply",
  business_or_creator_thread: "reply",
  security_risk: "review",
  billing_or_subscription: "review",
  account_notice: "review",
  low_value_cleanup: "cleanup",
};

function cardCategory(card) {
  if (card.userAction === "reply") return "reply";
  if (card.userAction === "review") return "review";
  if (card.userAction === "cleanup") return "cleanup";
  // backward compat: old cards without userAction
  if (card.cardType === "cleanup_bundle") return "cleanup";
  if (!isMainCard(card)) return "cleanup";
  if (card.item_type && ITEM_TYPE_CATEGORY[card.item_type]) {
    return ITEM_TYPE_CATEGORY[card.item_type];
  }
  return "review";
}

function filteredCards(category) {
  const visible = visibleCards();
  if (category === "all") return visible;
  return visible.filter(card => cardCategory(card) === category);
}

function filteredCount(category) {
  return filteredCards(category).length;
}

function currentDisplayList() {
  if (state.resultFilter === "all") {
    return [...mainCards(), ...lowerCards()];
  }
  return filteredCards(state.resultFilter);
}

function resolvedCards() {
  return state.cards.filter(c => c.status && c.status !== "pending");
}

function nextCardId() {
  if (!state.selectedCard) return null;
  const list = currentDisplayList();
  const idx = list.findIndex(c => c.id === state.selectedCard.id);
  if (idx < 0 || idx >= list.length - 1) return null;
  return list[idx + 1].id;
}

const content = document.getElementById("appContent");
const bottomBar = document.getElementById("bottomBar");
const toast = document.getElementById("toast");
const shell = document.getElementById("appShell");
const minimizedPill = document.getElementById("minimizedPill");
const annaAvatar = document.getElementById("annaAvatar");
const brandSubtitle = document.getElementById("brandSubtitle");
const connectionStatus = document.getElementById("connectionStatus");
const overlay = document.getElementById("drawerOverlay");
const sourcesDrawer = document.getElementById("sourcesDrawer");
const originalDrawer = document.getElementById("originalDrawer");
const historyDrawer = document.getElementById("historyDrawer");
const scanPlanDrawer = document.getElementById("scanPlanDrawer");

let annaClient = null;
let annaReady = null;

function getSavedMailbox() {
  try {
    return localStorage.getItem(MAILBOX_STORAGE_KEY) || DEFAULT_MAILBOX;
  } catch {
    return DEFAULT_MAILBOX;
  }
}

const state = {
  runtime: { connected: false, mode: "connecting" },
  view: "start",
  mailbox: getSavedMailbox(),
  mailboxInput: getSavedMailbox(),
  strategyMode: DEFAULT_MODE,
  loading: true,
  cards: [],
  scanState: null,
  history: [],
  scanStatus: "",
  scanError: "",
  isScanning: false,
  isCustomScanning: false,
  scanStepIndex: 0,
  scanStage: "",
  scanProgress: {},
  customPlans: [],
  customScanInput: "",
  customRunResult: null,
  customRunProgress: null,
  customTraceOpen: false,
  sourcesOpen: false,
  historyOpen: false,
  originalOpen: false,
  scanPlanOpen: false,
  scanPlan: null,
  selectedCard: null,
  selectedCardDetail: null,
  threadSummaryById: {},
  draftById: {},
  revisionById: {},
  replyModeById: {},
  threadContextExpanded: {},
  expandedDetails: {},
  snoozeMenuCardId: "",
  statusByCardId: {},
  lowerPriorityOpen: false,
  minimized: false,
  resultFilter: "all",
  llmProvider: "anna-llm",
  storageProvider: "aps",
  generatingDraft: false,
  draftDots: "",
  summarizingThread: false,
  cleanupReadState: {},  // cardId -> { read: bool, readMsgIndices: [] }
  markingReadIds: {},     // cardId -> true while Gmail batchModify is in flight
  gmailAuthStatus: { checked: false, authorized: true },  // 默认 true 避免闪烁
  askItemActions: {},  // { [message_id]: { replied, read, trashed, sending } }
  askEditDraft: {},     // { [key]: string } — 正在编辑中的草稿文本
};

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function pollBackgroundRun(runId) {
  for (let poll = 0; poll < 80; poll += 1) {  // 80 × 2.5s = 200s max
    await sleep(POLL_INTERVAL_MS);
    const status = await invokeTool("get_mail_agent_run", { run_id: runId });
    if (status.status === "done") return status.result;
    if (status.status === "failed") throw new Error(status.error || "Background task failed");
    if (poll === 79) throw new Error("Background task timed out after 200s");
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}

function formatBeijingTimestamp(value) {
  if (!value) return "";
  // Convert to number first for epoch ms strings (e.g. Gmail internalDate "1779773309000")
  const date = new Date(Number(value) || value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function showToast(message) {
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add("is-visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("is-visible"), 2200);
}

function storageProviderArgs() {
  return { storage_provider: state.storageProvider };
}

function aiProviderArgs() {
  return { ai_provider: state.llmProvider };
}

async function connectRuntime() {
  if (annaClient) return { connected: true, client: annaClient };

  try {
    annaClient = await AnnaAppRuntime.connect();
    try {
      await annaClient.window.set_title({ title: "Anna Inbox" });
    } catch {
    }
    return { connected: true, mode: "live", client: annaClient };
  } catch (error) {
    return { connected: false, mode: "mock", error: error.message || String(error) };
  }
}

function getAnna() {
  if (!annaReady) {
    annaReady = connectRuntime();
  }
  return annaReady;
}

async function checkGmailAuth() {
  try {
    const result = await invokeTool("check_gmail_auth", { mailbox: state.mailbox });
    state.gmailAuthStatus = {
      checked: true,
      authorized: Boolean(result && result.authorized),
      source: (result && result.source) || "none",
    };
  } catch {
    // 本地 dev mock 模式调用失败：回退到检测 token 文件
    state.gmailAuthStatus = { checked: true, authorized: true, source: "unknown" };
  }
}

function isValidMailbox(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(value || "").trim());
}

async function applyMailbox() {
  const nextMailbox = String(state.mailboxInput || "").trim().toLowerCase();
  if (!isValidMailbox(nextMailbox)) {
    showToast("Enter a valid email address");
    render();
    return;
  }
  if (nextMailbox === state.mailbox) {
    render();
    return;
  }

  state.mailbox = nextMailbox;
  state.mailboxInput = nextMailbox;
  state.cards = [];
  state.selectedCard = null;
  state.selectedCardDetail = null;
  state.history = [];
  state.scanPlan = null;
  state.customPlans = [];
  state.scanError = "";
  state.gmailAuthStatus = { checked: false, authorized: false, source: "none" };
  try {
    localStorage.setItem(MAILBOX_STORAGE_KEY, nextMailbox);
  } catch {
    // Local storage can be unavailable in hardened browser contexts.
  }
  showToast(`Mailbox: ${nextMailbox}`);
  render();

  await checkGmailAuth();
  if (state.runtime.connected && state.gmailAuthStatus.authorized) {
    await loadRunHistory();
    await loadCustomPlans();
    await loadScanPlan();
    await loadActiveCards();
  }
  render();
}

function unwrapToolResult(result) {
  // JSON-RPC envelope: {"jsonrpc":"2.0", "id":..., "result":...} — must carry "jsonrpc"
  const hostPayload = (result && typeof result === "object" && "result" in result && "jsonrpc" in result)
    ? result.result
    : result;
  // handle_invoke wrapper: {"success":true, "tool":"...", "data":...} — must carry "tool"
  const payload = (hostPayload && typeof hostPayload === "object" && "data" in hostPayload && "tool" in hostPayload)
    ? hostPayload.data
    : hostPayload;
  if (payload && typeof payload === "object" && payload.error) {
    throw new Error(String(payload.error));
  }
  if (payload && typeof payload === "object" && payload.success === false) {
    throw new Error(String(payload.error || "Tool call failed"));
  }
  if (payload && typeof payload === "object" && payload.success === true && "data" in payload) {
    return payload.data;
  }
  return payload;
}

const INVOKE_TIMEOUT_MS = 180000;  // 3 min — long enough for LLM draft/thread-summary calls

async function invokeTool(method, args = {}, options = {}) {
  const runtime = await getAnna();
  if (!runtime.connected) {
    throw new Error(runtime.error || "Anna runtime is not connected.");
  }
  const invokeArgs = {
    tool_id: TOOL_ID,
    method,
    args,
    timeoutMs: options.timeoutMs || INVOKE_TIMEOUT_MS,
  };
  // 线上 SDK 暴露 namespaced proxy；本地旧版 runtime 保留 call() 兼容路径。
  try {
    const result = runtime.client.tools && typeof runtime.client.tools.invoke === "function"
      ? await runtime.client.tools.invoke(invokeArgs)
      : await runtime.client.call("tools", "invoke", invokeArgs, { timeout: INVOKE_TIMEOUT_MS, ...options });
    return unwrapToolResult(result);
  } catch (error) {
    const details = error?.details || error?.data || {};
    const traceback = details?.traceback || details?.data?.traceback || "";
    const code = error?.code !== undefined ? `[${error.code}] ` : "";
    const message = error?.message || String(error);
    throw new Error(`${code}${message}${traceback ? `\n\n${traceback}` : ""}`);
  }
}

function isMainCard(card) {
  if (card.displaySection === "lower") return false;
  if (card.displaySection === "main") return true;
  const priority = String(card.priority || "").toLowerCase();
  if (priority === "critical" || priority === "high" || priority === "medium") return true;
  if (priority === "low" || priority === "ignore") return false;
  const label = String(card.label || "").toLowerCase();
  return card.status !== "snoozed" && card.status !== "resolved" && card.status !== "dismissed" &&
    !label.includes("safe") && !label.includes("cleanup") && !label.includes("low");
}

function visibleCards() {
  return state.cards.filter((card) => !card.status || card.status === "pending");
}

function mainCards() {
  return visibleCards().filter(isMainCard);
}

function lowerCards() {
  return visibleCards().filter((card) => !isMainCard(card));
}

function primaryAction(card) {
  const actions = Array.isArray(card.actions) ? card.actions : [];
  const primary = actions.find((action) => action.primary)
    || actions.find((action) => action.id !== "view")
    || { id: "handle", label: "Handle", buttonLabel: "" };
  return {
    id: primary.id,
    label: primary.buttonLabel || primary.label || "Handle",
  };
}

function normalizeRecommendation(text) {
  const value = String(text || "").trim();
  if (!value) return "Suggested: Review this item.";
  return value.toLowerCase().startsWith("suggested:") ? value : `Suggested: ${value}`;
}

function render() {
  if (state.view === "start" && state.originalOpen) {
    renderHandleView();
  } else if (state.view === "start") {
    renderStart();
  }
  if (state.view === "ask") renderAsk();
  if (state.view === "start" && state.isScanning) renderScanning();
  renderBottomBar();
  if (!state.originalOpen) {
    renderSourcesDrawer();
    renderOriginalDrawer();
    renderHistoryDrawer();
    renderScanPlanDrawer();
  }
  renderShellState();
  updateHeader();
}

function renderStart() {
  // ── Gmail authorization guide (takes priority over everything) ──
  if (state.gmailAuthStatus.checked && !state.gmailAuthStatus.authorized) {
    content.innerHTML = `
      <div class="first-run-layout">
        <section class="first-run-center" aria-label="Gmail authorization required">
          <div class="auth-orb" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <rect x="2" y="4" width="20" height="16" rx="2"/>
              <path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>
            </svg>
          </div>
          <h1 class="first-run-title">Connect your Gmail account</h1>
          <p class="first-run-copy">Authorize Anna to read and manage your inbox.</p>

          <div class="auth-guide-card">
            <div class="auth-guide-steps">
              <div class="auth-step">
                <span class="auth-step-num">1</span>
                <span>Open <strong>More → Authorizations</strong></span>
              </div>
              <div class="auth-step">
                <span class="auth-step-num">2</span>
                <span>Select <strong>Google</strong> → <strong>Connect with OAuth</strong></span>
              </div>
              <div class="auth-step">
                <span class="auth-step-num">3</span>
                <span>Tick these permissions:</span>
              </div>
              <div class="auth-perms">
                <span class="perm-chip">Gmail read</span>
                <span class="perm-chip">Gmail modify</span>
                <span class="perm-chip">Gmail compose</span>
                <span class="perm-chip">Gmail send</span>
                <span class="perm-chip">Calendar read</span>
                <span class="perm-chip">Calendar events</span>
              </div>
              <div class="auth-step">
                <span class="auth-step-num">4</span>
                <span>Click <strong>Authorize</strong> and return here</span>
              </div>
            </div>
          </div>

          <div class="first-run-actions">
            <button class="primary-btn" data-action="retry-auth-check">Check again</button>
            <button class="soft-btn" data-action="open-scan-plan">Scan setting</button>
          </div>
        </section>
      </div>
    `;
    return;
  }

  const cards = mainCards();
  const lower = lowerCards();
  const hasCards = visibleCards().length > 0;
  const totalScans = Number(state.scanState?.total_scans || 0);
  const isFresh = !state.loading && !state.isScanning && totalScans === 0 && !hasCards;

  if (isFresh) {
    content.innerHTML = `
      <div class="first-run-layout">
        <section class="first-run-center" aria-label="First inbox scan">
          <button class="scan-launch-btn" data-action="first-scan" aria-label="Start first scan" ${!state.runtime.connected ? "disabled" : ""}>
            <span class="scan-launch-core">
              <span class="scan-orb">A</span>
            </span>
          </button>
          <div>
            <h1 class="first-run-title">Let Anna take a first look.</h1>
            <p class="first-run-copy">She'll find what needs attention, and leave the noise behind.</p>
          </div>
          <div class="first-run-actions">
            <button class="primary-btn" data-action="first-scan" ${!state.runtime.connected ? "disabled" : ""}>Start scan</button>
            <button class="soft-btn" data-action="open-scan-plan">Scan setting</button>
          </div>
          ${state.scanError ? `<p class="assistant-copy is-error" style="margin-top:8px;">${escapeHtml(state.scanError)}</p>` : ""}
        </section>
      </div>
    `;
    return;
  }

  const title = state.loading
    ? "Anna is waking up..."
    : state.isScanning
      ? "Anna is scanning your inbox."
      : hasCards
        ? `I found ${cards.length} thing${cards.length === 1 ? "" : "s"} worth your attention.`
        : totalScans > 0
          ? "No attention cards right now."
          : "Welcome to Anna Inbox.";

  const activeFilter = state.resultFilter;
  const isAll = activeFilter === "all";
  const showCleanup = isAll || activeFilter === "cleanup";
  const totalVisible = visibleCards().length;
  const mainDisplayCards = isAll ? mainCards() : (activeFilter === "cleanup" || activeFilter === "review") ? filteredCards(activeFilter) : filteredCards(activeFilter).filter(isMainCard);

  const cleanupCards = lower.filter(c => c.cardType === "cleanup_bundle");
  const regularLower = lower.filter(c => c.cardType !== "cleanup_bundle");

  content.innerHTML = `
    <div class="start-grid">
      <section class="assistant-card">
        <div>
          <h1 class="assistant-says">${escapeHtml(title)}</h1>
          <div class="managed-mailboxes" aria-label="Watched mailboxes">
            <span class="mailbox-watch-chip">${escapeHtml(state.mailbox)}</span>
          </div>
          ${state.scanStatus ? `<p class="assistant-copy">${escapeHtml(state.scanStatus)}</p>` : ""}
          ${state.scanError ? `<p class="assistant-copy is-error">${escapeHtml(state.scanError)}</p>` : ""}
        </div>
      </section>

      ${totalVisible > 0 ? `
        <div class="result-filter-row">
          <div class="category-segment" role="tablist" aria-label="Card filters">
            ${CATEGORY_TABS.map(tab => `
              <button class="category-tab ${activeFilter === tab.id ? "is-active" : ""}" data-result-filter="${tab.id}" role="tab" aria-selected="${activeFilter === tab.id}">
                ${escapeHtml(tab.label)}&nbsp;${filteredCount(tab.id)}
              </button>
            `).join("")}
          </div>
        </div>
        ${!isAll && CATEGORY_NOTE[activeFilter] ? `<p class="category-note">${escapeHtml(CATEGORY_NOTE[activeFilter])}</p>` : ""}
      ` : ""}

      ${mainDisplayCards.length ? `
        <section class="noticed-section">
          <div class="attention-queue">
            ${mainDisplayCards.map(renderAttentionCard).join("")}
          </div>
        </section>
      ` : (totalVisible > 0 && !cleanupCards.length ? `<p class="assistant-copy" style="text-align:center;margin-top:12px;">No cards in this category.</p>` : "")}

      ${isAll && regularLower.length ? renderLowerPrioritySection(regularLower) : ""}

      ${showCleanup && cleanupCards.length ? cleanupCards.map(renderCleanupBundleCard).join("") : ""}
    </div>
  `;
}

function renderDebugProviderControls() {
  return `
    <div class="debug-provider-controls" aria-label="Debug providers">
      <div class="debug-provider-group" role="radiogroup" aria-label="LLM provider">
        <button class="debug-provider-btn ${state.llmProvider === "dashscope" ? "is-active" : ""}" data-debug-provider="llm:dashscope" aria-checked="${state.llmProvider === "dashscope"}" role="radio">DashScope</button>
        <button class="debug-provider-btn ${state.llmProvider === "anna-llm" ? "is-active" : ""}" data-debug-provider="llm:anna-llm" aria-checked="${state.llmProvider === "anna-llm"}" role="radio">Anna</button>
      </div>
      <div class="debug-provider-group" role="radiogroup" aria-label="Storage provider">
        <button class="debug-provider-btn ${state.storageProvider === "aps" ? "is-active" : ""}" data-debug-provider="storage:aps" aria-checked="${state.storageProvider === "aps"}" role="radio">APS</button>
        <button class="debug-provider-btn ${state.storageProvider === "local" ? "is-active" : ""}" data-debug-provider="storage:local" aria-checked="${state.storageProvider === "local"}" role="radio">Local</button>
      </div>
    </div>
  `;
}

function renderAttentionCard(card) {
  const expanded = Boolean(state.expandedDetails[card.id]);
  const snoozeOpen = state.snoozeMenuCardId === card.id;
  const status = state.statusByCardId[card.id];
  const action = primaryAction(card);
  const recommendation = normalizeRecommendation(card.recommendation);
  return `
    <article class="attention-item-card ${expanded ? "is-expanded" : ""}">
      <h2 class="attention-title">${escapeHtml(card.title || "Email thread needs review")}</h2>
      <p class="attention-field">${escapeHtml(card.summary || "")}</p>
      <p class="attention-field attention-recommendation">
        <span class="suggested-prefix">Suggested: </span>${escapeHtml(recommendation.replace(/^Suggested:\s*/i, ""))}
      </p>
      <div class="proposal-actions">
        <span class="snooze-wrap">
          <button class="soft-btn" data-snooze-card="${escapeHtml(card.id)}" aria-expanded="${snoozeOpen}">Snooze</button>
          ${snoozeOpen ? `
            <span class="snooze-menu" role="menu">
              <button class="snooze-option" role="menuitem" data-snooze-option="${escapeHtml(card.id)}:tomorrow">Tomorrow</button>
              <button class="snooze-option" role="menuitem" data-snooze-option="${escapeHtml(card.id)}:next-week">Next week</button>
              <button class="snooze-option" role="menuitem" data-snooze-option="${escapeHtml(card.id)}:dont-prioritize">Don't prioritize threads like this</button>
            </span>
          ` : ""}
        </span>
        <button class="primary-btn" data-handle-card="${escapeHtml(card.id)}:${escapeHtml(action.id || "handle")}">Handle</button>
      </div>
      <button class="link-btn" data-toggle-details="${escapeHtml(card.id)}">${expanded ? "Hide details" : "Details"}</button>
      ${expanded ? renderCardDetails(card) : ""}
      ${status ? `<div class="attention-status">${escapeHtml(status)}</div>` : ""}
    </article>
  `;
}

function renderCardDetails(card) {
  const details = card.details || {};
  const original = card.original || {};
  const body = String(original.body || "").trim();
  return `
    <div class="attention-details">
      <div class="attention-detail-grid">
        <div class="detail-line"><strong>Needs</strong>${escapeHtml(details.needs || "Review")}</div>
        <div class="detail-line"><strong>Latest activity</strong>${escapeHtml(details.latestActivity || "")}</div>
        <div class="detail-line"><strong>Anna reviewed</strong>${escapeHtml(details.reviewed || "")}</div>
        <div class="detail-line"><strong>Mailbox</strong>${escapeHtml(details.mailbox || state.mailbox)}</div>
      </div>
      ${body ? `
        <div class="original-email-block">
          ${original.thread ? `<div class="original-email-subject">${escapeHtml(original.thread)}</div>` : ""}
          <pre class="original-email-body">${escapeHtml(body)}</pre>
        </div>
      ` : ""}
    </div>
  `;
}

function renderLowerPrioritySection(cards) {
  if (!cards.length) return "";
  return `
    <section class="lower-priority">
      <button class="lower-priority-toggle" data-action="toggle-lower-priority" aria-expanded="${state.lowerPriorityOpen}">
        <span>
          <p class="lower-priority-title">Quiet for now</p>
          <p class="lower-priority-copy">${cards.length} lower-priority item${cards.length === 1 ? "" : "s"} from the same scan.</p>
        </span>
        <span class="attention-pill">${state.lowerPriorityOpen ? "Hide" : "Show"}</span>
      </button>
      ${state.lowerPriorityOpen ? `<div class="attention-queue">${cards.map(renderAttentionCard).join("")}</div>` : ""}
    </section>
  `;
}

function renderCleanupBundleCard(card) {
  const expanded = Boolean(state.expandedDetails[card.id]);
  const readState = state.cleanupReadState[card.id];
  const isAllRead = readState?.read;
  const messages = Array.isArray(card.bundledMessages) ? card.bundledMessages : [];
  const count = card.bundledCount || messages.length;
  const readCount = readState ? readState.readMsgIndices.length : 0;
  return `
    <article class="cleanup-bundle-card ${expanded ? "is-expanded" : ""} ${isAllRead ? "is-all-read" : ""}">
      <div class="cleanup-bundle-top">
        <button class="cleanup-bundle-header" data-toggle-details="${escapeHtml(card.id)}">
          <span class="cleanup-bundle-icon">${expanded ? "▾" : "▸"}</span>
          <span class="cleanup-bundle-info">
            <span class="cleanup-bundle-title">${escapeHtml(card.title)}</span>
            <span class="cleanup-bundle-hint">${escapeHtml(card.summary)}</span>
          </span>
          <span class="cleanup-bundle-count">${isAllRead ? "✓" : `${readCount}/${count}`}</span>
        </button>
        <button class="soft-btn" data-cleanup-mark-read="${escapeHtml(card.id)}" ${isAllRead || state.markingReadIds[card.id] ? "disabled" : ""}>${state.markingReadIds[card.id] ? "Marking…" : isAllRead ? "All read" : "Mark all read"}</button>
      </div>
      ${expanded ? `
        <div class="cleanup-bundle-body">
          ${messages.map((msg, i) => renderLowValueEmailCard(msg, i, card.id)).join("")}
        </div>
      ` : ""}
    </article>
  `;
}

function renderLowValueEmailCard(msg, index, cardId) {
  const readState = state.cleanupReadState[cardId];
  const isRead = readState && readState.readMsgIndices.includes(index);
  const dateStr = msg.date || "";
  let displayDate = "";
  if (dateStr) {
    try {
      displayDate = formatBeijingTimestamp(Number(dateStr));
    } catch (e) {
      displayDate = dateStr;
    }
  }
  return `
    <div class="low-value-email-card ${isRead ? "is-read" : ""}">
      ${isRead ? `<span class="low-value-email-check">✓</span>` : ""}
      <div class="low-value-email-header">
        <span class="low-value-email-from">${escapeHtml(msg.from_addr || "Unknown")}</span>
        ${displayDate ? `<span class="low-value-email-date">${escapeHtml(displayDate)}</span>` : ""}
      </div>
      <div class="low-value-email-subject">${escapeHtml(msg.subject || "(no subject)")}</div>
      ${msg.snippet ? `<div class="low-value-email-snippet">${escapeHtml(msg.snippet)}</div>` : ""}
      <div class="low-value-email-meta">
        <span class="low-value-email-type">low priority</span>
        ${msg.reason ? `<span class="low-value-email-reason">${escapeHtml(msg.reason)}</span>` : ""}
      </div>
    </div>
  `;
}

function renderAsk() {
  const plans = state.customPlans;
  content.innerHTML = `
    <div class="ask-layout">
      <section class="assistant-card">
        <div class="assistant-kicker">Custom scan</div>
        <h1 class="assistant-says">Ask Anna to do a custom scan</h1>
        <p class="assistant-copy">Describe what you need in natural language. Anna will scan ${escapeHtml(state.mailbox)} based on your request without changing your default daily briefing.</p>
      </section>
      <section class="composer-card">
        <textarea
          id="customScanInput"
          placeholder="e.g. Find all unread emails and check which need a reply..."
          rows="3"
        >${escapeHtml(state.customScanInput)}</textarea>
        <div class="proposal-actions" style="justify-content:flex-end;">
          <button class="primary-btn" data-action="run-custom-scan"
            ${state.isCustomScanning ? "disabled" : ""}>
            ${state.isCustomScanning ? "Planning & scanning..." : "Run custom scan"}
          </button>
        </div>
      </section>
      ${state.customRunProgress && !state.customRunResult ? renderCustomRunProgress(state.customRunProgress) : ""}
      ${state.scanError ? `<section class="custom-error-card">${escapeHtml(state.scanError)}</section>` : ""}
      ${state.customRunResult ? renderCustomRunResult(state.customRunResult) : ""}

      ${plans.length ? `
        <section class="noticed-section">
          <h2 class="noticed-title">Past custom scans</h2>
          <p class="noticed-subtitle">Click Re-run to execute a saved plan again without re-planning.</p>
          <div class="history-list">
            ${plans.map(renderCustomPlanEntry).join("")}
          </div>
        </section>
      ` : ""}
    </div>
  `;
}

const CUSTOM_PROGRESS_STEPS = [
  { key: "planning", label: "Plan" },
  { key: "searching", label: "Search" },
  { key: "reading", label: "Read" },
  { key: "answering", label: "Answer" },
  { key: "done", label: "Done" },
];

function customStageKey(stage, status) {
  if (status === "done") return "done";
  if (status === "failed") return "failed";
  if (stage === "scan" || stage === "scan_done") return "searching";
  if (stage === "read_context" || stage === "read_context_done") return "reading";
  if (stage === "evaluate" || stage === "evaluate_done") return "answering";
  if (stage === "done") return "done";
  return "planning";
}

function customStageCopy(stageKey, progress) {
  if (stageKey === "planning") return "Shaping the mailbox scan into a focused plan.";
  if (stageKey === "searching") {
    const found = Number(progress.scanned || 0);
    return found ? `Found ${found} relevant message${found === 1 ? "" : "s"}.` : "Running the planned Gmail searches.";
  }
  if (stageKey === "reading") {
    const current = Number(progress.current || 0);
    const total = Number(progress.total || 0);
    return total ? `Reading context ${current}/${total}.` : "Opening the relevant email context.";
  }
  if (stageKey === "answering") return "The mail evidence is ready. Anna is composing the answer.";
  if (stageKey === "done") return "Answer ready.";
  return "The scan stopped before an answer was produced.";
}

function updateCustomRunProgress(status, fallback = {}) {
  const progress = status.progress || {};
  const partial = {
    ...(state.customRunProgress?.partial || {}),
    ...(status.partial || {}),
  };
  const stageKey = customStageKey(status.stage, status.status);
  state.customRunProgress = {
    runId: status.run_id || fallback.runId || "",
    question: fallback.question || state.customRunProgress?.question || state.customScanInput || "",
    status: status.status || "running",
    stage: status.stage || "planning",
    stageKey,
    progress,
    partial,
    startedAt: status.started_at || fallback.startedAt || state.customRunProgress?.startedAt || "",
    updatedAt: status.updated_at || "",
  };
}

function renderCustomRunProgress(run) {
  const progress = run.progress || {};
  const partial = run.partial || {};
  const plan = partial.plan || {};
  const queries = Array.isArray(plan.gmail_queries) ? plan.gmail_queries : [];
  const sources = Array.isArray(partial.sources) ? partial.sources : [];
  const activeIndex = Math.max(0, CUSTOM_PROGRESS_STEPS.findIndex((step) => step.key === run.stageKey));
  const scanned = Number(progress.scanned || 0);
  const total = Number(progress.total || 0);
  const current = Number(progress.current || 0);
  const queryTotal = Number(progress.query_total || queries.length || 0);
  return `
    <section class="ask-progress-card">
      <div class="ask-progress-head">
        <div class="ask-orb" aria-hidden="true"><span></span></div>
        <div>
          <p class="ask-progress-kicker">Custom scan running</p>
          <h2 class="ask-progress-title">${escapeHtml(plan.title || "Planning your scan")}</h2>
          <p class="ask-progress-copy">${escapeHtml(customStageCopy(run.stageKey, progress))}</p>
        </div>
      </div>

      <div class="ask-progress-steps" aria-label="Custom scan progress">
        ${CUSTOM_PROGRESS_STEPS.map((step, index) => `
          <div class="ask-progress-step ${index < activeIndex ? "is-done" : ""} ${index === activeIndex ? "is-active" : ""}">
            <span class="ask-step-dot"></span>
            <span>${escapeHtml(step.label)}</span>
          </div>
        `).join("")}
      </div>

      <div class="ask-progress-metrics">
        <span><strong>${queryTotal || "-"}</strong> queries</span>
        <span><strong>${scanned || sources.length || "-"}</strong> found</span>
        <span><strong>${total ? `${current}/${total}` : "-"}</strong> read</span>
      </div>

      ${queries.length ? `
        <div class="ask-plan-preview">
          <div class="ask-plan-head">
            <span>Plan preview</span>
            <strong>${escapeHtml(plan.read_depth || "message_detail")}</strong>
          </div>
          <div class="ask-query-list">
            ${queries.slice(0, 3).map((query) => `
              <div class="ask-query-item">
                <code>${escapeHtml(query.query || "")}</code>
                ${query.purpose ? `<span>${escapeHtml(query.purpose)}</span>` : ""}
              </div>
            `).join("")}
          </div>
        </div>
      ` : `
        <div class="ask-skeleton">
          <span></span><span></span><span></span>
        </div>
      `}

      ${sources.length ? `
        <div class="ask-source-strip">
          ${sources.slice(0, 4).map((source) => `
            <div class="ask-source-chip">
              <strong>${escapeHtml(source.subject || "Untitled email")}</strong>
              <span>${escapeHtml(source.from || "")}</span>
            </div>
          `).join("")}
        </div>
      ` : ""}

      ${run.stageKey === "answering" ? `
        <div class="ask-answer-wait">
          <span></span><span></span><span></span>
        </div>
      ` : ""}
    </section>
  `;
}

function renderAskItemActions(item) {
  const mid = item.message_id || "";
  const acts = state.askItemActions[mid] || {};
  const hasDraft = Boolean(item.draft && item.draft.trim());
  const canReply = hasDraft && item.thread_id && item.from;
  const canMark = Boolean(mid);
  const canTrash = Boolean(mid);
  const actionKey = mid || item.thread_id || "";

  if (!canReply && !canMark && !canTrash) return "";

  if (acts.trashed && !canReply) {
    return `<div class="ask-item-actions"><span class="ask-action-done">Trashed</span></div>`;
  }

  // Editing mode: show textarea + Send / Cancel
  const editing = state.askEditDraft[actionKey] !== undefined;
  if (editing) {
    return `
      <div class="ask-item-actions">
        <textarea class="ask-edit-textarea" id="ask-edit-${escapeHtml(actionKey)}" rows="4">${escapeHtml(state.askEditDraft[actionKey])}</textarea>
        <div class="ask-item-actions">
          <button class="ask-action-btn ask-action-reply" data-ask-action="send-draft" data-key="${escapeHtml(actionKey)}" data-tid="${escapeHtml(item.thread_id || "")}" data-to="${escapeHtml(item.from || "")}">Send</button>
          <button class="ask-action-btn ask-action-cancel" data-ask-action="cancel-draft" data-key="${escapeHtml(actionKey)}">Cancel</button>
          ${canMark ? `<button class="ask-action-btn ask-action-read" data-ask-action="read" data-mid="${escapeHtml(mid)}" ${acts.read ? "disabled" : ""}>${acts.read ? "Read" : "Mark read"}</button>` : ""}
          ${canTrash ? `<button class="ask-action-btn ask-action-trash" data-ask-action="trash" data-mid="${escapeHtml(mid)}">Trash</button>` : ""}
        </div>
      </div>
    `;
  }

  const replyDisabled = acts.replied || acts.sending || !canReply ? "disabled" : "";
  const readDisabled = acts.read || acts.sending || !canMark ? "disabled" : "";
  const trashDisabled = acts.sending || !canTrash ? "disabled" : "";
  let replyLabel = "Reply";
  if (acts.sending) replyLabel = "Sending…";
  else if (acts.replied) replyLabel = "Replied";

  return `
    <div class="ask-item-actions">
      ${canReply ? `<button class="ask-action-btn ask-action-reply" data-ask-action="reply" data-mid="${escapeHtml(mid)}" data-tid="${escapeHtml(item.thread_id || "")}" data-to="${escapeHtml(item.from || "")}" data-draft="${escapeHtml(item.draft || "")}" data-key="${escapeHtml(actionKey)}" ${replyDisabled}>${replyLabel}</button>` : ""}
      ${canMark ? `<button class="ask-action-btn ask-action-read" data-ask-action="read" data-mid="${escapeHtml(mid)}" ${readDisabled}>${acts.read ? "Read" : "Mark read"}</button>` : ""}
      ${canTrash ? `<button class="ask-action-btn ask-action-trash" data-ask-action="trash" data-mid="${escapeHtml(mid)}" ${trashDisabled}>Trash</button>` : ""}
      ${acts.trashed ? `<span class="ask-action-done">Trashed</span>` : ""}
    </div>
  `;
}

function renderCustomRunResult(result) {
  const sections = Array.isArray(result.sections) ? result.sections : [];
  const plannerFallbackNote = result.planner_fallback
    ? `<div class="ask-plan-fallback">Anna could not generate a smart scan plan this time, so a rule-based fallback was used. The answer may be less precise.</div>`
    : "";
  return `
    <section class="custom-result-card">
      <div class="custom-result-head">
        <div>
          <p class="custom-result-title">${escapeHtml(result.title || result.plan_title || "Scan result")}</p>
          <p class="assistant-copy">${escapeHtml(result.summary || "")}</p>
        </div>
        <span class="attention-pill is-depth">Custom scan</span>
      </div>
      ${plannerFallbackNote}

      ${sections.map(sec => `
        <div class="point-box point-box-sm">
          <span class="box-label">${escapeHtml(sec.heading || "")}</span>
          ${sec.body ? `<p>${escapeHtml(sec.body).replace(/\n/g, "<br>")}</p>` : ""}
          ${Array.isArray(sec.items) && sec.items.length ? `
            <ul class="simple-list simple-list-sm">
              ${sec.items.map(it => `
                <li>
                  <strong>${escapeHtml(it.subject || "")}</strong>
                  ${it.context ? `<br><span class="text-muted-inline">${escapeHtml(it.context)}</span>` : ""}
                  ${it.suggestion ? `<br><span class="text-accent-inline">→ ${escapeHtml(it.suggestion)}</span>` : ""}
                  ${it.draft ? `
                    <div class="draft-preview-box">${escapeHtml(it.draft)}</div>
                    <button class="soft-btn draft-preview-btn" data-action="copy-draft" data-draft="${escapeHtml(it.draft)}">Copy draft</button>
                  ` : ""}
                  ${renderAskItemActions(it)}
                </li>
              `).join("")}
            </ul>
          ` : ""}
        </div>
      `).join("")}
      ${renderCustomTrace(result)}
    </section>
  `;
}

function normalizeSubject(subject) {
  return String(subject || "Untitled email").replace(/^(re|fw|fwd):\s*/i, "").trim() || "Untitled email";
}

function groupSources(sources) {
  const groups = new Map();
  for (const source of Array.isArray(sources) ? sources : []) {
    const subject = normalizeSubject(source.subject);
    const key = source.thread_id || subject;
    const existing = groups.get(key) || {
      subject,
      count: 0,
      people: new Set(),
    };
    existing.count += 1;
    if (source.from) existing.people.add(String(source.from));
    groups.set(key, existing);
  }
  return Array.from(groups.values()).map((group) => ({
    subject: group.subject,
    count: group.count,
    people: Array.from(group.people).slice(0, 2).join(", "),
  }));
}

function renderCustomTrace(result) {
  const trace = result.trace || {};
  const plan = trace.plan || {
    title: result.plan_title || "",
    description: result.plan_description || "",
    gmail_queries: result.plan_gmail_queries || [],
    read_depth: result.plan_read_depth || "",
  };
  const queries = Array.isArray(plan.gmail_queries) ? plan.gmail_queries : [];
  const sources = Array.isArray(trace.sources) ? trace.sources : [];
  const groups = groupSources(sources);
  const progress = trace.progress || {};
  const readTotal = Number(progress.total || progress.emails || sources.length || 0);
  const readCurrent = Number(progress.current || readTotal || 0);
  const found = Number(progress.scanned || sources.length || 0);
  const readDepth = plan.read_depth || result.plan_read_depth || "message_detail";
  const open = state.customTraceOpen;
  return `
    <section class="custom-trace-card ${open ? "is-open" : ""}">
      <button class="custom-trace-summary" data-action="toggle-custom-trace" aria-expanded="${open}">
        <span>
          <strong>How Anna got here</strong>
          <em>${queries.length || "-"} quer${queries.length === 1 ? "y" : "ies"} · ${found || "-"} found · ${readTotal ? `${readCurrent}/${readTotal}` : "-"} read · ${escapeHtml(readDepth)}</em>
        </span>
        <span class="trace-toggle">${open ? "Hide" : "Expand"}</span>
      </button>
      ${open ? `
        <div class="custom-trace-detail">
          <div class="trace-block">
            <div class="trace-block-head">
              <span>Plan</span>
              <strong>${escapeHtml(readDepth)}</strong>
            </div>
            <p>${escapeHtml(plan.title || "Custom scan")}</p>
            ${plan.description ? `<small>${escapeHtml(plan.description)}</small>` : ""}
          </div>
          ${queries.length ? `
            <div class="trace-block">
              <div class="trace-block-head">
                <span>Searched</span>
                <strong>${queries.length}</strong>
              </div>
              <div class="trace-query-list">
                ${queries.slice(0, 4).map((query) => `
                  <div class="trace-query">
                    <span>${escapeHtml(query.purpose || "Mailbox search")}</span>
                    <code>${escapeHtml(query.query || "")}</code>
                  </div>
                `).join("")}
              </div>
            </div>
          ` : ""}
          ${groups.length ? `
            <div class="trace-block">
              <div class="trace-block-head">
                <span>Evidence</span>
                <strong>${groups.length} group${groups.length === 1 ? "" : "s"}</strong>
              </div>
              <div class="trace-source-list">
                ${groups.slice(0, 6).map((group) => `
                  <div class="trace-source">
                    <strong>${escapeHtml(group.subject)}</strong>
                    <span>${group.count} message${group.count === 1 ? "" : "s"}${group.people ? ` · ${escapeHtml(group.people)}` : ""}</span>
                  </div>
                `).join("")}
              </div>
            </div>
          ` : ""}
        </div>
      ` : ""}
    </section>
  `;
}

function renderCustomPlanEntry(plan) {
  const timestampLabel = plan.last_used_at
    ? `Last run ${formatBeijingTimestamp(plan.last_used_at)}`
    : `Created ${formatBeijingTimestamp(plan.created_at || "")}`;
  return `
    <div class="custom-plan-row">
      <button class="history-row" data-action="re-run-custom-plan" data-plan-id="${escapeHtml(plan.plan_id)}"
        ${state.isCustomScanning ? "disabled" : ""}>
        <strong>${escapeHtml(plan.title || plan.user_request?.slice(0, 60) || "Custom scan")}</strong>
        <span class="custom-plan-meta">
          ${escapeHtml(plan.user_request?.slice(0, 100) || "")}
          ${plan.use_count ? `· Used ${plan.use_count} time${plan.use_count === 1 ? "" : "s"}` : ""}
          ${timestampLabel ? `· ${escapeHtml(timestampLabel)}` : ""}
        </span>
        ${plan.last_result_summary ? `<span class="custom-plan-result">${escapeHtml(plan.last_result_summary)}</span>` : ""}
        <span class="custom-plan-delete" data-action="delete-custom-plan" data-plan-id="${escapeHtml(plan.plan_id)}" role="button" aria-label="Delete plan">Delete</span>
      </button>
    </div>
  `;
}

function renderBottomBar() {
  if (!bottomBar) return;
  const cards = visibleCards();

  if (state.isScanning) {
    const step = SCAN_STEPS[Math.min(state.scanStepIndex, SCAN_STEPS.length - 1)];
    bottomBar.innerHTML = `
      <div>
        <p class="bar-title">Anna is scanning your inbox.</p>
        <p class="bar-copy">${escapeHtml(step.title)} · ${escapeHtml(scanProgressLabel(state.scanStage, state.scanProgress) || "Starting…")}</p>
      </div>
      <div class="bar-actions"><button class="soft-btn" disabled>Scanning…</button></div>
    `;
    return;
  }

  bottomBar.innerHTML = `
    <div>
      <p class="bar-title">${cards.length} active attention card${cards.length === 1 ? "" : "s"}.</p>
      <p class="bar-copy">${state.loading ? "Connecting to Anna runtime..." : "Ready."}</p>
    </div>
    <div class="bar-actions">
      ${renderDebugProviderControls()}
      <button class="soft-btn" data-action="open-scan-plan">Next Scan</button>
      <button class="soft-btn" data-action="go-ask">Ask</button>
      <button class="primary-btn" data-action="manual-scan" ${state.isScanning || !state.runtime.connected ? "disabled" : ""}>Scan now</button>
      ${cards.length > 0 ? `<button class="soft-btn" data-action="clear-all-cards" style="color:var(--muted);">Clear</button>` : ""}
    </div>
  `;
}

function renderSourcesDrawer() {
  if (!sourcesDrawer) return;
  sourcesDrawer.className = `drawer ${state.sourcesOpen ? "is-open" : ""}`;

  // Build dynamic scan description
  const totalScans = Number(state.scanState?.total_scans || 0);
  const plan = state.scanPlan || {};
  const timeRange = plan.time_range || "auto";
  const schedule = plan.schedule || "manual";
  const maxMessages = plan.max_messages || 50;

  // Adaptive window label
  let windowLabel = "";
  if (timeRange !== "auto") {
    const rangeLabels = { last_24h: "1 day", last_7d: "7 days", unread_backlog: "30 days", since_last: "since last scan" };
    windowLabel = rangeLabels[timeRange] || timeRange;
  } else if (totalScans === 0) {
    windowLabel = "7 days (first scan)";
  } else {
    // Compute days since last scan
    const lastScan = state.scanState?.last_scan_ts || "";
    if (lastScan) {
      try {
        const daysSince = Math.max(1, Math.floor((Date.now() - new Date(lastScan).getTime()) / 86400000));
        if (daysSince <= 1) windowLabel = "2 days (daily use)";
        else if (daysSince <= 7) windowLabel = `${daysSince} days (adaptive)`;
        else windowLabel = "7 days (adaptive)";
      } catch { windowLabel = "7 days (adaptive)"; }
    } else {
      windowLabel = "7 days (first scan)";
    }
  }

  const scheduleLabels = { manual: "manual", every_morning: "morning", every_afternoon: "afternoon", twice_daily: "2x/day", workdays: "workdays" };
  const scheduleLabel = scheduleLabels[schedule] || schedule;

  const storageLabel = state.storageProvider === "aps" ? "APS (Anna Persistent Storage)" : "local JSON files";

  const initials = (state.mailbox || "A").charAt(0).toUpperCase();

  sourcesDrawer.innerHTML = `
    <div class="drawer-head">
      <div>
        <h2 class="drawer-title">Sources</h2>
        <p class="drawer-copy">Connected mailbox and scan strategy.</p>
      </div>
      <button class="icon-btn" data-action="close-drawers">x</button>
    </div>
    <div class="drawer-body">
      <article class="source-card mailbox-card is-selected">
        <div class="source-icon">${escapeHtml(initials)}</div>
        <div>
          <div class="source-name">${escapeHtml(state.mailbox)}</div>
          <div class="source-meta">Gmail source · ${escapeHtml(modeLabel(state.strategyMode))}</div>
        </div>
        <div class="mailbox-edit">
          <label class="field-label" for="mailboxInput">Mailbox</label>
          <div class="mailbox-input-row">
            <input type="email" id="mailboxInput" value="${escapeHtml(state.mailboxInput || state.mailbox)}" autocomplete="email" spellcheck="false" />
            <button class="soft-btn" data-action="apply-mailbox">Apply</button>
          </div>
        </div>
        <div class="mailbox-actions">
          <button class="soft-btn" data-action="manual-scan" ${state.isScanning ? "disabled" : ""}>Rescan</button>
        </div>
      </article>
      <section class="config-block">
        <h3>Scan window</h3>
        <ul class="config-list">
          <li>Range: <strong>${escapeHtml(windowLabel)}</strong></li>
          ${totalScans > 0 ? `<li>Incremental: stops at last processed message time.</li>` : `<li>First scan covers the full window above.</li>`}
          <li>Max messages per scan: <strong>${maxMessages}</strong></li>
        </ul>
      </section>
      <section class="config-block">
        <h3>Schedule &amp; storage</h3>
        <ul class="config-list">
          <li>Schedule: <strong>${escapeHtml(scheduleLabel)}</strong></li>
          <li>Storage: ${escapeHtml(storageLabel)}</li>
          ${plan.include_newsletters ? `<li>Including newsletters</li>` : ""}
          ${plan.include_promotions ? `<li>Including promotions</li>` : ""}
        </ul>
      </section>
    </div>
  `;
}

function renderHistoryDrawer() {
  if (!historyDrawer) return;
  historyDrawer.className = `drawer ${state.historyOpen ? "is-open" : ""}`;

  const resolved = resolvedCards();
  const grouped = {
    snoozed: resolved.filter(c => c.status === "snoozed"),
    dismissed: resolved.filter(c => c.status === "dismissed"),
    resolved: resolved.filter(c => c.status === "resolved"),
  };
  const hasResolved = resolved.length > 0;

  function statusLabel(card) {
    if (card.status === "snoozed") return card.snooze_until ? `Until ${escapeHtml(formatBeijingTimestamp(card.snooze_until))}` : "Snoozed";
    if (card.status === "dismissed") return "Dismissed";
    const resolutionMap = { no_action_needed: "No action", handled_manually: "Handled", replied: "Replied", dont_prioritize: "Muted" };
    return resolutionMap[card.resolution] || card.resolution || "Resolved";
  }

  function renderResolvedGroup(label, cards) {
    if (!cards.length) return "";
    return `
      <div class="resolved-group">
        <div class="resolved-group-title">${escapeHtml(label)}</div>
        ${cards.map(card => `
          <div class="resolved-card">
            <div>
              <div class="resolved-card-title">${escapeHtml(card.title || "Untitled")}</div>
              <div class="resolved-card-meta">${statusLabel(card)}</div>
            </div>
            <button class="detail-back" data-restore-card="${escapeHtml(card.id)}">Restore</button>
          </div>
        `).join("")}
      </div>
    `;
  }

  historyDrawer.innerHTML = `
    <div class="drawer-head">
      <div>
        <h2 class="drawer-title">History</h2>
        <p class="drawer-copy">Cards you've processed and past runs.</p>
      </div>
      <button class="icon-btn" data-action="close-drawers">&times;</button>
    </div>
    <div class="drawer-body">
      ${hasResolved ? `
        <section class="resolved-section">
          ${renderResolvedGroup("Snoozed", grouped.snoozed)}
          ${renderResolvedGroup("Dismissed", grouped.dismissed)}
          ${renderResolvedGroup("Resolved", grouped.resolved)}
        </section>
      ` : `<p class="assistant-copy">No processed cards yet.</p>`}

      ${state.history.length ? `
        <section style="margin-top:18px;">
          <div class="resolved-group-title" style="margin-bottom:8px;">Past runs</div>
          ${state.history.map((run) => `
            <article class="source-card">
              <div class="source-icon">A</div>
              <div>
                <div class="source-name">${escapeHtml(run.request || run.strategy || "Mailbox scan")}</div>
                <div class="source-meta">${escapeHtml(formatBeijingTimestamp(run.ts) || "-")} · ${escapeHtml(run.result || "")}</div>
              </div>
            </article>
          `).join("")}
        </section>
      ` : ""}
    </div>
  `;
}

function renderScanPlanDrawer() {
  if (!scanPlanDrawer) return;
  const plan = state.scanPlan || {};
  scanPlanDrawer.className = `drawer ${state.scanPlanOpen ? "is-open" : ""}`;

  const rangeLabels = { auto: "Adaptive (auto)", since_last: "Since last brief", last_24h: "Last 24 hours", last_7d: "Last 7 days", unread_backlog: "Unread backlog" };
  const scheduleLabels = { manual: "Manual only", every_morning: "Every morning", every_afternoon: "Every afternoon", twice_daily: "Twice a day", workdays: "Workdays only" };

  scanPlanDrawer.innerHTML = `
    <div class="drawer-head">
      <div>
        <h2 class="drawer-title">Next Scan</h2>
        <p class="drawer-copy">Configure when and how Anna scans your inbox.</p>
      </div>
      <button class="icon-btn" data-action="close-drawers">&times;</button>
    </div>
    <div class="drawer-body">
      <section class="config-block">
        <h3>Schedule</h3>
        <div class="preset-row preset-row-sm">
          ${Object.entries(scheduleLabels).map(([v, label]) => `
            <button class="preset-chip ${plan.schedule === v ? 'is-active' : ''}" data-set-scan-plan="schedule:${v}">${label}</button>
          `).join("")}
        </div>
      </section>
      <section class="config-block">
        <h3>Time range</h3>
        <div class="preset-row preset-row-sm">
          ${Object.entries(rangeLabels).map(([v, label]) => `
            <button class="preset-chip ${plan.time_range === v ? 'is-active' : ''}" data-set-scan-plan="time_range:${v}">${label}</button>
          `).join("")}
        </div>
      </section>
      <section class="config-block">
        <h3>Messages per scan</h3>
        <div class="preset-row preset-row-sm">
          ${[50, 100, 200].map(n => `
            <button class="preset-chip ${plan.max_messages === n ? 'is-active' : ''}" data-set-scan-plan="max_messages:${n}">${n}</button>
          `).join("")}
        </div>
      </section>
      <section class="config-block">
        <h3>Include</h3>
        <div class="preset-row preset-row-sm">
          <button class="preset-chip ${plan.include_newsletters ? 'is-active' : ''}" data-set-scan-plan="include_newsletters:${!plan.include_newsletters}">Newsletters</button>
          <button class="preset-chip ${plan.include_promotions ? 'is-active' : ''}" data-set-scan-plan="include_promotions:${!plan.include_promotions}">Promotions</button>
        </div>
      </section>
      ${plan.updated_at ? `<p class="scan-plan-saved-text">Saved ${escapeHtml(formatBeijingTimestamp(plan.updated_at))}</p>` : ""}
    </div>
  `;
}

function renderHandleView() {
  const card = state.selectedCard;
  if (!card) {
    state.originalOpen = false;
    render();
    return;
  }
  const prevCardId = renderHandleView._lastCardId;
  renderHandleView._lastCardId = card.id;
  const isNewCard = prevCardId !== card.id;

  const original = card.original || {};
  const detail = state.selectedCardDetail || {};
  const context = detail.thread_context || {};
  const summary = state.threadSummaryById[card.id];
  const draft = state.draftById[card.id] || "";
  const replyMode = state.replyModeById[card.id] || "reply_to_sender";
  const cc = context.cc || original.cc || "None";
  const contextExpanded = Boolean(state.threadContextExpanded[card.id]);

  const senderName = (context.from || original.from || card.original?.from || "").split("<")[0].trim().replace(/"/g, "").replace(/^"|"$/g, "") || "Unknown";
  const threadSubject = (context.subject || original.thread || card.title || "").slice(0, 80);
  const hasDraft = Boolean(draft);
  const statusTag = hasDraft ? "Reply ready" : "Needs review";
  const statusTagClass = hasDraft ? "is-ready-state" : "";
  const draftDisplay = state.generatingDraft && !draft ? "Anna is drafting a reply" + (state.draftDots || "...") : draft;

  function renderSummaryBlock() {
    if (state.summarizingThread) {
      return `
        <section class="review-block is-summary is-loading">
          <p>Anna is reading the thread and preparing a summary…</p>
        </section>
      `;
    }
    if (!summary || !summary.core_ask) {
      return `
        <section class="review-block is-summary">
          <p>Anna can read the full thread and summarize what matters before you draft.</p>
          <div class="proposal-actions"><button class="soft-btn" data-action="summarize-thread">Summarize thread</button></div>
        </section>
      `;
    }
    const bullets = [
      summary.core_ask,
      summary.current_progress,
    ].filter(Boolean);
    const openQuestions = Array.isArray(summary.open_questions)
      ? summary.open_questions.filter(Boolean)
      : (summary.open_questions ? [summary.open_questions] : []);
    const openLoop = summary.user_action_needed || summary.core_ask || "";
    const why = (card.recommendation && card.recommendation.length > 10)
      ? card.recommendation.replace(/^Suggested:\s*/i, "")
      : summary.tone === "urgent" ? "This is time-sensitive and should be addressed quickly."
      : summary.tone === "warm" ? "A timely response keeps this relationship moving forward."
      : summary.tone === "waiting" ? "The other side is waiting, and silence may read as disinterest."
      : "This connects to an active thread and is worth resolving now.";

    return `
      <section class="review-block is-summary">
        <div class="review-block-kicker">Anna noticed</div>
        <ul>
          ${[...bullets, ...openQuestions].map(line => `<li>${escapeHtml(line)}</li>`).join("")}
        </ul>
        ${openLoop ? `<p><strong>Open loop:</strong> ${escapeHtml(openLoop)}</p>` : ""}
        <p><strong>Why this matters:</strong> ${escapeHtml(why)}</p>
      </section>
    `;
  }

  content.innerHTML = `
    <section class="detail-shell">
      <button class="detail-back" data-action="close-drawers">← Back to brief</button>
      <article class="detail-card">
        <div class="reply-review-head">
          <div>
            <h2 class="reply-review-title">${escapeHtml(card.title || "Email needs review")}</h2>
            <p class="detail-subtitle">${escapeHtml(senderName)} · ${escapeHtml(threadSubject)}</p>
          </div>
          <span class="category-tag ${statusTagClass}">${escapeHtml(statusTag)}</span>
        </div>

        ${renderSummaryBlock()}

        <section class="review-block is-composer ${state.generatingDraft ? "is-loading" : ""}">
          <h3 class="review-block-title">Draft reply</h3>
          <div class="reply-mode-row">
            <div class="reply-mode-control" role="group" aria-label="Reply mode">
              <button class="reply-mode-btn ${replyMode === "reply_to_sender" ? "is-active" : ""}" data-reply-mode="reply_to_sender">Reply to sender</button>
              <button class="reply-mode-btn ${replyMode === "reply_all" ? "is-active" : ""}" data-reply-mode="reply_all" ${cc && cc !== "None" ? "" : "disabled"}>Reply all</button>
            </div>
            ${cc && cc !== "None" ? "" : `<span class="reply-mode-note">No CC recipients</span>`}
          </div>
          <textarea class="draft-textarea${state.generatingDraft && !draft ? " is-draft-loading" : ""}" id="drawerDraftInput" placeholder="Click 'Generate draft' to have Anna write a reply based on this thread.">${escapeHtml(draftDisplay)}</textarea>
          ${draft ? `
            <div class="preset-row">
              <button class="preset-chip" data-revise-preset="Shorter" ${state.generatingDraft ? "disabled" : ""}>Shorter</button>
              <button class="preset-chip" data-revise-preset="Warmer" ${state.generatingDraft ? "disabled" : ""}>Warmer</button>
              <button class="preset-chip" data-revise-preset="More direct" ${state.generatingDraft ? "disabled" : ""}>More direct</button>
            </div>
          ` : ""}
          <div class="revise-row">
            <input type="text" id="drawerRevisionInput" placeholder="${draft ? 'Tell Anna how to revise this draft...' : 'Tell Anna how to write the reply (optional)'}" value="${escapeHtml(state.revisionById[card.id] || "")}" ${state.generatingDraft ? "disabled" : ""} />
            <button class="soft-btn generate-draft-btn is-glow" data-action="unified-draft" ${state.generatingDraft ? "disabled" : ""}>${state.generatingDraft ? "Working…" : (draft ? "Ask Anna to revise" : "Generate draft")}</button>
          </div>
        </section>

        <section class="review-block is-quiet">
          <button class="thread-context-toggle" data-action="toggle-thread-context" aria-expanded="${contextExpanded}">
            <strong>Thread context · ${escapeHtml(senderName)} · ${escapeHtml(formatBeijingTimestamp(context.latest_time || original.time))} · ${escapeHtml(String(context.message_count || 1))} ${Number(context.message_count || 1) === 1 ? "message" : "messages"}</strong>
            <span>${contextExpanded ? "Collapse" : "Expand"}</span>
          </button>
          ${contextExpanded ? `
            <div class="thread-context-grid">
              <div class="thread-context-row"><span>From</span><strong>${escapeHtml(context.from || original.from || "")}</strong></div>
              <div class="thread-context-row"><span>To</span><strong>${escapeHtml(context.to || original.to || "")}</strong></div>
              <div class="thread-context-row"><span>CC</span><strong>${escapeHtml(cc || "None")}</strong></div>
              <div class="thread-context-row"><span>Thread</span><strong>${escapeHtml(threadSubject)}</strong></div>
              <div class="thread-context-row"><span>Latest</span><strong>${escapeHtml(formatBeijingTimestamp(context.latest_time || original.time))}</strong></div>
            </div>
            <div class="original-body" style="margin-top:8px;">${escapeHtml(original.body || "Latest email body is not available.")}</div>
          ` : ""}
        </section>

        <div class="decision-row drawer-action-row">
          <button class="primary-btn" data-action="drawer-reply-now" ${!draft.trim() ? "disabled" : ""}>Reply now</button>
          <button class="soft-btn" data-action="drawer-no-action">No action needed</button>
          <button class="soft-btn" data-action="drawer-handled-manually">Handled manually</button>
          ${nextCardId() ? `<button class="detail-back" data-action="next-card" style="margin-left:auto;">Next card →</button>` : ""}
        </div>
      </article>
    </section>
  `;

  if (isNewCard) content.scrollTop = 0;
}

function renderScanning() {
  const stepIndex = Math.min(state.scanStepIndex, SCAN_STEPS.length - 1);
  const currentStep = SCAN_STEPS[stepIndex];
  content.innerHTML = `
    <div class="scanning-layout">
      <section class="scanning-panel" aria-label="Anna is scanning">
        <div>
          <div class="scanning-orb">A</div>
          <h1 class="scanning-title">${escapeHtml(currentStep.title)}</h1>
          <p class="scanning-copy">Anna is taking a first pass through your mailbox and turning the inbox into a short action brief.</p>
        </div>
        <div class="scan-flow">
          ${SCAN_STEPS.map((step, index) => `
            <div class="scan-flow-step ${index < stepIndex ? "is-done" : ""} ${index === stepIndex ? "is-active" : ""}">
              <span class="scan-flow-dot">${index < stepIndex ? "✓" : ""}</span>
              <span>${escapeHtml(step.title)}</span>
            </div>
          `).join("")}
        </div>
        <div class="scanning-microcopy">${escapeHtml(currentStep.microcopy)}</div>
        <div class="scanning-count">${escapeHtml(scanProgressLabel(state.scanStage, state.scanProgress) || "Starting…")}</div>
      </section>
    </div>
  `;
}

function renderOriginalDrawer() {
  if (!originalDrawer) return;
  const card = state.selectedCard;
  originalDrawer.className = `drawer ${state.originalOpen && card ? "is-open" : ""}`;
  if (!card) {
    originalDrawer.innerHTML = "";
    return;
  }
  const original = card.original || {};
  const detail = state.selectedCardDetail || {};
  const context = detail.thread_context || {};
  const summary = state.threadSummaryById[card.id];
  const draft = state.draftById[card.id] || "";
  const draftDisplay = state.generatingDraft && !draft ? "Anna is drafting a reply" + (state.draftDots || "...") : draft;
  const replyMode = state.replyModeById[card.id] || "reply_to_sender";
  const cc = context.cc || original.cc || "None";
  const threadLabel = context.subject
    ? `${context.subject}${context.message_count ? ` · ${context.message_count} message${context.message_count === 1 ? "" : "s"}` : ""}`
    : (original.thread || card.title || "");

  originalDrawer.innerHTML = `
    <div class="drawer-head">
      <div>
        <h2 class="drawer-title">Handle thread</h2>
        <p class="drawer-copy">Latest email, thread context, and reply options.</p>
      </div>
      <button class="icon-btn" data-action="close-drawers">x</button>
    </div>
    <div class="drawer-body">
      <div class="thread-context-grid">
        <div class="thread-context-row"><span>From</span><strong>${escapeHtml(context.from || original.from || "")}</strong></div>
        <div class="thread-context-row"><span>To</span><strong>${escapeHtml(context.to || original.to || "")}</strong></div>
        <div class="thread-context-row"><span>CC</span><strong>${escapeHtml(cc || "None")}</strong></div>
        <div class="thread-context-row"><span>Thread</span><strong>${escapeHtml(threadLabel)}</strong></div>
        <div class="thread-context-row"><span>Latest message</span><strong>${escapeHtml(formatBeijingTimestamp(context.latest_time || original.time))}</strong></div>
      </div>

      <section class="drawer-section">
        <h3 class="drawer-section-title">Latest email</h3>
        <div class="original-body">${escapeHtml(original.body || "Latest email body is not available in the compact card.")}</div>
      </section>

      <section class="drawer-section">
        <h3 class="drawer-section-title">Thread summary</h3>
        <div class="thread-summary-box">
          ${summary && summary.core_ask ? `<ul class="thread-summary-list">${[summary.core_ask, summary.current_progress].filter(Boolean).map((line) => `<li>${escapeHtml(line)}</li>`).join("")}${(Array.isArray(summary.open_questions) ? summary.open_questions.filter(Boolean) : []).map((q) => `<li>${escapeHtml(q)}</li>`).join("")}</ul>` : `<p>Anna can summarize the full thread before drafting.</p>`}
        </div>
        ${summary ? "" : `<div class="proposal-actions"><button class="soft-btn" data-action="summarize-thread">Summarize thread</button></div>`}
      </section>

      <section class="drawer-section">
        <h3 class="drawer-section-title">Draft reply</h3>
        <div class="reply-mode-row">
          <div class="reply-mode-control" role="group" aria-label="Reply mode">
            <button class="reply-mode-btn ${replyMode === "reply_to_sender" ? "is-active" : ""}" data-reply-mode="reply_to_sender">Reply to sender</button>
            <button class="reply-mode-btn ${replyMode === "reply_all" ? "is-active" : ""}" data-reply-mode="reply_all" ${cc && cc !== "None" ? "" : "disabled"}>Reply all</button>
          </div>
          ${cc && cc !== "None" ? "" : `<span class="reply-mode-note">No CC recipients</span>`}
        </div>
        <textarea class="draft-textarea${state.generatingDraft && !draft ? " is-draft-loading" : ""}" id="drawerDraftInput" placeholder="Click 'Generate draft' to have Anna write a reply based on this thread.">${escapeHtml(draftDisplay)}</textarea>
        <div class="revise-row">
          <input type="text" id="drawerRevisionInput" placeholder="${draft ? 'Tell Anna how to revise this draft...' : 'Tell Anna how to write the reply (optional)'}" value="${escapeHtml(state.revisionById[card.id] || "")}" />
          <button class="soft-btn generate-draft-btn is-glow" data-action="unified-draft">${draft ? "Ask Anna to revise" : "Generate draft"}</button>
        </div>
      </section>

      <div class="decision-row drawer-action-row">
        <button class="primary-btn" data-action="drawer-reply-now" ${!draft.trim() ? "disabled" : ""}>Reply now</button>
        <button class="soft-btn" data-action="drawer-no-action">No action needed</button>
        <button class="soft-btn" data-action="drawer-handled-manually">Handled manually</button>
      </div>
    </div>
  `;
}

function renderShellState() {
  if (shell) shell.classList.toggle("is-minimized", state.minimized);
  if (minimizedPill) minimizedPill.classList.toggle("is-visible", state.minimized);
  if (overlay) overlay.classList.toggle("is-open", state.sourcesOpen || state.historyOpen || state.scanPlanOpen);
}

function updateHeader() {
  if (annaAvatar) {
    annaAvatar.textContent = "A";
    annaAvatar.className = `anna-mark anna-avatar ${state.isScanning ? "is-thinking" : ""}`;
  }
  if (brandSubtitle) {
    const planLabel = state.scanPlan && state.scanPlan.time_range !== "auto"
      ? { last_24h: "24h", last_7d: "7d", unread_backlog: "backlog", since_last: "since last" }[state.scanPlan.time_range] || "custom"
      : "auto";
    const scheduleLabel = state.scanPlan && state.scanPlan.schedule !== "manual"
      ? { every_morning: "morning", every_afternoon: "afternoon", twice_daily: "2x/day", workdays: "workdays" }[state.scanPlan.schedule] || "scheduled"
      : "";
    const nextInfo = scheduleLabel ? ` · Next: ${scheduleLabel}` : "";
    brandSubtitle.textContent = state.isScanning
      ? `Scanning ${state.mailbox}`
      : `${visibleCards().length} active · ${planLabel}${nextInfo} · ${state.mailbox}`;
  }
  if (connectionStatus) {
    connectionStatus.textContent = state.runtime.connected ? `Live · ${state.mailbox}` : "Runtime not connected";
    connectionStatus.classList.toggle("is-live", state.runtime.connected);
    connectionStatus.classList.toggle("is-offline", !state.runtime.connected);
  }
}

function modeLabel(mode) {
  return {
    default_secretary: "Default secretary",
    creator_opportunity: "Creator opportunity",
    security_billing: "Security & billing",
  }[mode] || mode || "Default secretary";
}

async function loadCustomPlans() {
  try {
    const payload = await invokeTool("get_custom_plans", storageProviderArgs());
    state.customPlans = Array.isArray(payload.plans) ? payload.plans : [];
  } catch {
    state.customPlans = [];
  } finally {
    render();
  }
}

async function deleteCustomPlan(planId) {
  try {
    await invokeTool("delete_custom_plan", { plan_id: planId, ...storageProviderArgs() });
    state.customPlans = state.customPlans.filter(p => p.plan_id !== planId);
    showToast("Plan deleted.");
    render();
  } catch (error) {
    showToast(error.message || String(error));
  }
}

async function startCustomScan() {
  const input = document.getElementById("customScanInput");
  const userRequest = (input?.value || "").trim();
  if (!userRequest || state.isCustomScanning) return;

  state.customScanInput = userRequest;
  state.isCustomScanning = true;
  state.scanError = "";
  state.scanStatus = "Planning scan strategy...";
  state.customRunResult = null;
  state.customRunProgress = {
    runId: "",
    question: userRequest,
    status: "queued",
    stage: "planning",
    stageKey: "planning",
    progress: {},
    partial: {},
    startedAt: "",
  };
  if (state.view === "ask") render();

  try {
    const started = await invokeTool("start_custom_scan", {
      user_request: userRequest,
      mailbox: state.mailbox,
      primary_count: CUSTOM_SCAN_MESSAGE_LIMIT,
      max_messages: CUSTOM_SCAN_MESSAGE_LIMIT,
      ai_provider: state.llmProvider,
      ...storageProviderArgs(),
    });
    if (!started.run_id) throw new Error(started.error || "start_custom_scan did not return a run id");
    updateCustomRunProgress({
      ...started,
      run_id: started.run_id,
      status: started.status || "queued",
      stage: started.stage || "planning",
      progress: started.progress || {},
      partial: started.partial || {},
    }, { runId: started.run_id, question: userRequest, startedAt: started.started_at || "" });

    for (let poll = 0; poll < POLL_LIMIT; poll += 1) {
      await sleep(POLL_INTERVAL_MS);
      const status = await invokeTool("get_mail_agent_run", { run_id: started.run_id });
      updateCustomRunProgress(status, { runId: started.run_id, question: userRequest });
      state.scanStatus = scanStageLabel(status.stage, status.progress);
      // Only refresh full view on Ask tab; on Brief, just update bottom bar
      if (state.view === "ask") {
        render();
      } else {
        renderBottomBar();
        updateHeader();
      }
      if (status.status === "done") break;
      if (status.status === "failed") throw new Error(status.error || "Custom scan failed");
      if (poll === POLL_LIMIT - 1) throw new Error("Custom scan timed out");
    }
    await loadActiveCards();
    await loadRunHistory();
    await loadCustomPlans();
    state.scanStatus = "";
    state.customScanInput = "";

    // Build inline result for Ask view display
    const doneStatus = await invokeTool("get_mail_agent_run", { run_id: started.run_id });
    const result = doneStatus.result || {};
    state.customRunResult = {
      runId: started.run_id,
      planId: result.plan_id || "",
      plan_title: result.plan_title || "",
      plan_description: result.plan_description || "",
      plan_gmail_queries: result.plan_gmail_queries || [],
      plan_read_depth: result.plan_read_depth || "",
      title: result.title || result.plan_title || "",
      summary: result.summary || "",
      sections: result.sections || [],
      trace: result.trace || {},
      planner_fallback: result.planner_llm && result.planner_llm.fallback_used,
    };
    state.customRunProgress = null;
    const itemCount = (result.sections || []).reduce((sum, s) => sum + (s.items || []).length, 0);
    showToast(`Custom scan complete: ${(result.sections || []).length} sections, ${itemCount} items`);
  } catch (error) {
    state.scanError = error.message || String(error);
    if (state.customRunProgress) {
      state.customRunProgress.status = "failed";
      state.customRunProgress.stageKey = "failed";
    }
    showToast(state.scanError);
  } finally {
    state.isCustomScanning = false;
    if (state.view === "ask") {
      render();
    } else {
      renderBottomBar();
      updateHeader();
    }
  }
}

async function reRunCustomPlan(planId) {
  if (state.isCustomScanning) return;
  state.isCustomScanning = true;
  state.scanError = "";
  state.scanStatus = "Re-running saved scan...";
  const plan = state.customPlans.find((item) => item.plan_id === planId) || {};
  state.customRunResult = null;
  state.customRunProgress = {
    runId: "",
    question: plan.user_request || "Re-run saved scan",
    status: "queued",
    stage: "planning_done",
    stageKey: "planning",
    progress: {},
    partial: {
      plan: {
        plan_id: plan.plan_id || planId,
        title: plan.title || "Saved scan",
        description: plan.description || "",
        gmail_queries: plan.gmail_queries || [],
        read_depth: plan.read_depth || "",
      },
    },
    startedAt: "",
  };
  if (state.view === "ask") render();

  try {
    const started = await invokeTool("re_run_custom_scan", {
      plan_id: planId,
      mailbox: state.mailbox,
      primary_count: CUSTOM_SCAN_MESSAGE_LIMIT,
      max_messages: CUSTOM_SCAN_MESSAGE_LIMIT,
      ai_provider: state.llmProvider,
      ...storageProviderArgs(),
    });
    if (!started.run_id) throw new Error(started.error || "re_run_custom_scan did not return a run id");
    updateCustomRunProgress({
      ...started,
      run_id: started.run_id,
      status: started.status || "queued",
      stage: started.stage || "planning_done",
      progress: started.progress || {},
      partial: state.customRunProgress?.partial || {},
    }, { runId: started.run_id, question: plan.user_request || "Re-run saved scan", startedAt: started.started_at || "" });

    for (let poll = 0; poll < POLL_LIMIT; poll += 1) {
      await sleep(POLL_INTERVAL_MS);
      const status = await invokeTool("get_mail_agent_run", { run_id: started.run_id });
      updateCustomRunProgress(status, { runId: started.run_id, question: plan.user_request || "Re-run saved scan" });
      state.scanStatus = scanStageLabel(status.stage, status.progress);
      if (state.view === "ask") {
        render();
      } else {
        renderBottomBar();
        updateHeader();
      }
      if (status.status === "done") break;
      if (status.status === "failed") throw new Error(status.error || "Custom scan re-run failed");
      if (poll === POLL_LIMIT - 1) throw new Error("Custom scan re-run timed out");
    }
    await loadActiveCards();
    await loadRunHistory();
    await loadCustomPlans();
    state.scanStatus = "";

    // Build inline result for Ask view display
    const doneStatus = await invokeTool("get_mail_agent_run", { run_id: started.run_id });
    const result = doneStatus.result || {};
    state.customRunResult = {
      runId: started.run_id,
      planId: result.plan_id || "",
      plan_title: result.plan_title || "",
      plan_description: result.plan_description || "",
      plan_gmail_queries: result.plan_gmail_queries || [],
      plan_read_depth: result.plan_read_depth || "",
      title: result.title || result.plan_title || "",
      summary: result.summary || "",
      sections: result.sections || [],
      trace: result.trace || {},
      planner_fallback: result.planner_llm && result.planner_llm.fallback_used,
    };
    state.customRunProgress = null;
    const itemCount = (result.sections || []).reduce((sum, s) => sum + (s.items || []).length, 0);
    showToast(`Re-run complete: ${(result.sections || []).length} sections, ${itemCount} items`);
  } catch (error) {
    state.scanError = error.message || String(error);
    if (state.customRunProgress) {
      state.customRunProgress.status = "failed";
      state.customRunProgress.stageKey = "failed";
    }
    showToast(state.scanError);
  } finally {
    state.isCustomScanning = false;
    if (state.view === "ask") {
      render();
    } else {
      renderBottomBar();
      updateHeader();
    }
  }
}

async function loadActiveCards() {
  try {
    const payload = await invokeTool("get_active_cards", { mailbox: state.mailbox, ...storageProviderArgs() });
    state.cards = Array.isArray(payload.cards) ? payload.cards : [];
    state.scanState = payload.scan_state || null;
    state.scanError = "";

    state.cards.forEach(card => {
      if (card.draft_reply && !state.draftById[card.id]) {
        state.draftById[card.id] = card.draft_reply;
      }
      if (card.thread_summary && !state.threadSummaryById[card.id]) {
        try {
          state.threadSummaryById[card.id] = JSON.parse(card.thread_summary);
        } catch {
        }
      }
      if (card.cardType === "cleanup_bundle" && !(card.id in state.expandedDetails)) {
        state.expandedDetails[card.id] = true;
      }
    });
  } catch (error) {
    state.scanError = error.message || String(error);
    state.cards = [];
    state.scanState = null;
    return;
  } finally {
    state.loading = false;
    render();
  }
}

async function loadRunHistory() {
  try {
    const payload = await invokeTool("get_run_history", storageProviderArgs());
    state.history = Array.isArray(payload.history) ? payload.history : [];
  } catch {
    state.history = [];
  }
}

function requestForMode(mode) {
  return {
    default_secretary: "Brief this mailbox and surface only emails that need attention.",
    creator_opportunity: "Review creator and partnership opportunities that need follow-up.",
    security_billing: "Check recent security, account, billing, and subscription notices.",
  }[mode] || "Brief this mailbox and surface only emails that need attention.";
}

async function startScan({ reason = "manual" } = {}) {
  if (!state.runtime.connected || state.isScanning) return;
  state.isScanning = true;
  state.scanError = "";
  state.scanStepIndex = 0;
  state.scanStage = "";
  state.scanProgress = {};
  state.resultFilter = "all";
  // Stay on current view — let the user switch between Brief and Ask freely
  render();
  try {
    const started = await invokeTool("start_mail_agent_run", {
      user_request: requestForMode(state.strategyMode),
      mailbox: state.mailbox,
      mode: state.strategyMode,
      primary_count: 30,
      max_messages: 50,
      ai_provider: state.llmProvider,
      ...storageProviderArgs(),
    });
    if (!started || !started.run_id) throw new Error((started && started.error) || "start_mail_agent_run did not return a run id");
    for (let poll = 0; poll < POLL_LIMIT; poll += 1) {
      await sleep(POLL_INTERVAL_MS);
      const status = await invokeTool("get_mail_agent_run", { run_id: started.run_id });
      const progress = status.progress || {};
      state.scanStepIndex = stageToStep(status.stage || "");
      state.scanStage = status.stage || "";
      state.scanProgress = progress;
      // Only update the full view on Brief tab; on Ask, just refresh the bottom bar
      if (state.view === "start") {
        render();
      } else {
        renderBottomBar();
        updateHeader();
      }
      if (status.status === "done") break;
      if (status.status === "failed") throw new Error(status.error || "Mail agent scan failed");
      if (poll === POLL_LIMIT - 1) throw new Error("Mail agent scan timed out");
    }
    await loadActiveCards();
    await loadRunHistory();
    state.scanStatus = "Scan complete. Showing persisted attention cards.";
    showToast(`Scan complete: ${visibleCards().length} active card${visibleCards().length === 1 ? "" : "s"}`);
  } catch (error) {
    state.scanError = error.message || String(error);
    state.scanStatus = "";
    showToast(state.scanError);
  } finally {
    state.isScanning = false;
    if (state.view === "start") {
      render();
    } else {
      renderBottomBar();
      updateHeader();
    }
  }
}

const STAGE_STEP_MAP = {
  queued: 0, parse_intent: 0, scan: 0, scan_done: 0,
  storage_filter: 1, thread_dedup: 1, phase1: 1, phase1_done: 1,
  read_context: 2, read_context_done: 2,
  evaluate: 3, evaluate_done: 3,
  plan: 4, storage_saved: 4, done: 4,
  planning: 0, planning_done: 0,
};

function stageToStep(stage) {
  return STAGE_STEP_MAP[stage] ?? 0;
}

function scanStageLabel(stage, progress) {
  const labels = {
    queued: "Scan queued.",
    planning: "Generating scan plan with Anna LLM.",
    planning_done: "Plan ready. Starting scan.",
    parse_intent: "Choosing scan strategy.",
    scan: "Reading Gmail source.",
    scan_done: "New messages loaded.",
    storage_filter: "Skipping messages already processed.",
    phase1: "Finding candidate attention items.",
    phase1_done: "Candidate scan complete.",
    read_context: "Reading context for candidates.",
    read_context_done: "Context loaded.",
    evaluate: "Evaluating cards.",
    evaluate_done: "Evaluation complete.",
    plan: "Building action plan.",
    storage_saved: "Persisting cards locally.",
    done: "Scan complete.",
  };
  const count = progress && progress.current && progress.total ? ` (${progress.current}/${progress.total})` : "";
  return `${labels[stage] || stage || "Scanning."}${count}`;
}

function scanProgressLabel(stage, progress) {
  const p = progress || {};
  if (!stage || stage === "queued") return "";
  if (stage === "scan" || stage === "parse_intent") return "Connecting to Gmail…";
  if (stage === "scan_done") return `${p.scanned || 0} emails loaded`;
  if (stage === "storage_filter") return `${p.skipped || 0} skipped, ${p.new || 0} new`;
  if (stage === "thread_dedup") return `${p.after || 0} after dedup`;
  if (stage === "phase1") return `Classifying ${p.scanned || 0} emails…`;
  if (stage === "phase1_done") return `${p.candidates || 0} candidates, ${p.low_value || 0} low-priority`;
  if (stage === "read_context") return `Reading context ${p.current || 0}/${p.total || 0}`;
  if (stage === "read_context_done") return `${p.total || 0} contexts loaded`;
  if (stage === "evaluate") return `Evaluating ${p.evaluated || 0}/${p.total || 0}`;
  if (stage === "evaluate_done") return `${p.evaluated || p.total || 0} evaluated`;
  if (stage === "plan") return "Building action plan…";
  if (stage === "storage_saved") return "Cards saved.";
  if (stage === "done") return "Scan complete.";
  return "";
}

async function openCard(cardId) {
  const card = visibleCards().find((item) => item.id === cardId);
  if (!card) return;
  state.selectedCard = card;
  state.selectedCardDetail = null;
  state.originalOpen = true;
  state.sourcesOpen = false;
  state.historyOpen = false;
  state.snoozeMenuCardId = "";
  render();
  try {
    state.selectedCardDetail = await invokeTool("get_card_detail", { mailbox: state.mailbox, card_id: cardId, ...storageProviderArgs() });
    render();
  } catch {
  }
}

async function summarizeSelectedThread() {
  if (!state.selectedCard || state.summarizingThread) return;
  const cardId = state.selectedCard.id;
  state.summarizingThread = true;
  render();
  try {
    const started = await invokeTool("start_summarize_thread", { mailbox: state.mailbox, card_id: cardId, ...storageProviderArgs(), ...aiProviderArgs() });
    const result = await pollBackgroundRun(started.run_id);
    const summary = (result || {}).summary || {};
    state.threadSummaryById[cardId] = summary;
  } catch (error) {
    showToast(error.message || String(error));
  } finally {
    state.summarizingThread = false;
    render();
  }
}

async function generateDraft() {
  if (!state.selectedCard || state.generatingDraft) return;
  const cardId = state.selectedCard.id;
  const draftInput = document.getElementById("drawerDraftInput");
  const revisionInput = document.getElementById("drawerRevisionInput");
  const currentDraft = draftInput ? draftInput.value : state.draftById[cardId] || "";
  const revision = revisionInput ? revisionInput.value.trim() : "";

  state.generatingDraft = true;
  const dotsFrames = ["", ".", "..", "..."];
  let dotsIdx = 0;
  const dotsTimer = setInterval(() => {
    dotsIdx = (dotsIdx + 1) % dotsFrames.length;
    state.draftDots = dotsFrames[dotsIdx];
    if (state.originalOpen && state.view === "start") render();
  }, 400);
  render();
  try {
    const started = await invokeTool("start_generate_draft", {
      mailbox: state.mailbox,
      card_id: cardId,
      reply_mode: state.replyModeById[cardId] || "reply_to_sender",
      current_draft: currentDraft,
      revision_input: revision,
      ...storageProviderArgs(),
      ...aiProviderArgs(),
    });
    const result = await pollBackgroundRun(started.run_id);
    state.draftById[cardId] = (result && result.draft && result.draft.body) || (result && result.body) || currentDraft || "";
    if (revision) state.revisionById[cardId] = revision;
  } catch (error) {
    showToast(error.message || String(error));
  } finally {
    clearInterval(dotsTimer);
    state.draftDots = "";
    state.generatingDraft = false;
    render();
  }
}

async function _removed_reviseDraftWithInput(currentDraft, revision) {
  if (!state.selectedCard || !currentDraft || !revision || state.generatingDraft) return;
  const cardId = state.selectedCard.id;
  state.generatingDraft = true;
  render();
  try {
    const started = await invokeTool("start_revise_draft", {
      mailbox: state.mailbox,
      card_id: cardId,
      current_draft: currentDraft,
      revision_input: revision,
      ...storageProviderArgs(),
      ...aiProviderArgs(),
    });
    const result = await pollBackgroundRun(started.run_id);
    state.draftById[cardId] = (result && result.revised && result.revised.body) || (result && result.draft && result.draft.body) || (result && result.body) || currentDraft;
    state.revisionById[cardId] = revision;
  } catch (error) {
    showToast(error.message || String(error));
  } finally {
    state.generatingDraft = false;
    render();
  }
}

async function reviseDraft() {
  if (!state.selectedCard || state.generatingDraft) return;
  const cardId = state.selectedCard.id;
  const draftInput = document.getElementById("drawerDraftInput");
  const revisionInput = document.getElementById("drawerRevisionInput");
  const currentDraft = draftInput ? draftInput.value : state.draftById[cardId] || "";
  const revision = revisionInput ? revisionInput.value.trim() : "";
  if (!currentDraft || !revision) return;
  state.generatingDraft = true;
  render();
  try {
    const payload = await invokeTool("revise_draft", {
      mailbox: state.mailbox,
      card_id: cardId,
      current_draft: currentDraft,
      revision_input: revision,
      ...storageProviderArgs(),
      ...aiProviderArgs(),
    });
    state.draftById[cardId] = payload.revised?.body || payload.draft?.body || payload.body || currentDraft;
    state.revisionById[cardId] = revision;
  } catch (error) {
    showToast(error.message || String(error));
  } finally {
    state.generatingDraft = false;
    render();
  }
}

async function recordDecision(decision, cardId) {
  const cid = cardId || (state.selectedCard && state.selectedCard.id);
  if (!cid) return;
  try {
    await invokeTool("record_card_decision", { mailbox: state.mailbox, card_id: cid, decision, ...storageProviderArgs() });
    state.originalOpen = false;
    state.selectedCard = null;
    state.expandedDetails[cid] = false;
    await loadActiveCards();
    showToast("Card removed from this briefing.");
  } catch (error) {
    showToast(error.message || String(error));
  }
}

async function handleAskMarkRead(mid) {
  if (!mid || state.askItemActions[mid]?.read) return;
  state.askItemActions[mid] = { ...state.askItemActions[mid], read: true };
  renderAsk();
  try {
    const result = await invokeTool("mark_read_from_ask", {
      mailbox: state.mailbox,
      message_ids: [mid],
    });
    if (!result || !result.ok) {
      state.askItemActions[mid].read = false;
      showToast(result?.error || "Failed to mark as read");
    }
  } catch (error) {
    state.askItemActions[mid].read = false;
    showToast(error.message || String(error));
  }
  renderAsk();
}

async function handleAskTrash(mid) {
  if (!mid || state.askItemActions[mid]?.trashed) return;
  state.askItemActions[mid] = { ...state.askItemActions[mid], trashed: true };
  renderAsk();
  try {
    const result = await invokeTool("trash_from_ask", {
      mailbox: state.mailbox,
      message_ids: [mid],
    });
    if (!result || !result.ok) {
      state.askItemActions[mid].trashed = false;
      showToast(result?.error || "Failed to trash email");
    }
  } catch (error) {
    state.askItemActions[mid].trashed = false;
    showToast(error.message || String(error));
  }
  renderAsk();
}

async function handleAskReply(btn) {
  const tid = btn.dataset.tid;
  const to = btn.dataset.to;
  const draft = btn.dataset.draft || "";
  const key = btn.dataset.key || tid;
  if (!tid || !to) return;
  if (!draft.trim()) { showToast("No draft to send."); return; }
  // Enter edit mode
  state.askEditDraft[key] = draft;
  // Sync textarea input back to state
  setTimeout(() => {
    const ta = document.getElementById("ask-edit-" + key.replace(/[^a-zA-Z0-9_-]/g, ""));
    if (ta) {
      ta.addEventListener("input", function() { state.askEditDraft[key] = this.value; });
    }
  }, 50);
  renderAsk();
}

async function handleAskSendDraft(key, tid, to) {
  const draft = (state.askEditDraft[key] || "").trim();
  if (!draft) { showToast("Draft is empty."); return; }
  if (state.askItemActions[key]?.sending) return;
  state.askItemActions[key] = { ...state.askItemActions[key], sending: true };
  renderAsk();
  try {
    const result = await invokeTool("reply_from_ask", {
      mailbox: state.mailbox,
      thread_id: tid,
      to_addr: to,
      body: draft,
      reply_mode: "reply_to_sender",
      dry_run: false,
    });
    if (result && result.ok && !result.dry_run) {
      state.askItemActions[key] = { ...state.askItemActions[key], sending: false, replied: true };
      delete state.askEditDraft[key];
      showToast("Reply sent.");
    } else {
      state.askItemActions[key].sending = false;
      showToast(result?.error || "Failed to send reply.");
    }
  } catch (error) {
    state.askItemActions[key].sending = false;
    showToast(error.message || String(error));
  }
  renderAsk();
}

function handleAskCancelDraft(key) {
  delete state.askEditDraft[key];
  renderAsk();
}

async function replyNow() {
  if (!state.selectedCard) return;
  const card = state.selectedCard;
  const draft = state.draftById[card.id] || "";
  if (!draft.trim()) {
    showToast("Draft is empty. Generate a draft first.");
    return;
  }
  try {
    await invokeTool("reply_now", {
      mailbox: state.mailbox,
      card_id: card.id,
      draft_body: draft,
      reply_mode: state.replyModeById[card.id] || "reply_to_sender",
      dry_run: false,
    });
    state.originalOpen = false;
    state.selectedCard = null;
    showToast("Reply sent successfully.");
    await loadActiveCards();
  } catch (error) {
    showToast(error.message || String(error));
  }
}

async function clearAllCards() {
  try {
    await invokeTool("clear_active_cards", { mailbox: state.mailbox, ...storageProviderArgs() });
    state.cards = [];
    state.scanState = null;
    state.expandedDetails = {};
    state.cleanupReadState = {};
    state.markingReadIds = {};
    showToast("All cards cleared.");
  } catch (error) {
    showToast(error.message || String(error));
  }
  render();
}

async function markCleanupAsRead(cardId) {
  const card = state.cards.find(c => c.id === cardId);
  if (!card) return;
  const messages = Array.isArray(card.bundledMessages) ? card.bundledMessages : [];
  const total = card.bundledCount || messages.length;
  if (!total || state.cleanupReadState[cardId]?.read || state.markingReadIds[cardId]) return;

  const messageIds = messages.map(m => m.message_id || m.id).filter(Boolean);
  if (!messageIds.length) return;

  state.markingReadIds[cardId] = true;

  // Start local animation
  state.cleanupReadState[cardId] = { read: false, readMsgIndices: [] };
  for (let i = 0; i < total; i++) {
    await sleep(50);
    state.cleanupReadState[cardId].readMsgIndices.push(i);
    if (i === total - 1) state.cleanupReadState[cardId].read = true;
    render();
  }

  // Call backend to mark read in Gmail and storage
  try {
    const result = await invokeTool("mark_cleanup_read", {
      mailbox: state.mailbox,
      card_id: cardId,
      message_ids: messageIds,
      ...storageProviderArgs(),
    });
    if (result && result.ok) {
      showToast(total + " emails marked as read in Gmail.");
      state.cards = state.cards.filter(c => c.id !== cardId);
      delete state.expandedDetails[cardId];
      delete state.cleanupReadState[cardId];
    } else {
      showToast(result?.gmail_error || "Failed to mark as read in Gmail.");
    }
  } catch (error) {
    showToast(error.message || String(error));
  } finally {
    delete state.markingReadIds[cardId];
  }
  render();
}

async function restoreCard(cardId) {
  try {
    await invokeTool("restore_card", { mailbox: state.mailbox, card_id: cardId, ...storageProviderArgs() });
    await loadActiveCards();
    showToast("Card restored.");
  } catch (error) {
    showToast(error.message || String(error));
  }
}

async function snoozeCard(cardId, option) {
  const optionMap = {
    tomorrow: "tomorrow",
    "next-week": "next_week",
    "dont-prioritize": "dont_prioritize",
  };
  try {
    await invokeTool("record_snooze", {
      mailbox: state.mailbox,
      card_id: cardId,
      snooze_option: optionMap[option] || option,
      ...storageProviderArgs(),
    });
    state.snoozeMenuCardId = "";
    await loadActiveCards();
    showToast(option === "dont-prioritize" ? "Preference saved." : "Card snoozed.");
  } catch (error) {
    showToast(error.message || String(error));
  }
}

function closeDrawers() {
  state.sourcesOpen = false;
  state.historyOpen = false;
  state.originalOpen = false;
  state.scanPlanOpen = false;
  state.selectedCard = null;
  render();
}

function startPeriodicScanIfNeeded() {
  const schedule = state.scanPlan?.schedule || "manual";
  if (schedule === "manual") return;
  // Map schedule to interval: every_morning/every_afternoon → 12h, twice_daily → 12h, workdays → 24h
  const intervalMs = { twice_daily: 12, every_morning: 24, every_afternoon: 24, workdays: 24 }[schedule];
  if (!intervalMs) return;
  window.setInterval(() => {
    if (state.runtime.connected && !state.isScanning) {
      startScan({ reason: "periodic" });
    }
  }, intervalMs * 60 * 60 * 1000);
}

async function loadScanPlan() {
  try {
    state.scanPlan = await invokeTool("get_scan_plan", { mailbox: state.mailbox, ...storageProviderArgs() });
  } catch {
    state.scanPlan = { time_range: "auto", max_messages: 50, schedule: "manual", include_newsletters: false, include_promotions: false };
  }
  render();
}

async function saveScanPlanField(field, value) {
  const args = { mailbox: state.mailbox, ...storageProviderArgs() };
  args[field] = value;
  try {
    await invokeTool("set_scan_plan", args);
    state.scanPlan[field] = value;
    state.scanPlan.updated_at = new Date().toISOString();
    showToast("Scan plan updated");
    render();
  } catch (e) {
    showToast("Failed: " + (e.message || "unknown"));
  }
}

document.addEventListener("input", (event) => {
  if (event.target && event.target.id === "strategySelect") {
    state.strategyMode = event.target.value;
  }
  if (event.target && event.target.id === "drawerDraftInput" && state.selectedCard) {
    state.draftById[state.selectedCard.id] = event.target.value;
  }
  if (event.target && event.target.id === "drawerRevisionInput" && state.selectedCard) {
    state.revisionById[state.selectedCard.id] = event.target.value;
  }
  if (event.target && event.target.id === "customScanInput") {
    state.customScanInput = event.target.value;
  }
  if (event.target && event.target.id === "mailboxInput") {
    state.mailboxInput = event.target.value;
  }
});

document.addEventListener("keydown", (event) => {
  if (event.target && event.target.id === "mailboxInput" && event.key === "Enter") {
    event.preventDefault();
    applyMailbox();
  }
});

document.addEventListener("click", (event) => {
  const snoozeButton = event.target.closest("[data-snooze-card]");
  if (snoozeButton) {
    const cardId = snoozeButton.dataset.snoozeCard;
    state.snoozeMenuCardId = state.snoozeMenuCardId === cardId ? "" : cardId;
    render();
    return;
  }

  const snoozeOption = event.target.closest("[data-snooze-option]");
  if (snoozeOption) {
    const [cardId, option] = snoozeOption.dataset.snoozeOption.split(":");
    snoozeCard(cardId, option);
    return;
  }

  const detailsButton = event.target.closest("[data-toggle-details]");
  if (detailsButton) {
    const cardId = detailsButton.dataset.toggleDetails;
    state.expandedDetails[cardId] = !state.expandedDetails[cardId];
    state.snoozeMenuCardId = "";
    render();
    return;
  }

  const handleButton = event.target.closest("[data-handle-card]");
  if (handleButton) {
    const [cardId] = handleButton.dataset.handleCard.split(":");
    openCard(cardId);
    return;
  }

  const cleanupMarkReadButton = event.target.closest("[data-cleanup-mark-read]");
  if (cleanupMarkReadButton) {
    const cardId = cleanupMarkReadButton.dataset.cleanupMarkRead;
    markCleanupAsRead(cardId);
    return;
  }

  const resultFilterButton = event.target.closest("[data-result-filter]");
  if (resultFilterButton) {
    state.resultFilter = resultFilterButton.dataset.resultFilter;
    render();
    return;
  }

  const debugProviderButton = event.target.closest("[data-debug-provider]");
  if (debugProviderButton) {
    const [kind, value] = String(debugProviderButton.dataset.debugProvider || "").split(":");
    if (kind === "llm" && (value === "dashscope" || value === "anna-llm")) {
      state.llmProvider = value;
      showToast(value === "dashscope" ? "LLM: DashScope" : "LLM: Anna sampling");
    }
    if (kind === "storage" && (value === "aps" || value === "local")) {
      state.storageProvider = value;
      state.customPlans = [];
      showToast(value === "aps" ? "Storage: APS" : "Storage: local");
      loadActiveCards();
      loadRunHistory();
      loadCustomPlans();
    }
    render();
    return;
  }

  const replyModeButton = event.target.closest("[data-reply-mode]");
  if (replyModeButton && state.selectedCard) {
    state.replyModeById[state.selectedCard.id] = replyModeButton.dataset.replyMode;
    render();
    return;
  }

  const restoreButton = event.target.closest("[data-restore-card]");
  if (restoreButton) {
    restoreCard(restoreButton.dataset.restoreCard);
    return;
  }

  const revisePreset = event.target.closest("[data-revise-preset]");
  if (revisePreset && state.selectedCard) {
    const presetMap = { Shorter: "Make it shorter", Warmer: "Make it warmer", "More direct": "Make it more direct" };
    const preset = revisePreset.dataset.revisePreset;
    const inputEl = document.getElementById("drawerRevisionInput");
    if (inputEl) inputEl.value = presetMap[preset] || preset;
    generateDraft();
    return;
  }

  // ── Ask item action buttons (check before data-action) ──
  const askActionBtn = event.target.closest("[data-ask-action]");
  if (askActionBtn) {
    const askAction = askActionBtn.dataset.askAction;
    const mid = askActionBtn.dataset.mid;
    const key = askActionBtn.dataset.key;
    const tid = askActionBtn.dataset.tid;
    const to = askActionBtn.dataset.to;
    if (askAction === "read") handleAskMarkRead(mid);
    if (askAction === "trash") handleAskTrash(mid);
    if (askAction === "reply") handleAskReply(askActionBtn);
    if (askAction === "send-draft") handleAskSendDraft(key, tid, to);
    if (askAction === "cancel-draft") handleAskCancelDraft(key);
    return;
  }

  const actionEl = event.target.closest("[data-action]");
  if (!actionEl) return;
  const action = actionEl.dataset.action;

  if (action === "open-sources") {
    state.sourcesOpen = true;
    state.historyOpen = false;
    state.originalOpen = false;
    state.scanPlanOpen = false;
    render();
  }
  if (action === "open-scan-plan") {
    state.scanPlanOpen = true;
    state.sourcesOpen = false;
    state.historyOpen = false;
    state.originalOpen = false;
    loadScanPlan();
    render();
  }
  if (action === "history") {
    state.historyOpen = true;
    state.sourcesOpen = false;
    state.originalOpen = false;
    render();
  }
  if (action === "go-brief") {
    state.view = "start";
    state.customRunResult = null;
    state.lowerPriorityOpen = false;
    closeDrawers();
  }
  if (action === "go-ask") {
    state.view = "ask";
    state.sourcesOpen = false;
    state.historyOpen = false;
    state.originalOpen = false;
    loadCustomPlans();
    render();
  }
  if (action === "close-drawers") closeDrawers();
  if (action === "minimize") {
    state.minimized = true;
    render();
  }
  if (action === "toggle-lower-priority") {
    state.lowerPriorityOpen = !state.lowerPriorityOpen;
    render();
  }
  if (action === "clear-all-cards") {
    clearAllCards();
    return;
  }
  if (action === "toggle-custom-trace") {
    state.customTraceOpen = !state.customTraceOpen;
    render();
  }

  // Scan plan chip clicks
  const scanPlanChip = event.target.closest("[data-set-scan-plan]");
  if (scanPlanChip) {
    const raw = scanPlanChip.dataset.setScanPlan;
    const colon = raw.indexOf(":");
    const field = raw.slice(0, colon);
    const strVal = raw.slice(colon + 1);
    let value;
    if (strVal === "true") value = true;
    else if (strVal === "false") value = false;
    else if (/^\d+$/.test(strVal)) value = parseInt(strVal, 10);
    else value = strVal;
    saveScanPlanField(field, value);
    return;
  }

  if (action === "first-scan") startScan({ reason: "first" });
  if (action === "retry-auth-check") {
    state.gmailAuthStatus.checked = false;
    checkGmailAuth().then(() => render());
  }
  if (action === "apply-mailbox") {
    applyMailbox();
    return;
  }
  if (action === "manual-scan") startScan({ reason: "manual" });
  if (action === "run-custom-scan") startCustomScan();
  if (action === "re-run-custom-plan") {
    const planId = actionEl.dataset.planId;
    if (planId) reRunCustomPlan(planId);
  }
  if (action === "delete-custom-plan") {
    const planId = actionEl.dataset.planId;
    if (planId) deleteCustomPlan(planId);
  }
  if (action === "copy-draft") {
    const draftText = actionEl.dataset.draft || "";
    navigator.clipboard.writeText(draftText).then(() => showToast("Draft copied")).catch(() => showToast("Copy failed"));
  }

  if (action === "summarize-thread") summarizeSelectedThread();
  if (action === "unified-draft") generateDraft();
  if (action === "drawer-no-action") recordDecision("no_action_needed");
  if (action === "drawer-handled-manually") recordDecision("handled_manually");
  if (action === "drawer-reply-now") replyNow();
  if (action === "toggle-thread-context" && state.selectedCard) {
    state.threadContextExpanded[state.selectedCard.id] = !state.threadContextExpanded[state.selectedCard.id];
    render();
  }
  if (action === "next-card") {
    const nid = nextCardId();
    if (nid) openCard(nid);
  }
});

if (minimizedPill) {
  minimizedPill.addEventListener("click", () => {
    state.minimized = false;
    render();
  });
}

async function init() {
  render();
  try {
    state.runtime = await getAnna();
    if (!state.runtime.connected) {
      state.loading = false;
    }
    // Check Gmail authorization (runs regardless of runtime connection)
    await checkGmailAuth();
    render();
    if (state.runtime.connected) {
      if (!state.gmailAuthStatus.authorized) {
        state.loading = false;
        return;
      }
      await loadRunHistory();
      await loadCustomPlans();
      await loadScanPlan();
      await loadActiveCards();
      startPeriodicScanIfNeeded();
    }
  } catch (error) {
    if (!state.runtime.connected) {
      state.runtime = { connected: false, mode: "mock", error: error.message || String(error) };
    }
    state.loading = false;
    state.scanError = state.scanError || error.message || String(error);
    render();
  }
}

init();
