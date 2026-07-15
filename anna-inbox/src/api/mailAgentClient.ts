import type {
  ActiveCardsPayload,
  AttachmentDownloadPayload,
  CardDetailPayload,
  CleanupBundlePayload,
  ContactMemoryDetailPayload,
  ContactMemorySummary,
  ContactAvatarPayload,
  ComposeContact,
  ComposeDraft,
  ComposeDraftListPayload,
  CustomPlanSummary,
  LlmStatus,
  InboxFeedPayload,
  InboxEmailDetailPayload,
  InboxMessageDisplayBodyPayload,
  InboxThreadDraftPayload,
  InboxThreadPagePayload,
  InboxSettingsPayload,
  InboxSettings,
  MailboxListPayload,
  MailboxInfo,
  RunHistoryEntry,
  RunStatus,
  RuntimeState,
  ScanPlan,

} from "../types/mail";
import appManifest from "../../manifest.json";

const BUNDLED_EXECUTA_HANDLE = "inbox-executa";

declare global {
  interface Window {
    __ANNA_TOOL_IDS__?: Record<string, string>;
  }
}

function getRequiredExecutaToolId(): string {
  const resolvedToolId = typeof window !== "undefined"
    ? window.__ANNA_TOOL_IDS__?.[BUNDLED_EXECUTA_HANDLE]
    : undefined;
  if (resolvedToolId) {
    return resolvedToolId;
  }

  const toolId = appManifest.required_executas[0]?.tool_id;
  if (!toolId) {
    throw new Error("anna-inbox/manifest.json is missing required_executas[0].tool_id");
  }
  return toolId;
}

const TOOL_ID = getRequiredExecutaToolId();
const INVOKE_TIMEOUT_MS = 180000;
/** Ask 初等等待 60s + 网络余量；后端超时后返回可轮询状态，勿与 wait_timeout 倒置。 */
const CUSTOM_SCAN_INVOKE_TIMEOUT_MS = 120_000;
const SAFE_RETRY_DELAYS_MS = [500, 1000];

export function isRetryableToolInvocationError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  return /unexpected token\s+['"]?<|<!doctype html|text\/html|failed to fetch|network(?:error| request)?|fetch failed|econnreset|enotfound|etimedout|timeout|\b(?:429|5\d\d)\b/i.test(message);
}

function waitForRetry(delayMs: number) {
  return new Promise<void>((resolve) => globalThis.setTimeout(resolve, delayMs));
}

export function unwrapToolResult(result: unknown): unknown {
  const envelope = result && typeof result === "object" && "result" in result && "jsonrpc" in result
    ? (result as { result: unknown }).result
    : result;
  const payload = envelope && typeof envelope === "object" && "data" in envelope && "tool" in envelope
    ? (envelope as { data: unknown }).data
    : envelope;
  if (payload && typeof payload === "object" && "error" in payload && (payload as { error?: unknown }).error) {
    throw new Error(String((payload as { error: unknown }).error));
  }
  if (payload && typeof payload === "object" && (payload as { success?: unknown }).success === false) {
    throw new Error(String((payload as { error?: unknown }).error || "Tool call failed"));
  }
  if (payload && typeof payload === "object" && (payload as { success?: unknown }).success === true && "data" in payload) {
    return (payload as { data: unknown }).data;
  }
  return payload;
}

export class MailAgentClient {
  constructor(private readonly getRuntime: () => Promise<RuntimeState>) {}

  async invoke<T = unknown>(method: string, args: Record<string, unknown> = {}, options: { timeoutMs?: number; retry?: "safe" } = {}): Promise<T> {
    const runtime = await this.getRuntime();
    if (!runtime.connected || !runtime.client) {
      throw new Error(runtime.error || "Anna runtime is not connected.");
    }
    const timeoutMs = options.timeoutMs || INVOKE_TIMEOUT_MS;
    const invokeArgs = {
      tool_id: TOOL_ID,
      method,
      args,
      timeoutMs,
    };
    for (let attempt = 0; ; attempt += 1) {
      try {
        const result = runtime.client.tools && typeof runtime.client.tools.invoke === "function"
          ? await runtime.client.tools.invoke(invokeArgs, { timeoutMs })
          : await runtime.client.call?.("tools", "invoke", invokeArgs, { timeout: timeoutMs, timeoutMs });
        return unwrapToolResult(result) as T;
      } catch (error) {
        const err = error as { details?: Record<string, unknown>; data?: Record<string, unknown>; code?: string | number; message?: string };
        const details = err.details || err.data || {};
        const data = details.data as Record<string, unknown> | undefined;
        const traceback = String(details.traceback || data?.traceback || "");
        const code = err.code !== undefined ? `[${err.code}] ` : "";
        const message = err.message || String(error);
        const wrapped = new Error(`[tool:${method}] ${code}${message}${traceback ? `\n\n${traceback}` : ""}`);
        if (options.retry === "safe" && attempt < SAFE_RETRY_DELAYS_MS.length && isRetryableToolInvocationError(wrapped)) {
          await waitForRetry(SAFE_RETRY_DELAYS_MS[attempt]);
          continue;
        }
        throw wrapped;
      }
    }
  }

  getAuthorizedMailbox() {
    return this.invoke<{ mailbox?: string; source?: string }>("get_authorized_email");
  }

  checkGmailAuth(mailbox: string) {
    return this.invoke<{ authorized?: boolean; source?: string }>("check_gmail_auth", { mailbox });
  }

  checkAnyGmailAuth() {
    return this.invoke<{ authorized?: boolean; source?: string; authorized_email?: string }>("check_gmail_auth", { mailbox: "" });
  }

  checkSamplingStatus() {
    return this.invoke<LlmStatus & { ok?: boolean; code?: string; provider?: string }>("check_sampling_status", {}, { timeoutMs: 15_000 });
  }

  listMailboxes(storageProvider: string) {
    return this.invoke<MailboxListPayload>("list_mailboxes", { storage_provider: storageProvider });
  }

  getMailboxRegistry(storageProvider: string) {
    return this.invoke<{ mailboxes: MailboxInfo[]; selected: string[]; discovered?: MailboxInfo[] }>("get_mailbox_registry", { storage_provider: storageProvider });
  }

  setMailboxSelected(mailbox: string, selected: boolean, storageProvider: string) {
    return this.invoke<{ ok?: boolean; mailboxes: MailboxInfo[]; selected: string[] }>("set_mailbox_selected", { mailbox, selected, storage_provider: storageProvider });
  }

  removeMailbox(mailbox: string, storageProvider: string) {
    return this.invoke<{ ok?: boolean; mailboxes: MailboxInfo[]; selected: string[] }>("remove_mailbox", { mailbox, storage_provider: storageProvider });
  }

  loadActiveCards(mailbox: string, storageProvider: string, offset = 0, limit = 50, timeoutMs = 55_000) {
    return this.invoke<ActiveCardsPayload>("get_active_cards", { mailbox, storage_provider: storageProvider, offset, limit }, { timeoutMs });
  }

  loadCleanupBundlePage(mailbox: string, storageProvider: string, offset = 0, limit = 100, timeoutMs = 55_000) {
    return this.invoke<CleanupBundlePayload>("get_cleanup_bundle_page", { mailbox, storage_provider: storageProvider, offset, limit }, { timeoutMs });
  }

  loadRunHistory() {
    return this.invoke<{ history: RunHistoryEntry[] }>("get_run_history");
  }

  loadScanPlan(mailbox: string, storageProvider: string) {
    return this.invoke<ScanPlan>("get_scan_plan", { mailbox, storage_provider: storageProvider });
  }


  saveScanPlanField(mailbox: string, storageProvider: string, field: string, value: unknown) {
    return this.invoke<{ ok?: boolean; updated_at?: string }>("set_scan_plan", { mailbox, storage_provider: storageProvider, [field]: value });
  }

  startBriefRun(args: Record<string, unknown>) {
    return this.invoke<RunStatus>("start_mail_agent_run", args);
  }

  continueBriefRun(args: Record<string, unknown>) {
    return this.invoke<RunStatus>("continue_mail_agent_run", args, { timeoutMs: 55_000 });
  }

  getRun(runId: string) {
    return this.invoke<RunStatus>("get_mail_agent_run", { run_id: runId }, { retry: "safe" });
  }

  getCardDetail(mailbox: string, cardId: string, storageProvider: string, includeBody = false) {
    return this.invoke<CardDetailPayload>("get_card_detail", { mailbox, card_id: cardId, storage_provider: storageProvider, include_body: includeBody });
  }

  loadInboxSettings(mailbox: string, storageProvider: string) {
    return this.invoke<InboxSettingsPayload>("get_inbox_settings", { mailbox, storage_provider: storageProvider });
  }

  saveInboxSettings(mailbox: string, patch: Partial<InboxSettings>, ifMatch: string, storageProvider: string) {
    return this.invoke<InboxSettingsPayload & { ok?: boolean }>("save_inbox_settings", { mailbox, if_match: ifMatch, storage_provider: storageProvider, ...patch });
  }

  listInboxEmails(mailbox: string, days = 30, limit = 100, category = "inbox", clearCache = false) {
    return this.invoke<InboxFeedPayload>("list_inbox_emails", { mailbox, days, limit, category, clear_cache: clearCache }, { timeoutMs: 120_000 });
  }

  listCachedEmails(mailbox: string, days = 30, limit = 100, category = "all", offset = 0) {
    return this.invoke<InboxFeedPayload>("list_cached_emails", { mailbox, days, limit, category, offset }, { timeoutMs: 30_000 });
  }

  listGmailEmailsPage(mailbox: string, days = 30, limit = 100, category = "all", pageToken = "", pageOffset = 0, excludeMessageIds: string[] = []) {
    return this.invoke<InboxFeedPayload>("list_gmail_emails_page", {
      mailbox,
      days,
      limit,
      category,
      page_token: pageToken,
      page_offset: pageOffset,
      exclude_message_ids: excludeMessageIds,
    }, { timeoutMs: 120_000 });
  }

  getInboxEmail(mailbox: string, messageId: string) {
    return this.invoke<InboxEmailDetailPayload>("get_cached_email", { mailbox, message_id: messageId }, { timeoutMs: 120_000 });
  }

  getInboxThreadPage(mailbox: string, threadId: string, options: {
    anchorMessageId?: string;
    beforeIndex?: number | null;
    limit?: number;
    includeDisplayBody?: boolean;
  } = {}) {
    return this.invoke<InboxThreadPagePayload>(
      "get_inbox_thread_page",
      {
        mailbox,
        thread_id: threadId,
        anchor_message_id: options.anchorMessageId,
        before_index: options.beforeIndex,
        limit: options.limit ?? 5,
        include_display_body: options.includeDisplayBody ?? true,
      },
      { timeoutMs: 120_000 },
    );
  }

  getInboxMessageDisplayBody(mailbox: string, messageId: string) {
    return this.invoke<InboxMessageDisplayBodyPayload>(
      "get_inbox_message_display_body",
      { mailbox, message_id: messageId },
      { timeoutMs: 120_000 },
    );
  }

  prepareInboxAttachmentAccess(mailbox: string, messageId: string, attachmentId: string, mode: "preview" | "download") {
    return this.invoke<AttachmentDownloadPayload>(
      "prepare_inbox_attachment_access",
      { mailbox, message_id: messageId, attachment_id: attachmentId, mode },
      { timeoutMs: 120_000 },
    );
  }

  startInboxThreadAssist(args: Record<string, unknown>) {
    return this.invoke<RunStatus>("start_inbox_thread_assist", args);
  }

  startInboxMailPrompt(args: Record<string, unknown>) {
    return this.invoke<RunStatus>("start_inbox_mail_prompt", args, { retry: "safe" });
  }

  startComposeMailPrompt(args: Record<string, unknown>) {
    return this.invoke<RunStatus>("start_compose_mail_prompt", args, { retry: "safe" });
  }

  getInboxThreadDraft(mailbox: string, threadId: string) {
    return this.invoke<InboxThreadDraftPayload>("get_inbox_thread_draft", { mailbox, thread_id: threadId });
  }

  listInboxThreadDrafts(mailbox: string, limit = 100) {
    return this.invoke<InboxFeedPayload>("list_inbox_thread_drafts", { mailbox, limit });
  }

  saveInboxThreadDraft(mailbox: string, threadId: string, body: string, ifMatch?: string, message?: Record<string, unknown>) {
    return this.invoke<{ ok?: boolean; etag?: string; updated?: boolean }>(
      "save_inbox_thread_draft",
      { mailbox, thread_id: threadId, body, if_match: ifMatch, message: message || {} },
    );
  }

  deleteInboxThreadDraft(mailbox: string, threadId: string) {
    return this.invoke<{ ok?: boolean }>("delete_inbox_thread_draft", { mailbox, thread_id: threadId });
  }

  resolveContactAvatars(mailbox: string, emails: string[]) {
    return this.invoke<ContactAvatarPayload>("resolve_contact_avatars", { mailbox, emails }, { timeoutMs: 120_000 });
  }

  searchComposeContacts(mailbox: string, query: string, limit = 10, storageProvider?: string) {
    return this.invoke<{ contacts?: ComposeContact[]; permission_required?: boolean }>("search_compose_contacts", { mailbox, query, limit, storage_provider: storageProvider });
  }

  listComposeDrafts(mailbox: string, limit = 100, storageProvider?: string) {
    return this.invoke<ComposeDraftListPayload>("list_compose_drafts", { mailbox, limit, storage_provider: storageProvider });
  }

  saveComposeDraft(mailbox: string, draft: Partial<ComposeDraft>, ifMatch?: string, storageProvider?: string) {
    return this.invoke<{ ok?: boolean; etag?: string; draft: ComposeDraft }>("create_or_update_compose_draft", { mailbox, draft, if_match: ifMatch, storage_provider: storageProvider });
  }

  deleteComposeDraft(mailbox: string, draftId: string, storageProvider?: string) {
    return this.invoke<{ ok?: boolean }>("delete_compose_draft", { mailbox, draft_id: draftId, storage_provider: storageProvider });
  }

  sendComposeEmails(mailbox: string, messages: ComposeDraft[], storageProvider?: string) {
    return this.invoke<{ ok?: boolean; results?: Array<{ id: string; ok: boolean; error?: string }> }>("send_compose_emails", { mailbox, messages, storage_provider: storageProvider });
  }

  setMessageStarred(mailbox: string, messageId: string, starred: boolean) {
    return this.invoke<{ ok?: boolean; error?: string; starred?: boolean }>("set_message_starred", { mailbox, message_id: messageId, starred });
  }

  modifyMessageLabels(mailbox: string, messageIds: string[], addLabelIds: string[] = [], removeLabelIds: string[] = []) {
    return this.invoke<{ ok?: boolean; error?: string; message_ids?: string[] }>(
      "modify_message_labels",
      { mailbox, message_ids: messageIds, add_label_ids: addLabelIds, remove_label_ids: removeLabelIds },
    );
  }

  updateInboxThreadState(mailbox: string, threadId: string, operation: string) {
    return this.invoke<{ ok?: boolean; error?: string; thread_id?: string; message_ids?: string[] }>(
      "update_inbox_thread_state",
      { mailbox, thread_id: threadId, operation },
    );
  }

  getThreadContextPage(mailbox: string, cardId: string, storageProvider: string, beforeIndex?: number | null, limit = 50) {
    return this.invoke<CardDetailPayload["thread_context"]>(
      "get_thread_context_page",
      { mailbox, card_id: cardId, storage_provider: storageProvider, before_index: beforeIndex, limit },
    );
  }

  prepareAttachmentDownload(mailbox: string, cardId: string, attachmentId: string, storageProvider: string) {
    return this.invoke<AttachmentDownloadPayload>(
      "prepare_attachment_download",
      { mailbox, card_id: cardId, attachment_id: attachmentId, storage_provider: storageProvider },
      { timeoutMs: 120_000 },
    );
  }

  listContactMemories(mailboxes: string[], storageProvider: string) {
    return this.invoke<{ contacts: ContactMemorySummary[]; count?: number; mailboxes?: string[] }>("list_contact_memories", { mailboxes, storage_provider: storageProvider });
  }

  getContactMemory(mailbox: string, contactEmail: string, storageProvider: string) {
    return this.invoke<ContactMemoryDetailPayload>("get_contact_memory", { mailbox, contact_email: contactEmail, storage_provider: storageProvider });
  }

  deleteContactMemory(mailbox: string, contactEmail: string, storageProvider: string) {
    return this.invoke<{ ok?: boolean; error?: string }>("delete_contact_memory", { mailbox, contact_email: contactEmail, storage_provider: storageProvider });
  }

  clearContactMemories(mailboxes: string[], storageProvider: string) {
    return this.invoke<{ ok?: boolean; deleted?: number; error?: string }>("clear_contact_memories", { mailboxes, storage_provider: storageProvider });
  }

  generateContactMemories(args: Record<string, unknown>) {
    return this.startContactMemoryRun(args);
  }

  startContactMemoryRun(args: Record<string, unknown>) {
    return this.invoke<RunStatus>("start_contact_memory_run", args);
  }

  continueContactMemoryRun(args: Record<string, unknown>) {
    return this.invoke<RunStatus>("continue_contact_memory_run", args, { timeoutMs: 55_000 });
  }

  startSummarizeThread(args: Record<string, unknown>) {
    return this.invoke<RunStatus>("start_summarize_thread", args);
  }

  startGenerateDraft(args: Record<string, unknown>) {
    return this.invoke<RunStatus>("start_generate_draft", args);
  }

  recordCardDecision(args: Record<string, unknown>) {
    return this.invoke<{ ok?: boolean }>("record_card_decision", args);
  }

  clearActiveCards(mailbox: string, storageProvider: string) {
    return this.invoke<{ ok?: boolean }>("clear_active_cards", { mailbox, storage_provider: storageProvider });
  }

  markCleanupRead(args: Record<string, unknown>) {
    return this.invoke<{ ok?: boolean; gmail_error?: string; gmail_code?: string; marked_count?: number }>("mark_cleanup_read", args);
  }

  markCardRead(args: Record<string, unknown>) {
    return this.invoke<{ ok?: boolean; gmail_error?: string; gmail_code?: string; marked_count?: number }>("mark_card_read", args);
  }

  restoreCard(mailbox: string, cardId: string, storageProvider: string) {
    return this.invoke<{ ok?: boolean }>("restore_card", { mailbox, card_id: cardId, storage_provider: storageProvider });
  }

  recordSnooze(args: Record<string, unknown>) {
    return this.invoke<{ ok?: boolean; snooze_until?: string }>("record_snooze", args);
  }

  replyNow(args: Record<string, unknown>) {
    return this.invoke<{ ok?: boolean; dry_run?: boolean; error?: string }>("reply_now", args);
  }

  loadCustomPlans(storageProvider: string) {
    return this.invoke<{ plans: CustomPlanSummary[] }>("get_custom_plans", { storage_provider: storageProvider });
  }

  deleteCustomPlan(planId: string, storageProvider: string) {
    return this.invoke<{ ok?: boolean }>("delete_custom_plan", { plan_id: planId, storage_provider: storageProvider });
  }

  startCustomScan(args: Record<string, unknown>) {
    return this.invoke<RunStatus>("start_custom_scan", args, { timeoutMs: CUSTOM_SCAN_INVOKE_TIMEOUT_MS, retry: "safe" });
  }

  reRunCustomScan(args: Record<string, unknown>) {
    return this.invoke<RunStatus>("re_run_custom_scan", args, { timeoutMs: CUSTOM_SCAN_INVOKE_TIMEOUT_MS });
  }

  /** 统一 AI 侧栏 turn：后端本地 Router + 白名单工具，可轮询 run。 */
  startAiTurn(args: Record<string, unknown>) {
    return this.invoke<RunStatus>("start_ai_turn", args, { timeoutMs: CUSTOM_SCAN_INVOKE_TIMEOUT_MS, retry: "safe" });
  }

  /** 用户确认整理建议后执行（非 Router 静默 mutation）。 */
  applyProposedActions(args: { action: string; items: Array<Record<string, unknown>> }) {
    return this.invoke<{
      success?: boolean;
      action?: string;
      results?: Array<Record<string, unknown>>;
      local_done?: Array<{ mailbox?: string; message_id?: string; thread_id?: string }>;
      errors?: string[];
      requires_local_done?: boolean;
      error?: string;
    }>("apply_proposed_actions", args);
  }

  listSavedPrompts() {
    return this.invoke<{ success?: boolean; prompts?: Array<Record<string, unknown>>; etag?: string }>("list_saved_prompts", {});
  }

  saveSavedPrompt(args: { prompt_id?: string; title?: string; body: string }) {
    return this.invoke<{ success?: boolean; prompt?: Record<string, unknown>; error?: string }>("save_saved_prompt", args);
  }

  deleteSavedPrompt(promptId: string) {
    return this.invoke<{ success?: boolean; deleted?: number }>("delete_saved_prompt", { prompt_id: promptId });
  }

  listAiMemories() {
    return this.invoke<{ success?: boolean; memories?: Array<Record<string, unknown>>; etag?: string }>("list_ai_memories", {});
  }

  addAiMemory(text: string, source = "settings") {
    return this.invoke<{ success?: boolean; memory?: Record<string, unknown>; error?: string }>("add_ai_memory", { text, source });
  }

  deleteAiMemory(memoryId: string) {
    return this.invoke<{ success?: boolean; deleted?: number }>("delete_ai_memory", { memory_id: memoryId });
  }

  clearCards(mailbox: string, category: string) {
    return this.invoke<{ ok: boolean; removed: number }>("clear_cards", { mailbox, category });
  }

  clearHistory() {
    return this.invoke<{ ok: boolean }>("clear_history", {});
  }

  resetAllData() {
    return this.invoke<{ ok: boolean }>("reset_all_data", {});
  }

  resetMailboxScanHistory(mailbox: string, storageProvider: string) {
    return this.invoke<{ ok: boolean; deleted: Record<string, number> }>("reset_mailbox_scan_history", { mailbox, storage_provider: storageProvider });
  }

  deleteMailboxData(mailbox: string) {
    return this.invoke<{ ok: boolean; deleted: Record<string, number> }>("delete_mailbox_data", { mailbox });
  }

  markReadFromAsk(mailbox: string, messageIds: string[]) {
    return this.invoke<{ ok?: boolean; error?: string }>("mark_read_from_ask", { mailbox, message_ids: messageIds });
  }

  trashFromAsk(mailbox: string, messageIds: string[]) {
    return this.invoke<{ ok?: boolean; error?: string }>("trash_from_ask", { mailbox, message_ids: messageIds });
  }

  replyFromAsk(args: Record<string, unknown>) {
    return this.invoke<{ ok?: boolean; dry_run?: boolean; error?: string }>("reply_from_ask", args);
  }

  generateAskDraft(args: Record<string, unknown>) {
    return this.invoke<{ subject?: string; body?: string; fallback_used?: boolean; note?: string }>("generate_ask_draft", args);
  }
}
