import type {
  ActiveCardsPayload,
  CardDetailPayload,
  ContactMemoryDetailPayload,
  ContactMemorySummary,
  CustomPlanSummary,
  MailboxInfo,
  RunHistoryEntry,
  RunStatus,
  RuntimeState,
  ScanPlan,

} from "../types/mail";
import appManifest from "../../manifest.json";

function getRequiredExecutaToolId(): string {
  const toolId = appManifest.required_executas[0]?.tool_id;
  if (!toolId) {
    throw new Error("anna-inbox/manifest.json is missing required_executas[0].tool_id");
  }
  return toolId;
}

const TOOL_ID = getRequiredExecutaToolId();
const INVOKE_TIMEOUT_MS = 180000;

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

  async invoke<T = unknown>(method: string, args: Record<string, unknown> = {}, options: { timeoutMs?: number } = {}): Promise<T> {
    const runtime = await this.getRuntime();
    if (!runtime.connected || !runtime.client) {
      throw new Error(runtime.error || "Anna runtime is not connected.");
    }
    const timeoutMs = options.timeoutMs || INVOKE_TIMEOUT_MS;
    const invokeArgs = {
      tool_id: TOOL_ID,
      method,
      args,
    };
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
      throw new Error(`[tool:${method}] ${code}${message}${traceback ? `\n\n${traceback}` : ""}`);
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

  listMailboxes(storageProvider: string) {
    return this.invoke<{ mailboxes: MailboxInfo[]; selected: string[]; discovered?: MailboxInfo[] }>("list_mailboxes", { storage_provider: storageProvider });
  }

  setMailboxSelected(mailbox: string, selected: boolean, storageProvider: string) {
    return this.invoke<{ ok?: boolean; mailboxes: MailboxInfo[]; selected: string[] }>("set_mailbox_selected", { mailbox, selected, storage_provider: storageProvider });
  }

  removeMailbox(mailbox: string, storageProvider: string) {
    return this.invoke<{ ok?: boolean; mailboxes: MailboxInfo[]; selected: string[] }>("remove_mailbox", { mailbox, storage_provider: storageProvider });
  }

  loadActiveCards(mailbox: string, storageProvider: string, offset = 0, limit = 50, includeCleanup = false, cleanupOffset = 0, cleanupLimit = 100) {
    return this.invoke<ActiveCardsPayload>("get_active_cards", { mailbox, storage_provider: storageProvider, offset, limit, include_cleanup: includeCleanup, cleanup_offset: cleanupOffset, cleanup_limit: cleanupLimit });
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
    return this.invoke<RunStatus>("continue_mail_agent_run", args, { timeoutMs: 70_000 });
  }

  getRun(runId: string) {
    return this.invoke<RunStatus>("get_mail_agent_run", { run_id: runId });
  }

  getCardDetail(mailbox: string, cardId: string, storageProvider: string) {
    return this.invoke<CardDetailPayload>("get_card_detail", { mailbox, card_id: cardId, storage_provider: storageProvider });
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
    return this.invoke<{ ok?: boolean; backfilled?: number; skipped_old?: number; error?: string }>("generate_contact_memories", args, { timeoutMs: 600_000 });
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
    return this.invoke<{ ok?: boolean; gmail_error?: string }>("mark_cleanup_read", args);
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
    return this.invoke<RunStatus>("start_custom_scan", args, { timeoutMs: 600_000 });
  }

  reRunCustomScan(args: Record<string, unknown>) {
    return this.invoke<RunStatus>("re_run_custom_scan", args, { timeoutMs: 600_000 });
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
