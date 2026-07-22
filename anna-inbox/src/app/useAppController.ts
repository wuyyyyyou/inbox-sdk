import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelAiAgentTurn,
  clearAiAgentSession,
  runAiAgentTurn,
  stripTerminalDoneMarker,
  type AgentToolOutcome,
} from "../api/agentSessionClient";
import { MailAgentClient } from "../api/mailAgentClient";
import { makeCustomRunProgress, scanProgressLabel, scanStageLabel, stageToStep } from "../features/brief/runHelpers";
import { buildDraftPreferencesInstruction, resolveDraftPreferences } from "../features/handle/draftPreferences";
import {
  mergeInboxMessagesById,
  pruneInboxMessagesToCacheWindow,
  sortInboxMessagesDesc,
} from "../features/home/inboxMessageOrder";
import { clampInboxSettings } from "../features/settings/inboxSettings";
import { connectRuntime } from "../runtime/runtimeLoader";
import { triggerBrowserDownload } from "../shared/browserDownload";
import {
  cacheMailboxes,
  clearMailboxCacheData,
  clearMailboxDatabase,
  getContactAvatarCache,
  migrateSelectedMailboxFromLocalStorage,
  setSelectedMailbox,
  setContactAvatarCache,
} from "../shared/browserStorage";
import { senderParts, splitAddresses } from "../shared/mailIdentity";
import type {
  ActiveCardsPayload,
  AiClarificationPayload,
  AiChatMessage,
  AiInboxListContext,
  AiMailContextRef,
  AppState,
  AskHistoryEntry,
  AttachmentDownloadPayload,
  CleanupMessage,
  ComposeContact,
  ComposeDraft,
  ComposeDraftListPayload,
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
  InboxSettings,
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
  DEFAULT_MODE,
  MAILBOX_STORAGE_KEY,
  POLL_INTERVAL_MS,
  POLL_LIMIT,
  requestForMode,
} from "./constants";
import { createInitialState, removeAskHistoryEntry } from "./state";
import { buildRevisionPrompt } from "./aiRoute";
import { connectedAccountsStatusMessage } from "./connectedAccounts";
import { resolveMailboxSelection } from "./mailboxSelection";

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
  const scanQuery = String(result.scan_query || "").trim()
    || (Array.isArray(result.plan_gmail_queries) && result.plan_gmail_queries[0]
      ? String((result.plan_gmail_queries[0] as { query?: string }).query || "").trim()
      : "");
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
    sampling: result.sampling && typeof result.sampling === "object"
      ? result.sampling as import("../types/mail").SamplingUsageSummary
      : undefined,
    trace: (result.trace as Record<string, unknown>) || {},
    planner_fallback: Boolean(planner?.fallback_used),
    scan_query: scanQuery || undefined,
    scan_source: scanQuery ? String(result.scan_source || "cache") : undefined,
  };
}

function payloadScanQuery(payload: Record<string, unknown>): { scanQuery?: string; scanSource?: string } {
  const direct = String(payload.scan_query || "").trim();
  if (direct) {
    return { scanQuery: direct, scanSource: String(payload.scan_source || "cache") };
  }
  const queries = payload.plan_gmail_queries;
  if (Array.isArray(queries) && queries[0] && typeof queries[0] === "object") {
    const query = String((queries[0] as { query?: string }).query || "").trim();
    if (query) return { scanQuery: query, scanSource: "cache" };
  }
  return {};
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

function buildAiTurnUiContext(args: {
  mailbox: string;
  selectedMailboxes: string[];
  conversationId: string;
  scanPlan: ScanPlan | null | undefined;
  displayRangeDays?: number;
  currentMailContext?: SendAiMessageOptions["currentMailContext"];
  languageHint?: string;
  messages?: AiChatMessage[];
  savedPromptId?: string;
  selectedThreads?: SendAiMessageOptions["selectedThreads"];
  routingIntent?: SendAiMessageOptions["routingIntent"];
  inboxListContext?: AiInboxListContext;
}) {
  const mailbox = selectedOrPrimary(args.selectedMailboxes, args.mailbox);
  const plan = normalizeScanPlan(args.scanPlan);
  const rangeDays = clampInt(args.displayRangeDays, plan.scan_window_days, 1, 90);
  const ctx = args.currentMailContext;
  const thread = ctx && ctx.kind === "gmail_thread"
    ? {
        kind: "thread" as const,
        mailbox: ctx.mailbox || mailbox,
        message_id: ctx.latest_message_id || ctx.anchor_message_id || "",
        thread_id: ctx.thread_id || "",
        subject: "",
        snippet: "",
      }
    : ctx && ctx.kind === "compose"
      ? {
          kind: "compose" as const,
          mailbox: ctx.mailbox || mailbox,
          message_id: "",
          thread_id: "",
          subject: ctx.subject || "",
          snippet: String(ctx.body || "").slice(0, 240),
        }
      : { kind: "none" as const, mailbox, message_id: "", thread_id: "", subject: "", snippet: "" };
  // 多轮改写：注入上一轮 draft 或 Compose 正文
  let lastDraft: { body: string; source: string } | undefined;
  if (ctx && ctx.kind === "compose" && String(ctx.body || "").trim()) {
    lastDraft = { body: String(ctx.body).slice(0, 8000), source: "compose_box" };
  } else {
    const recent = [...(args.messages || [])].reverse().find(
      (message) => message.role === "assistant" && message.artifact && String((message.artifact as { body?: string }).body || "").trim(),
    );
    if (recent?.artifact) {
      const body = String((recent.artifact as { body?: string }).body || "").slice(0, 8000);
      if (body) lastDraft = { body, source: "assistant_artifact" };
    }
  }
  // 收件箱多选：上限 20，与设计文档一致；后端 batch 再截到 5
  const selectedThreads = (args.selectedThreads || [])
    .filter((item) => item && (item.message_id || item.thread_id) && item.mailbox)
    .slice(0, 20)
    .map((item) => ({
      mailbox: String(item.mailbox || mailbox),
      message_id: String(item.message_id || ""),
      thread_id: String(item.thread_id || item.message_id || ""),
      subject: String(item.subject || "").slice(0, 200),
    }));
  const listContext = args.inboxListContext;
  return {
    conversation_id: args.conversationId,
    mailbox,
    selected_mailboxes: selectableMailboxes(args.selectedMailboxes, args.mailbox),
    display_range_days: rangeDays,
    max_messages: plan.max_messages,
    screen: {
      view: ctx?.kind === "compose" ? "compose" : ctx?.kind === "gmail_thread" ? "thread" : "inbox",
      focus: ctx ? "detail" : "list",
    },
    current_thread: thread,
    selected_threads: selectedThreads,
    last_draft: lastDraft,
    saved_prompt_id: args.savedPromptId || "",
    language_hint: args.languageHint || "",
    routing_intent: args.routingIntent || "",
    // 工作流状态只传 message_id，不传邮件正文；详情/旧入口未给列表快照时保留 Todo 兼容读取。
    todo_message_ids: listContext?.todo_message_ids || readTodoMessageIds(mailbox),
    done_message_ids: listContext?.done_message_ids || [],
    snoozed_message_ids: listContext?.snoozed_message_ids || [],
    mailbox_view: listContext?.mailbox_view || "",
    inbox_group: listContext?.inbox_group || "",
    search_input: listContext?.search_input || "",
    active_search: listContext?.active_search || "",
    custom_category: listContext?.custom_category,
  };
}

function buildAiAgentContent(userText: string, uiContext: Record<string, unknown>): string {
  // ui_context 是用户输入中的只读事实，不能覆盖 session 的 systemPrompt。
  return `[ui_context]\n${JSON.stringify(uiContext)}\n\n[user]\n${userText}`;
}

function agentOutcomePayload(outcomes: AgentToolOutcome[], finalText: string): Record<string, unknown> {
  const latest = [...outcomes].reverse().find((outcome) => typeof outcome.kind === "string") || {};
  // 仅当本轮真实跑过 search 时带上 scan_query（供 Thinking 后小字 chip）。
  let scanQuery = "";
  let scanSource = "";
  for (const outcome of [...outcomes].reverse()) {
    const query = String(outcome.scan_query || outcome.query || "").trim();
    if (query) {
      scanQuery = query;
      scanSource = String(outcome.scan_source || "cache");
      break;
    }
    const nested = outcome.scan_result;
    if (nested && typeof nested === "object") {
      const nestedQuery = String((nested as Record<string, unknown>).scan_query || "").trim();
      if (nestedQuery) {
        scanQuery = nestedQuery;
        scanSource = String((nested as Record<string, unknown>).scan_source || "cache");
        break;
      }
    }
    // start_ai_turn 完成态：result 内带 scan_query
    const result = outcome.result;
    if (result && typeof result === "object") {
      const fromResult = String((result as Record<string, unknown>).scan_query || "").trim();
      if (fromResult) {
        scanQuery = fromResult;
        scanSource = String((result as Record<string, unknown>).scan_source || "cache");
        break;
      }
    }
    const progress = outcome.progress;
    if (progress && typeof progress === "object") {
      const fromProgress = String((progress as Record<string, unknown>).scan_query || "").trim();
      if (fromProgress) {
        scanQuery = fromProgress;
        scanSource = "cache";
        break;
      }
    }
  }
  // 若 Host 误调 start_ai_turn 且已完成，优先用 run result 的 kind/文案
  const finishedRun = [...outcomes].reverse().find((outcome) => {
    const status = String(outcome.status || "");
    return status === "done" && outcome.result && typeof outcome.result === "object";
  });
  const runResult = finishedRun?.result && typeof finishedRun.result === "object"
    ? finishedRun.result as Record<string, unknown>
    : null;
  const kind = String(
    runResult?.kind
    || latest.kind
    || (scanQuery ? "chat" : "chat"),
  );
  const assistantFromRun = stripTerminalDoneMarker(
    String(runResult?.assistant_text || runResult?.summary || "").trim(),
  );
  return {
    ...latest,
    ...(runResult || {}),
    kind,
    assistant_text: finalText || assistantFromRun || String(latest.assistant_text || ""),
    ...(scanQuery ? { scan_query: scanQuery, scan_source: scanSource || "cache" } : {}),
  };
}

/** 从 Host tool 结果中提取 start_ai_turn / custom scan 的 run_id。 */
function findBackgroundRunId(outcomes: AgentToolOutcome[]): string {
  for (const outcome of [...outcomes].reverse()) {
    const runId = String(outcome.run_id || "").trim();
    if (runId.startsWith("at_") || runId.length >= 8) return runId;
  }
  return "";
}

/** 从本地 mail-flags 读取 todos id，供后端 is:todo cache 过滤。 */
function readTodoMessageIds(mailbox: string): string[] {
  if (typeof window === "undefined" || !mailbox) return [];
  try {
    const raw = window.localStorage.getItem(`anna-inbox:mail-flags:${mailbox}`);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as { todos?: unknown };
    if (!Array.isArray(parsed?.todos)) return [];
    return parsed.todos.map(String).filter(Boolean).slice(0, 500);
  } catch {
    return [];
  }
}

function isTransientConnectionError(error: unknown) {
  const raw = error instanceof Error ? error.message : String(error);
  return /unexpected token\s+['"]?<|<!doctype html|text\/html|failed to fetch|network(?:error| request)?|fetch failed|econnreset|enotfound|etimedout|timeout|\b5\d\d\b|\[tool_failed\]|executa process exited/i.test(raw);
}

function patchInboxThreadAttachmentSummary(
  messages: InboxFeedPayload["messages"],
  mailbox: string,
  threadId: string,
  attachmentCount: number,
) {
  const normalized = normalizedMailbox(mailbox);
  const count = Math.max(0, Math.floor(Number(attachmentCount) || 0));
  if (!normalized || !threadId || !count) return messages;
  return messages.map((message) => {
    if (normalizedMailbox(message.mailbox || normalized) !== normalized || message.thread_id !== threadId) return message;
    const nextCount = Math.max(Number(message.attachment_count || 0), count);
    if (message.has_attachment && Number(message.attachment_count || 0) === nextCount) return message;
    return { ...message, has_attachment: true, attachment_count: nextCount };
  });
}

function formatRunDiagnostics(status: RunStatus, message: string): string {
  const diagnostic = status.diagnostics;
  if (!diagnostic?.trace_id) return message;
  const spans = diagnostic.spans.slice(-12).map((span) => {
    const error = span.outcome === "error" && span.error_type ? ` ${span.error_type}` : "";
    return `${span.stage} ${span.elapsed_ms}ms${error}`;
  });
  return [
    message,
    `Diagnostic ${diagnostic.trace_id} (${diagnostic.elapsed_ms}ms)`,
    ...spans,
  ].join("\n");
}

function safeDiagnosticsFromError(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  const lines = raw.split("\n");
  const start = lines.findIndex((line) => /^Diagnostic rt_[a-f0-9]{12} \(\d+ms\)$/.test(line.trim()));
  if (start < 0) return "";
  const safe = [lines[start].trim()];
  for (const line of lines.slice(start + 1, start + 13)) {
    const normalized = line.trim();
    if (!/^[a-z_.]+ \d+ms(?: [A-Za-z][A-Za-z0-9_.-]{0,79})?$/.test(normalized)) break;
    safe.push(normalized);
  }
  return safe.join("\n");
}

function sanitizeToolError(error: unknown, input: string) {
  const raw = error instanceof Error ? error.message : String(error);
  // 后端以稳定错误码标识 Answer 模型未能生成有效分析。该码不直接展示，
  // 侧栏改为可操作的用户文案，并沿用下方诊断信息的安全过滤规则。
  const analysisUnavailable = /\banalysis_unavailable\b/i.test(raw);
  const unavailable = prefersChinese(input)
    ? "Anna 暂时无法完成这项邮箱任务，请稍后重试。"
    : "Anna couldn't complete that inbox task right now. Please try again shortly.";
  const message = analysisUnavailable
    ? prefersChinese(input)
      ? "AI 分析暂时不可用，请稍后重试。"
      : "AI analysis is temporarily unavailable. Please try again."
    : isTransientConnectionError(error)
    ? prefersChinese(input)
      ? "连接 Anna 服务时出现问题。我已经自动重试；请稍后再试。"
      : "There was a problem connecting to Anna. I retried automatically; please try again shortly."
    : unavailable;
  // 工具和后台任务的原始错误可能含协议、供应商或 HTML 文本，不能直接展示。
  // 仅保留本地按严格格式生成的时序行，供平台问题复现时复制到报告。
  const diagnostics = safeDiagnosticsFromError(error);
  return diagnostics ? `${message}\n\n${diagnostics}` : message;
}

function pruneAskHistoryEntries(history: AskHistoryEntry[], nowMs = Date.now()): AskHistoryEntry[] {
  const cutoff = nowMs - 7 * 24 * 60 * 60 * 1000;
  return history
    .filter((entry) => {
      const ts = Date.parse(String(entry?.timestamp || ""));
      return Number.isFinite(ts) && ts >= cutoff;
    })
    .slice(0, 30);
}

function persistAskHistory(history: AskHistoryEntry[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      AI_ASK_HISTORY_STORAGE_KEY,
      JSON.stringify(pruneAskHistoryEntries(history)),
    );
  } catch {
    // localStorage 写入失败不影响主流程，最多只是刷新后不能恢复侧栏对话。
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
  // Ask 扫描可能超过单次工具调用预算；前端用 run_id 轮询，避免长时间阻塞导致 Executa 被 host 杀掉。
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
  durationMs?: number;
  actionLabel?: string;
  onAction?: () => void;
  secondaryActionLabel?: string;
  onSecondaryAction?: () => void;
};

type BatchInboxActionResult = {
  ok: boolean;
  count: number;
  /** 撤销回调仅在批量操作成功后提供；调用方负责同步本地工作流状态。 */
  undo?: () => Promise<boolean>;
};

type InboxPageResult = { ok: boolean; count: number; hasMore: boolean; nextOffset: number; messages?: InboxMessage[] };
type GmailInboxPageResult = { ok: boolean; count: number; hasMore: boolean; pageToken: string; pageOffset: number; messages?: InboxMessage[] };

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
  setMailDetailOpen(open: boolean, messageId?: string): void;
  setProvider(kind: "llm" | "storage", value: string): void;
  setDrawer(drawer: "sources" | "history" | "memory" | "scanPlan", open: boolean): void;
  minimize(value: boolean): void;
  openSettings(focusSavedPrompts?: boolean): void;
  closeSettings(): void;
  loadInboxSettings(mailbox?: string): Promise<InboxSettings | null>;
  saveInboxSettings(patch: Partial<InboxSettings>): Promise<boolean>;
  checkGmailAuth(mailboxOverride?: string): Promise<{ authorized: boolean; source: string }>;
  checkAnyGmailAuth(): Promise<{ authorized: boolean; source: string }>;
  closeGmailErrorPopup(): void;
  loadMailboxes(): Promise<{ mailboxes: MailboxInfo[]; selected: string[]; primary: string }>;
  switchMailbox(mailbox: string): Promise<void>;
  setBriefMailboxFilter(mailboxes: string[]): void;
  loadActiveCards(): Promise<void>;
  loadInboxEmails(category?: string, days?: number, force?: boolean): Promise<boolean>;
  refreshInboxEmails(category?: string, days?: number, clearCache?: boolean): Promise<InboxPageResult>;
  /** 与自动同步相同：History 增量 + 静默合并快照，不整表清空 */
  silentSyncInbox(days?: number): Promise<boolean>;
  /** 设置页：清空本地缓存并硬重载当前邮箱 */
  clearInboxCacheAndReload(days?: number): Promise<boolean>;
  /** 扩大 All mail 时间窗并只追加新邮件，不清空已有快照 */
  expandInboxFeedWindow(days: number): Promise<InboxPageResult>;
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
  saveInboxThreadDraft(
    mailbox: string,
    threadId: string,
    body: string,
    bodyHtml?: string,
    ifMatch?: string,
    message?: Record<string, unknown>,
    attachments?: Array<Record<string, unknown>>,
  ): Promise<{ ok?: boolean; etag?: string; updated?: boolean }>;
  deleteInboxThreadDraft(mailbox: string, threadId: string): Promise<{ ok?: boolean }>;
  searchComposeContacts(mailbox: string, query: string): Promise<{ contacts: ComposeContact[]; permissionRequired: boolean }>;
  listComposeDrafts(mailbox: string): Promise<ComposeDraftListPayload>;
  saveComposeDraft(mailbox: string, draft: Partial<ComposeDraft>, ifMatch?: string): Promise<ComposeDraft>;
  deleteComposeDraft(mailbox: string, draftId: string): Promise<void>;
  sendComposeEmails(mailbox: string, messages: ComposeDraft[]): Promise<Array<{ id: string; ok: boolean; error?: string }>>;
  prepareInboxAttachmentAccess(mailbox: string, messageId: string, attachmentId: string, mode: "preview" | "download"): Promise<AttachmentDownloadPayload>;
  beginStageOutgoingAttachment(
    mailbox: string,
    args: {
      filename: string;
      mime_type?: string;
      size: number;
      existing_total_bytes?: number;
      draft_scope?: string;
      draft_key?: string;
    },
  ): Promise<{
    ok?: boolean;
    error?: string;
    attachment_id?: string;
    filename?: string;
    mime_type?: string;
    size?: number;
    storage_key?: string;
    upload_url?: string;
    upload_headers?: Record<string, string>;
  }>;
  completeStageOutgoingAttachment(mailbox: string, storageKey: string, size: number, mimeType?: string): Promise<{ ok?: boolean; storage_key?: string }>;
  deleteStagedOutgoingAttachment(mailbox: string, storageKey: string): Promise<void>;
  prepareStagedOutgoingAttachmentAccess(
    mailbox: string,
    storageKey: string,
    filename?: string,
    mimeType?: string,
  ): Promise<AttachmentDownloadPayload>;
  modifyInboxMessageLabels(mailbox: string, messageIds: string[], addLabelIds?: string[], removeLabelIds?: string[]): Promise<void>;
  updateInboxThreadState(mailbox: string, threadId: string, operation: InboxThreadStateOperation): Promise<void>;
  submitMailContextPrompt(request: SubmitMailPromptRequest): Promise<MailPromptRunResult | null>;
  sendInboxThreadReply(args: {
    mailbox: string;
    threadId: string;
    to: string;
    body: string;
    bodyHtml?: string;
    cc?: string[];
    bcc?: string[];
    replyMode?: string;
    dryRun?: boolean;
    attachments?: import("../types/mail").OutgoingAttachmentMeta[];
  }): Promise<{ ok?: boolean; dry_run?: boolean; error?: string }>;
  loadContactAvatars(emails: string[], mailbox?: string): Promise<{
    avatars: Record<string, string>;
    permissionRequired: boolean;
    serviceDisabled: boolean;
    activationUrl: string;
  }>;
  setInboxStarred(messageId: string, starred: boolean): Promise<void>;
  markInboxRead(messageId: string): Promise<void>;
  trashInboxMessage(messageId: string): Promise<void>;
  /** 收件箱多选批量操作（已读/未读/星标/归档/垃圾箱/Done） */
  batchInboxActions(
    messageIds: string[],
    action: "mark_read" | "mark_unread" | "star" | "unstar" | "archive" | "trash" | "mark_done",
  ): Promise<BatchInboxActionResult>;
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
  retryAiMessage(messageId: string): void;
  dismissAiClarification(messageId: string): void;
  resolveAiClarification(messageId: string, actionId: string): void;
  stopAiGeneration(): void;
  startNewAiConversation(): void;
  openAiConversation(index: number): void;
  resumeAiConversation(index: number): void;
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
  /** 用户确认整理建议后执行 mutation + 返回 local_done 供 UI 写 Done */
  applyProposedActions(args: {
    action: string;
    items: Array<{ mailbox: string; message_id: string; thread_id: string; subject?: string }>;
  }): Promise<{ local_done?: Array<{ mailbox?: string; message_id?: string; thread_id?: string }>; success?: boolean }>;
  listSavedPrompts(): Promise<Array<{ id: string; title: string; body: string }>>;
  saveSavedPrompt(args: { prompt_id?: string; title?: string; body: string }): Promise<boolean>;
  deleteSavedPrompt(promptId: string): Promise<boolean>;
  listAiMemories(): Promise<Array<{ id: string; text: string }>>;
  addAiMemory(text: string): Promise<boolean>;
  deleteAiMemory(memoryId: string): Promise<boolean>;
  /** 探测 Anna LLM 连通性与延迟；侧栏状态点击 / Toast Retry 可触发 */
  refreshSamplingStatus(options?: { fromPoll?: boolean }): Promise<AppState["llmStatus"]>;
  /** 探测 Gmail API 连通性与延迟；与 LLM 探测并行 */
  refreshGmailApiStatus(options?: { fromPoll?: boolean }): Promise<AppState["gmailApiStatus"]>;
  /** 并行刷新 LLM + Gmail API 状态 */
  refreshConnectivityStatus(options?: { fromPoll?: boolean }): Promise<void>;
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
  const draftRequestSequence = useRef(0);
  const snapshotRequestMailbox = useRef("");
  const snapshotPromise = useRef<Promise<boolean> | null>(null);
  const runtimeReconnectPromise = useRef<Promise<AppState["runtime"]> | null>(null);
  const inboxAutoSyncTimer = useRef<number | null>(null);
  const inboxAutoSyncInFlight = useRef(false);
  const inboxAutoSyncPending = useRef<{ mailbox: string; days: number } | null>(null);
  /** 扫描/Ask 进行中切邮箱时，延后 live Gmail 拉取，避免与扫描抢 Host getToken。 */
  const gmailBusyRef = useRef(false);
  const deferredInboxLoadTimer = useRef<number | null>(null);
  /** Brief 扫描代际号：切换邮箱时 +1，continue 循环立刻退出。 */
  const briefScanGenerationRef = useRef(0);
  const briefScanRunIdRef = useRef("");
  /** Ask/自定义扫描的 run_id，切换邮箱时一并取消。 */
  const activeBackgroundRunIdRef = useRef("");

  const getRuntime = useCallback(async () => {
    if (!runtimePromise.current) {
      runtimePromise.current = connectRuntime();
    }
    const currentPromise = runtimePromise.current;
    const runtime = await currentPromise;
    if (!runtime.connected && runtimePromise.current === currentPromise) {
      runtimePromise.current = null;
    }
    return runtime;
  }, []);

  const reconnectRuntime = useCallback(async () => {
    if (runtimeReconnectPromise.current) return runtimeReconnectPromise.current;
    const reconnect = (async () => {
      const previous = runtimePromise.current ? await runtimePromise.current.catch(() => undefined) : undefined;
      previous?.client?.dispose?.();
      runtimePromise.current = null;
      const runtime = await getRuntime();
      setState((current) => ({ ...current, runtime }));
      return runtime;
    })();
    runtimeReconnectPromise.current = reconnect;
    try {
      return await reconnect;
    } finally {
      if (runtimeReconnectPromise.current === reconnect) runtimeReconnectPromise.current = null;
    }
  }, [getRuntime]);

  const client = useMemo(() => new MailAgentClient(getRuntime, reconnectRuntime), [getRuntime, reconnectRuntime]);

  const showToast = useCallback((message: string, options?: ToastOptions) => {
    setToast({ message, ...options });
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), options?.durationMs ?? 3200);
  }, []);

  const dismissToast = useCallback(() => {
    if (toastTimer.current) {
      window.clearTimeout(toastTimer.current);
      toastTimer.current = null;
    }
    setToast(null);
  }, []);

  // All mail 混排后 INBOX 占比可能偏低；刷新/预热多拉一些进缓存，投影后 Inbox 才够用
  const ALL_MAIL_CACHE_FETCH_LIMIT = 400;

  const applyInboxSnapshotPayload = useCallback((payload: InboxFeedPayload, options: {
    error?: string;
    append?: boolean;
    /** 静默刷新：按 id 合并并保留未变对象引用，避免整表抖动 */
    merge?: boolean;
    /** 合并后按 keepIds 裁剪当前同步窗口内已删除邮件 */
    keepIds?: Set<string>;
    pruneDays?: number;
  } = {}) => {
    const pageMessages = Array.isArray(payload.messages) ? payload.messages : [];
    setState((s) => {
      let snapshotMessages: InboxMessage[];
      if (options.append || options.merge) {
        snapshotMessages = mergeInboxMessagesById(s.inboxSnapshotMessages, pageMessages);
      } else {
        snapshotMessages = sortInboxMessagesDesc(pageMessages);
      }
      if (options.keepIds) {
        // 详情页打开的邮件不得被 soft prune 清掉，否则会退出详情/附件预览
        const keepIds = new Set(options.keepIds);
        if (s.mailDetailMessageId) keepIds.add(s.mailDetailMessageId);
        snapshotMessages = pruneInboxMessagesToCacheWindow(
          snapshotMessages,
          keepIds,
          options.pruneDays ?? 0,
        );
      }
      return {
        ...s,
        // 标签大小写兼容，避免漏掉 INBOX 导致 Inbox 列表异常偏少
        inboxMessages: snapshotMessages.filter((message) =>
          (message.label_ids || []).some((label) => String(label).toUpperCase() === "INBOX"),
        ),
        inboxSnapshotMessages: snapshotMessages,
        inboxUpdatedAt: String(payload.updated_at || s.inboxUpdatedAt || ""),
        inboxLoading: false,
        inboxError: options.error || "",
        inboxSnapshotComplete: true,
      };
    });
  }, []);

  const loadInboxThreadDrafts = useCallback(async (mailbox: string, limit = 100) => {
    const normalized = normalizedMailbox(mailbox);
    if (!normalized || normalized === "all") {
      return { mailbox: normalized, messages: [] } as InboxFeedPayload;
    }
    const requestId = ++draftRequestSequence.current;
    const payload = await client.listInboxThreadDrafts(normalized, limit);
    if (requestId !== draftRequestSequence.current) return payload;
    setState((s) => {
      const currentMailbox = normalizedMailbox(s.selectedMailboxes[0] || s.mailbox);
      if (currentMailbox !== normalized) return s;
      return {
        ...s,
        inboxDraftMessages: Array.isArray(payload.messages) ? payload.messages : [],
        inboxUpdatedAt: String(payload.updated_at || s.inboxUpdatedAt || ""),
      };
    });
    return payload;
  }, [client]);

  // 刷新邮件缓存后预热当前快照中的联系人头像，避免详情页再次访问 Gmail。
  const preloadContactAvatars = useCallback(async (mailbox: string, messages: InboxMessage[]) => {
    const normalized = normalizedMailbox(mailbox);
    if (!normalized || normalized === "all" || !messages.length) return;
    const legacyKey = `anna-inbox:contact-avatars:${normalized}`;
    const cached = await getContactAvatarCache(normalized, legacyKey);
    const known = cached?.avatars && typeof cached.avatars === "object" ? cached.avatars : {};
    const missing = new Set(Array.isArray(cached?.missing) ? cached.missing : []);
    const emails = [...new Set(messages.flatMap((message) =>
      [message.from, message.to]
        .flatMap(splitAddresses)
        .map((value) => senderParts(value).email.trim().toLowerCase())
        .filter((email) => email.includes("@")),
    ))].filter((email) => !known[email] && !missing.has(email)).slice(0, 200);
    if (!emails.length) return;
    try {
      const result = await client.resolveContactAvatars(normalized, emails);
      const avatars = result.avatars && typeof result.avatars === "object" ? result.avatars : {};
      const unresolved = result.permission_required || result.service_disabled
        ? []
        : emails.filter((email) => !avatars[email]);
      await setContactAvatarCache(normalized, {
        avatars: { ...known, ...avatars },
        missing: [...missing, ...unresolved],
        avatarsUpdatedAt: Date.now(),
        missingUpdatedAt: Date.now(),
      });
    } catch {
      // 头像预热失败不能阻断邮件缓存刷新，详情页仍可按需重试。
    }
  }, [client]);

  /**
   * 从本地 All mail 缓存（必要时回源 Gmail）加载快照。
   * soft=true：合并进现有列表并在结束后裁剪窗口内已删除项，不整表清空。
   */
  const loadMailboxSnapshotFromCache = useCallback(async (
    mailbox: string,
    days: number,
    requestKey: string,
    options: { soft?: boolean; skipLiveGmail?: boolean } = {},
  ) => {
    const soft = Boolean(options.soft);
    const skipLiveGmail = Boolean(options.skipLiveGmail);
    const keepIds = soft ? new Set<string>() : null;
    const collectedMessages: InboxMessage[] = [];
    const noteIds = (messages: InboxMessage[]) => {
      if (!keepIds) return;
      for (const message of messages) {
        if (message.id) keepIds.add(message.id);
      }
    };
    const applyPage = (payload: InboxFeedPayload, firstPage: boolean) => {
      if (Array.isArray(payload.messages)) collectedMessages.push(...payload.messages);
      if (soft) {
        noteIds(Array.isArray(payload.messages) ? payload.messages : []);
        applyInboxSnapshotPayload(payload, { merge: true });
        return;
      }
      applyInboxSnapshotPayload(payload, firstPage ? {} : { append: true });
    };
    const finishSoftPrune = () => {
      if (!keepIds) return;
      applyInboxSnapshotPayload(
        { messages: [] },
        { merge: true, keepIds, pruneDays: days },
      );
    };

    const [cached] = await Promise.all([
      // 启动/刷新快照固定读 All mail 缓存，分类由前端标签投影
      client.listCachedEmails(mailbox, days, 100, "all", 0),
      // A draft-index failure must not prevent the Inbox cache from opening.
      loadInboxThreadDrafts(mailbox).catch(() => undefined),
    ]);
    if (snapshotRequestMailbox.current !== requestKey) return { ok: false, count: 0 };
    const cachedMessages = Array.isArray(cached.messages) ? cached.messages : [];
    if (cachedMessages.length) {
      applyPage(cached, true);
      setState((s) => ({ ...s, inboxSnapshotLoading: false, inboxLoading: false }));
      let nextOffset = Number(cached.next_offset ?? cachedMessages.length);
      let hasMore = Boolean(cached.has_more);
      let pages = 0;
      while (hasMore && pages < 30) {
        pages += 1;
        const more = await client.listCachedEmails(mailbox, days, 100, "all", nextOffset);
        if (snapshotRequestMailbox.current !== requestKey) return { ok: false, count: 0 };
        applyPage(more, false);
        const pageCount = Array.isArray(more.messages) ? more.messages.length : 0;
        nextOffset = Number(more.next_offset ?? nextOffset + pageCount);
        hasMore = Boolean(more.has_more) && pageCount > 0;
        if (!pageCount) break;
      }
      finishSoftPrune();
      return { ok: true, count: nextOffset, source: "cache" as const, messages: collectedMessages };
    }

    // 其他邮箱正在 Brief/Ask 扫描时，先跳过 live Gmail，避免与扫描并发抢 getToken。
    if (skipLiveGmail) {
      setState((s) => ({
        ...s,
        inboxSnapshotLoading: false,
        inboxLoading: false,
        inboxSnapshotComplete: true,
      }));
      return { ok: false, count: 0, source: "deferred" as const };
    }

    // 切换到无本地缓存的邮箱时必打 Gmail；Host 偶发先给废票导致 401，短暂重试一次。
    let payload: InboxFeedPayload;
    try {
      payload = await client.listInboxEmails(mailbox, days, ALL_MAIL_CACHE_FETCH_LIMIT, "all");
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      const authFlaky = /401|invalid credentials|unauthenticated|authError/i.test(detail);
      if (!authFlaky || snapshotRequestMailbox.current !== requestKey) throw error;
      await sleep(450);
      if (snapshotRequestMailbox.current !== requestKey) return { ok: false, count: 0 };
      payload = await client.listInboxEmails(mailbox, days, ALL_MAIL_CACHE_FETCH_LIMIT, "all");
    }
    if (snapshotRequestMailbox.current !== requestKey) return { ok: false, count: 0 };
    applyPage(payload, true);
    setState((s) => ({ ...s, inboxSnapshotLoading: false, inboxLoading: false }));
    let nextOffset = Number(payload.next_offset ?? (Array.isArray(payload.messages) ? payload.messages.length : 0));
    let hasMore = Boolean(payload.has_more);
    let pages = 0;
    while (hasMore && pages < 30) {
      pages += 1;
      const more = await client.listCachedEmails(mailbox, days, 100, "all", nextOffset);
      if (snapshotRequestMailbox.current !== requestKey) return { ok: false, count: 0 };
      applyPage(more, false);
      const pageCount = Array.isArray(more.messages) ? more.messages.length : 0;
      nextOffset = Number(more.next_offset ?? nextOffset + pageCount);
      hasMore = Boolean(more.has_more) && pageCount > 0;
      if (!pageCount) break;
    }
    finishSoftPrune();
    return { ok: true, count: nextOffset, source: "gmail" as const, messages: collectedMessages };
  }, [applyInboxSnapshotPayload, client, loadInboxThreadDrafts]);

  const preloadMailboxSnapshot = useCallback(async (
    mailboxOverride?: string,
    days = 30,
    force = false,
    options: { skipLiveGmail?: boolean } = {},
  ) => {
    const mailbox = normalizedMailbox(mailboxOverride || state.selectedMailboxes[0] || state.mailbox);
    if (!mailbox || mailbox === "all") return false;
    if (!force && snapshotPromise.current && snapshotRequestMailbox.current === mailbox) return snapshotPromise.current;
    snapshotRequestMailbox.current = mailbox;
    const skipLiveGmail = Boolean(options.skipLiveGmail);
    const run = (async () => {
      const startedAt = performance.now();
      // 已有列表时 force 也走 soft，避免自动同步整表替换抖动
      const soft = force;
      setState((s) => ({
        ...s,
        inboxSnapshotLoading: true,
        // soft 保留现有列表；冷启动仍可显示 loading
        inboxLoading: soft ? false : s.inboxLoading || !(s.inboxSnapshotMessages.length || s.inboxMessages.length),
        inboxError: soft ? "" : s.inboxError,
      }));
      try {
        const result = await loadMailboxSnapshotFromCache(mailbox, days, mailbox, { soft, skipLiveGmail });
        if (snapshotRequestMailbox.current !== mailbox) return false;
        if (!result.ok) return false;
        await preloadContactAvatars(mailbox, result.messages || []);
        console.info(`[inbox-startup] mailbox=${mailbox} days=${days} source=${result.source || "cache"} messages=${result.count} soft=${soft} elapsed_ms=${Math.round(performance.now() - startedAt)}`);
        return result.count > 0;
      } catch (error) {
        if (snapshotRequestMailbox.current !== mailbox) return false;
        const detail = error instanceof Error ? error.message : String(error);
        setState((s) => ({
          ...s,
          inboxSnapshotLoading: false,
          inboxLoading: false,
          inboxSnapshotComplete: true,
          // 切换邮箱冷启动无列表时也要露出错误，否则 401 被静默吞掉只剩空收件箱
          inboxError: detail,
        }));
        return false;
      }
    })();
    snapshotPromise.current = run.finally(() => {
      if (snapshotRequestMailbox.current === mailbox) snapshotPromise.current = null;
    });
    return snapshotPromise.current;
  }, [client, loadMailboxSnapshotFromCache, preloadContactAvatars, state.mailbox, state.selectedMailboxes]);

  /** History 增量同步 + 静默合并快照（自动同步 / 手动 Refresh / 切换邮箱后台同步共用） */
  const silentSyncInbox = useCallback(async (days?: number, mailboxOverride?: string) => {
    const mailbox = normalizedMailbox(mailboxOverride || state.selectedMailboxes[0] || state.mailbox);
    if (!mailbox || mailbox === "all") return false;
    const rangeDays = Math.max(0, Number(days ?? state.inboxSettings.display_range_days) || 30);
    const requestKey = `${mailbox}#silent:${++inboxRequestSequence.current}`;
    snapshotRequestMailbox.current = requestKey;
    snapshotPromise.current = null;
    setState((s) => ({
      ...s,
      inboxSnapshotLoading: true,
      inboxLoading: false,
      // 切换后的后台同步不清理已展示缓存，失败再写 error
      inboxError: s.mailbox === mailbox ? s.inboxError : "",
    }));
    try {
      const result = await client.syncInboxCache(mailbox);
      if (snapshotRequestMailbox.current !== requestKey) return false;
      if (result.resync_required) {
        // History 游标失效或附件摘要版本升级时重建缓存；常规增量同步不重新抓取 All-mail。
        await client.listInboxEmails(mailbox, rangeDays, ALL_MAIL_CACHE_FETCH_LIMIT, "all", true);
        if (snapshotRequestMailbox.current !== requestKey) return false;
      }
      const loaded = await loadMailboxSnapshotFromCache(mailbox, rangeDays, requestKey, { soft: true });
      if (snapshotRequestMailbox.current !== requestKey) return false;
      await preloadContactAvatars(mailbox, loaded.messages || []);
      setState((s) => ({
        ...s,
        inboxSnapshotLoading: false,
        inboxLoading: false,
        inboxError: loaded.ok ? "" : s.inboxError,
      }));
      return Boolean(loaded.ok);
    } catch (error) {
      if (snapshotRequestMailbox.current !== requestKey) return false;
      const detail = error instanceof Error ? error.message : String(error);
      setState((s) => ({
        ...s,
        inboxSnapshotLoading: false,
        inboxLoading: false,
        inboxSnapshotComplete: true,
        // 后台同步失败只出横幅，不回滚当前邮箱
        inboxError: detail,
      }));
      return false;
    } finally {
      if (snapshotRequestMailbox.current === requestKey) {
        setState((s) => ({ ...s, inboxSnapshotLoading: false }));
      }
    }
  }, [client, loadMailboxSnapshotFromCache, preloadContactAvatars, state.inboxSettings.display_range_days, state.mailbox, state.selectedMailboxes]);

  // 第三方 Gmail 客户端变更只在前台、当前邮箱稳定且没有 AI/列表重任务时同步。
  // 使用递归 timeout 而不是 interval，避免平台较慢时堆叠多个 History invoke。
  useEffect(() => {
    const AUTO_SYNC_INTERVAL_MS = Number(state.inboxSettings.auto_sync_seconds) * 1000;
    const mailbox = normalizedMailbox(state.selectedMailboxes[0] || state.mailbox);
    const canSync = state.runtime.connected
      && Boolean(mailbox && mailbox !== "all")
      && !state.aiChatLoading
      && !state.isCustomScanning
      && !state.inboxSnapshotLoading
      && AUTO_SYNC_INTERVAL_MS > 0;
    if (!canSync) return;

    let disposed = false;
    const clearTimer = () => {
      if (inboxAutoSyncTimer.current) {
        window.clearTimeout(inboxAutoSyncTimer.current);
        inboxAutoSyncTimer.current = null;
      }
    };
    let sync: () => Promise<void>;
    const schedule = (delay = AUTO_SYNC_INTERVAL_MS) => {
      clearTimer();
      inboxAutoSyncTimer.current = window.setTimeout(() => { void sync(); }, delay);
    };
    sync = async () => {
      if (
        disposed
        || document.visibilityState !== "visible"
        || inboxAutoSyncInFlight.current
      ) {
        if (!disposed && document.visibilityState === "visible") schedule();
        return;
      }
      inboxAutoSyncInFlight.current = true;
      try {
        if (state.mailDetailOpen) {
          // 详情抽屉打开期间不改快照，避免正文/滚动被重置；关闭后再静默同步。
          inboxAutoSyncPending.current = { mailbox, days: state.inboxSettings.display_range_days };
        } else if (!disposed && document.visibilityState === "visible") {
          await silentSyncInbox(state.inboxSettings.display_range_days);
        }
      } catch {
        // silentSyncInbox 已写入 inboxError；下一轮按同一 cursor 安全重试。
      } finally {
        inboxAutoSyncInFlight.current = false;
        if (!disposed && document.visibilityState === "visible") schedule();
      }
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        void sync();
      } else {
        clearTimer();
      }
    };

    document.addEventListener("visibilitychange", onVisibilityChange);
    schedule();
    return () => {
      disposed = true;
      clearTimer();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [silentSyncInbox, state.aiChatLoading, state.inboxSettings.auto_sync_seconds, state.inboxSettings.display_range_days, state.inboxSnapshotLoading, state.isCustomScanning, state.mailDetailOpen, state.mailbox, state.runtime.connected, state.selectedMailboxes]);

  useEffect(() => {
    if (state.mailDetailOpen) return;
    const pending = inboxAutoSyncPending.current;
    if (!pending) return;
    inboxAutoSyncPending.current = null;
    void silentSyncInbox(pending.days);
  }, [silentSyncInbox, state.mailDetailOpen]);

  const upsertAiConversationHistory = useCallback((
    conversationId: string,
    messages: AiChatMessage[],
    options: {
      kind: "chat" | "scan";
      query: string;
      result?: CustomRunResult;
      pendingRun?: AskHistoryEntry["pendingRun"];
    },
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
        pendingRun: options.pendingRun,
      };
      const nextHistory = pruneAskHistoryEntries([
        entry,
        ...s.askHistory.filter((item) => item.conversationId !== conversationId),
      ]);
      persistAskHistory(nextHistory);
      // Ask history 是“会话索引”；点击历史恢复 messages 后，用户可以继续在同一 conversationId 里追问。
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

  const loadInboxEmails = useCallback(async (mailboxOverride?: string, category = "inbox", days = 30, force = false) => {
    const mailbox = normalizedMailbox(mailboxOverride || state.selectedMailboxes[0] || state.mailbox);
    const requestId = ++inboxRequestSequence.current;
    if (!mailbox || mailbox === "all") {
      setState((s) => ({ ...s, inboxMessages: [], inboxLoading: false, inboxError: "Connect a Gmail mailbox to load your inbox." }));
      return false;
    }
    // 内存缓存键统一为 all：后端始终返回 All mail 快照
    const cacheKey = `${mailbox}|all|${days}`;
    const cached = inboxFeedCache.current.get(cacheKey);
    if (cached) {
      applyInboxSnapshotPayload(cached.payload);
      if (!force && Date.now() - cached.loadedAt < 60_000) return true;
    }
    setState((s) => ({ ...s, inboxLoading: true, inboxError: "" }));
    try {
      const payload = await client.listInboxEmails(mailbox, days, ALL_MAIL_CACHE_FETCH_LIMIT, "all");
      inboxFeedCache.current.set(cacheKey, { payload, loadedAt: Date.now() });
      if (requestId !== inboxRequestSequence.current) return false;
      applyInboxSnapshotPayload(payload);
      return true;
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      console.error("[loadInboxEmails] failed:", detail, error);
      if (requestId !== inboxRequestSequence.current) return false;
      setState((s) => ({ ...s, inboxLoading: false, inboxError: detail }));
      return false;
    }
    return false;
  }, [applyInboxSnapshotPayload, client, state.mailbox, state.selectedMailboxes]);

  const refreshInboxEmails = useCallback(async (category = "inbox", days = 30, clearCache = false): Promise<InboxPageResult> => {
    const mailbox = normalizedMailbox(state.selectedMailboxes[0] || state.mailbox);
    if (!mailbox || mailbox === "all") {
      setState((s) => ({ ...s, inboxMessages: [], inboxSnapshotMessages: [], inboxLoading: false, inboxSnapshotLoading: false, inboxError: "Connect a Gmail mailbox to load your inbox." }));
      return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
    }

    // 非清缓存刷新与自动同步对齐：History 增量 + 静默合并
    if (!clearCache) {
      const ok = await silentSyncInbox(days);
      const count = 0;
      // 调用方主要看 ok；count/hasMore 由后续 feed 状态自行推导
      if (ok) {
        showToast(days > 7
          ? `Inbox synced for the last ${days} days.`
          : "Inbox refreshed.");
      }
      return {
        ok,
        count,
        hasMore: false,
        nextOffset: 0,
        messages: [],
      };
    }

    const requestKey = `${mailbox}#refresh:${++inboxRequestSequence.current}`;
    snapshotRequestMailbox.current = requestKey;
    snapshotPromise.current = null;
    for (const key of inboxFeedCache.current.keys()) {
      if (key.startsWith(`${mailbox}|`)) inboxFeedCache.current.delete(key);
    }
    await clearMailboxCacheData(mailbox);
    setState((s) => ({
      ...s,
      inboxMessages: [],
      inboxSnapshotMessages: [],
      inboxLoading: true,
      inboxSnapshotLoading: true,
      inboxSnapshotComplete: false,
      inboxError: "",
    }));

    try {
      // 硬刷新：清空后首屏 replace，后续分页 append
      const payload = await client.listInboxEmails(mailbox, days, ALL_MAIL_CACHE_FETCH_LIMIT, "all", true);
      if (snapshotRequestMailbox.current !== requestKey) return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
      applyInboxSnapshotPayload(payload);
      const refreshedMessages: InboxMessage[] = Array.isArray(payload.messages) ? [...payload.messages] : [];
      setState((s) => ({ ...s, inboxSnapshotLoading: false, inboxLoading: false }));
      let nextOffset = Number(payload.next_offset ?? (Array.isArray(payload.messages) ? payload.messages.length : 0));
      let hasMore = Boolean(payload.has_more);
      let pages = 0;
      while (hasMore && pages < 30) {
        pages += 1;
        const cached = await client.listCachedEmails(mailbox, days, 100, "all", nextOffset);
        if (snapshotRequestMailbox.current !== requestKey) return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
        applyInboxSnapshotPayload(cached, { append: true });
        if (Array.isArray(cached.messages)) refreshedMessages.push(...cached.messages);
        const pageCount = Array.isArray(cached.messages) ? cached.messages.length : 0;
        nextOffset = Number(cached.next_offset ?? nextOffset + pageCount);
        hasMore = Boolean(cached.has_more) && pageCount > 0;
        if (!pageCount) break;
      }
      await preloadContactAvatars(mailbox, refreshedMessages);
      inboxFeedCache.current.set(`${mailbox}|all|${days}`, { payload, loadedAt: Date.now() });
      const count = nextOffset;
      showToast(days > 7
        ? `Inbox synced for the last ${days} days. ${count} email${count === 1 ? "" : "s"} loaded.`
        : `Inbox refreshed. ${count} email${count === 1 ? "" : "s"} loaded.`);
      return { ok: true, count, hasMore, nextOffset, messages: Array.isArray(payload.messages) ? payload.messages : [] };
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
  }, [applyInboxSnapshotPayload, client, preloadContactAvatars, silentSyncInbox, state.mailbox, state.selectedMailboxes]);

  const clearInboxCacheAndReload = useCallback(async (days?: number) => {
    const rangeDays = Math.max(0, Number(days ?? state.inboxSettings.display_range_days) || 30);
    const result = await refreshInboxEmails("all", rangeDays, true);
    return Boolean(result.ok);
  }, [refreshInboxEmails, state.inboxSettings.display_range_days]);

  const expandInboxFeedWindow = useCallback(async (days = 30): Promise<InboxPageResult> => {
    const mailbox = normalizedMailbox(state.selectedMailboxes[0] || state.mailbox);
    if (!mailbox || mailbox === "all") {
      return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
    }
    const requestKey = `${mailbox}#expand:${++inboxRequestSequence.current}`;
    snapshotRequestMailbox.current = requestKey;
    const windowDays = Math.max(0, Math.min(Number(days) || 0, 3650));
    setState((s) => ({
      ...s,
      inboxSnapshotLoading: true,
      inboxError: "",
    }));
    try {
      // 不清缓存、不替换快照：先把更宽窗口写入本地 All mail，再 append 分页补齐
      await client.listInboxEmails(mailbox, windowDays, ALL_MAIL_CACHE_FETCH_LIMIT, "all", false);
      if (snapshotRequestMailbox.current !== requestKey) {
        return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
      }
      let nextOffset = 0;
      let hasMore = true;
      let pages = 0;
      let appended = 0;
      while (hasMore && pages < 30) {
        pages += 1;
        const cached = await client.listCachedEmails(mailbox, windowDays, 100, "all", nextOffset);
        if (snapshotRequestMailbox.current !== requestKey) {
          return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
        }
        applyInboxSnapshotPayload(cached, { append: true });
        const pageCount = Array.isArray(cached.messages) ? cached.messages.length : 0;
        appended += pageCount;
        nextOffset = Number(cached.next_offset ?? nextOffset + pageCount);
        hasMore = Boolean(cached.has_more) && pageCount > 0;
        if (!pageCount) break;
      }
      // days=0 时缓存耗尽后可继续用 Gmail 页扩 All mail；append 按 id 去重
      if (windowDays === 0) {
        let gmailHasMore = true;
        let pageToken = "";
        let pageOffset = 0;
        let gmailPages = 0;
        const exclude = new Set<string>();
        while (gmailHasMore && gmailPages < 10) {
          gmailPages += 1;
          const page = await client.listGmailEmailsPage(
            mailbox,
            0,
            100,
            "all",
            pageToken,
            pageOffset,
            [...exclude],
          );
          if (snapshotRequestMailbox.current !== requestKey) {
            return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
          }
          applyInboxSnapshotPayload(page, { append: true });
          const pageMessages = Array.isArray(page.messages) ? page.messages : [];
          for (const message of pageMessages) {
            if (message.id) exclude.add(message.id);
          }
          appended += pageMessages.length;
          pageToken = String(page.page_token || "");
          pageOffset = Number(page.page_offset || 0);
          gmailHasMore = Boolean(page.has_more) && pageMessages.length > 0;
          if (!pageMessages.length) break;
        }
        hasMore = gmailHasMore;
      }
      return { ok: true, count: appended, hasMore, nextOffset };
    } catch (error) {
      if (snapshotRequestMailbox.current !== requestKey) {
        return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
      }
      const detail = error instanceof Error ? error.message : String(error);
      console.error("[expandInboxFeedWindow] failed:", detail, error);
      setState((s) => ({
        ...s,
        inboxSnapshotComplete: true,
        inboxError: detail,
      }));
      return { ok: false, count: 0, hasMore: false, nextOffset: 0 };
    } finally {
      if (snapshotRequestMailbox.current === requestKey) {
        setState((s) => ({ ...s, inboxSnapshotLoading: false }));
      }
    }
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

  const loadInboxSettings = useCallback(async (mailboxOverride?: string): Promise<InboxSettings | null> => {
    const mailbox = normalizedMailbox(mailboxOverride || state.mailbox);
    if (!mailbox) return null;
    setState((s) => ({ ...s, inboxSettingsLoading: true, inboxSettingsError: "" }));
    try {
      const payload = await client.loadInboxSettings(mailbox, state.storageProvider);
      // 平台/旧存储可能缺 custom_categories 或类型异常；统一 clamp 避免首页迭代白屏。
      const settings = clampInboxSettings(payload.settings);
      setState((s) => {
        // 启动时 mailbox 可能尚未写入；只要请求邮箱匹配当前或当前仍为空则接受
        const current = normalizedMailbox(s.mailbox);
        if (current && current !== mailbox) return { ...s, inboxSettingsLoading: false };
        return {
          ...s,
          mailbox: current || mailbox,
          inboxSettings: settings,
          inboxSettingsEtag: payload.etag || "",
          inboxSettingsLoading: false,
        };
      });
      return settings;
    } catch (error) {
      setState((s) => ({ ...s, inboxSettingsLoading: false, inboxSettingsError: error instanceof Error ? error.message : String(error) }));
      return null;
    }
  }, [client, state.mailbox, state.storageProvider]);

  const saveInboxSettings = useCallback(async (patch: Partial<InboxSettings>) => {
    const previous = state.inboxSettings;
    const previousEtag = state.inboxSettingsEtag;
    setState((s) => ({ ...s, inboxSettings: clampInboxSettings({ ...s.inboxSettings, ...patch }), inboxSettingsLoading: true, inboxSettingsError: "" }));
    try {
      const payload = await client.saveInboxSettings(state.mailbox, patch, previousEtag, state.storageProvider);
      setState((s) => ({ ...s, inboxSettings: clampInboxSettings(payload.settings), inboxSettingsEtag: payload.etag || "", inboxSettingsLoading: false }));
      return true;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState((s) => ({ ...s, inboxSettings: previous, inboxSettingsEtag: previousEtag, inboxSettingsLoading: false, inboxSettingsError: message }));
      showToast(message);
      return false;
    }
  }, [client, showToast, state.inboxSettings, state.inboxSettingsEtag, state.mailbox, state.storageProvider]);

  const loadScanPlanForRun = useCallback(async (mailbox: string): Promise<Required<Pick<ScanPlan, "scan_window_days" | "max_messages">>> => {
    const normalized = normalizedMailbox(mailbox);
    const visiblePlanMailbox = normalizedMailbox(state.configMailbox || state.mailbox);
    let plan: Required<Pick<ScanPlan, "scan_window_days" | "max_messages">>;
    if (normalized && normalized === visiblePlanMailbox && state.scanPlan) {
      plan = normalizeScanPlan(state.scanPlan);
    } else {
      try {
        plan = normalizeScanPlan(await client.loadScanPlan(mailbox, state.storageProvider));
      } catch {
        plan = normalizeScanPlan(null);
      }
    }

    try {
      const settings = clampInboxSettings(
        normalized && normalized === normalizedMailbox(state.mailbox)
          ? state.inboxSettings
          : (await client.loadInboxSettings(mailbox, state.storageProvider)).settings,
      );
      // Display range 是用户在 Settings 中可见的时间选择，AI 扫描必须使用同一范围。
      return {
        ...plan,
        scan_window_days: clampInt(settings.display_range_days, plan.scan_window_days, 1, 90),
      };
    } catch {
      return plan;
    }
  }, [client, state.configMailbox, state.inboxSettings, state.mailbox, state.scanPlan, state.storageProvider]);

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

  // 供 Toast Retry / 轮询回调稳定调用最新探测函数，避免闭包陈旧
  const refreshSamplingStatusRef = useRef<(options?: { fromPoll?: boolean }) => Promise<AppState["llmStatus"]>>(async () => ({
    status: "unknown",
    checked: false,
  }));
  const refreshGmailApiStatusRef = useRef<(options?: { fromPoll?: boolean }) => Promise<AppState["gmailApiStatus"]>>(async () => ({
    status: "unknown",
    checked: false,
  }));
  const refreshConnectivityStatusRef = useRef<(options?: { fromPoll?: boolean }) => Promise<void>>(async () => undefined);
  const llmStatusRef = useRef(state.llmStatus);
  llmStatusRef.current = state.llmStatus;
  const gmailApiStatusRef = useRef(state.gmailApiStatus);
  gmailApiStatusRef.current = state.gmailApiStatus;
  const mailboxForGmailCheckRef = useRef(state.mailbox);
  mailboxForGmailCheckRef.current = state.mailbox;
  gmailBusyRef.current = Boolean(
    state.isScanning || state.isPreparingScan || state.isCustomScanning || state.aiChatLoading,
  );
  // 进行中探测去重：轮询/连点不叠发，避免与扫描争抢
  const llmCheckInFlightRef = useRef(false);
  const gmailCheckInFlightRef = useRef(false);

  const refreshSamplingStatus = useCallback(async (options?: { fromPoll?: boolean }): Promise<AppState["llmStatus"]> => {
    if (llmCheckInFlightRef.current) {
      // 已有探测在飞：轮询直接跳过；手动点击返回当前状态
      return llmStatusRef.current;
    }
    llmCheckInFlightRef.current = true;
    const previous = llmStatusRef.current;
    // 轮询静默探测，避免侧栏每分钟闪 Checking…；手动/启动探测才显示 checking
    if (!options?.fromPoll) {
      setState((s) => ({ ...s, llmStatus: { ...s.llmStatus, status: "checking", message: "Checking Anna LLM..." } }));
    }
    let next: AppState["llmStatus"];
    try {
      const result = await client.checkSamplingStatus();
      const status = result.ok === false
        ? (result.status === "error" ? "error" : "unavailable")
        : (result.status || "connected");
      next = {
        status: status === "connected" ? "connected" : status === "error" ? "error" : "unavailable",
        checked: true,
        message: result.message || (status === "connected" ? "Anna LLM sampling is connected." : "Anna LLM sampling is unavailable."),
        elapsed_ms: result.elapsed_ms,
      };
    } catch (error) {
      next = {
        status: "error",
        checked: true,
        message: error instanceof Error ? error.message : String(error),
      };
    } finally {
      llmCheckInFlightRef.current = false;
    }
    setState((s) => ({ ...s, llmStatus: next }));
    llmStatusRef.current = next;
    // 失败时 toast + Retry；轮询仅在「刚从可用变为不可用」时提示，避免每分钟刷屏
    if (next.status !== "connected") {
      const transitionedToFail = previous.status === "connected" || previous.status === "unknown" || !previous.checked;
      if (!options?.fromPoll || transitionedToFail) {
        showToast("Anna LLM is unavailable. Please enable sampling permission for this Executa app, then try again.", {
          actionLabel: "Retry",
          onAction: () => {
            void refreshSamplingStatusRef.current();
          },
          durationMs: 8_000,
        });
      }
    }
    return next;
  }, [client, showToast]);
  refreshSamplingStatusRef.current = refreshSamplingStatus;

  const refreshGmailApiStatus = useCallback(async (options?: { fromPoll?: boolean }): Promise<AppState["gmailApiStatus"]> => {
    if (gmailCheckInFlightRef.current) {
      return gmailApiStatusRef.current;
    }
    gmailCheckInFlightRef.current = true;
    const previous = gmailApiStatusRef.current;
    if (!options?.fromPoll) {
      setState((s) => ({ ...s, gmailApiStatus: { ...s.gmailApiStatus, status: "checking", message: "Checking Gmail API..." } }));
    }
    let next: AppState["gmailApiStatus"];
    try {
      const result = await client.checkGmailApiStatus(mailboxForGmailCheckRef.current || "");
      const status = result.ok === false
        ? (result.status === "error" ? "error" : "unavailable")
        : (result.status || "connected");
      next = {
        status: status === "connected" ? "connected" : status === "error" ? "error" : "unavailable",
        checked: true,
        message: result.message || (status === "connected" ? "Gmail API is connected." : "Gmail API is unavailable."),
        elapsed_ms: result.elapsed_ms,
        mailbox: result.mailbox,
      };
    } catch (error) {
      next = {
        status: "error",
        checked: true,
        message: error instanceof Error ? error.message : String(error),
      };
    } finally {
      gmailCheckInFlightRef.current = false;
    }
    setState((s) => ({ ...s, gmailApiStatus: next }));
    gmailApiStatusRef.current = next;
    if (next.status !== "connected") {
      const transitionedToFail = previous.status === "connected" || previous.status === "unknown" || !previous.checked;
      if (!options?.fromPoll || transitionedToFail) {
        showToast("Gmail API is unavailable. Check network or re-authorize the mailbox, then try again.", {
          actionLabel: "Retry",
          onAction: () => {
            void refreshGmailApiStatusRef.current();
          },
          durationMs: 8_000,
        });
      }
    }
    return next;
  }, [client, showToast]);
  refreshGmailApiStatusRef.current = refreshGmailApiStatus;

  // LLM 与 Gmail API 并行探测，侧栏点击 / 轮询 / Toast Retry 共用
  const refreshConnectivityStatus = useCallback(async (options?: { fromPoll?: boolean }) => {
    await Promise.all([
      refreshSamplingStatusRef.current(options),
      refreshGmailApiStatusRef.current(options),
    ]);
  }, []);
  refreshConnectivityStatusRef.current = refreshConnectivityStatus;

  // 扫描 / AI turn 进行中不跑定时探测，避免与业务 invoke 争抢后端 worker
  const connectivityBusy =
    state.isScanning || state.isPreparingScan || state.isCustomScanning || state.aiChatLoading;

  // Settings 配置的连通性轮询；0 表示关闭；LLM 与 Gmail 并行
  useEffect(() => {
    const pollSeconds = Number(state.inboxSettings.llm_status_poll_seconds);
    if (!state.runtime.connected || connectivityBusy || !Number.isFinite(pollSeconds) || pollSeconds <= 0) return;
    const timer = window.setInterval(() => {
      void refreshConnectivityStatusRef.current({ fromPoll: true });
    }, pollSeconds * 1000);
    return () => window.clearInterval(timer);
  }, [connectivityBusy, state.inboxSettings.llm_status_poll_seconds, state.runtime.connected]);

  const ensureSamplingAvailable = useCallback(async (): Promise<boolean> => {
    if (state.llmProvider !== "anna-llm") return true;
    const status = await refreshSamplingStatus();
    if (status.status === "connected") return true;
    const message = "Anna LLM is unavailable. Please enable sampling permission for this Executa app, then try again.";
    setState((s) => ({ ...s, scanError: status.message ? `${message}\n${status.message}` : message }));
    return false;
  }, [refreshSamplingStatus, state.llmProvider]);

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

  const stopActiveScans = useCallback((reason = "switched mailbox") => {
    // 立刻抬升代际号，阻断 Brief continue 循环与后续邮箱扫描。
    briefScanGenerationRef.current += 1;
    // 让已经发出的邮件/草稿请求失效；请求本身无法被 stdio 强制撤销时，
    // 其返回值也不能再写入切换后的邮箱状态。
    inboxRequestSequence.current += 1;
    draftRequestSequence.current += 1;
    snapshotRequestMailbox.current = "";
    const briefRunId = briefScanRunIdRef.current;
    briefScanRunIdRef.current = "";
    const backgroundRunId = activeBackgroundRunIdRef.current;
    activeBackgroundRunIdRef.current = "";
    const aiRun = aiGenerationRun.current;
    if (aiRun) {
      aiRun.cancelled = true;
      aiRun.controller.abort();
      aiGenerationRun.current = null;
    }
    if (deferredInboxLoadTimer.current) {
      window.clearTimeout(deferredInboxLoadTimer.current);
      deferredInboxLoadTimer.current = null;
    }
    gmailBusyRef.current = false;
    setState((s) => ({
      ...s,
      isScanning: false,
      isPreparingScan: false,
      isCustomScanning: false,
      aiChatLoading: false,
      scanStatus: "",
      scanStage: "",
      scanProgress: {},
      customRunProgress: null,
    }));
    // 后端取消：阻止 continue 继续占 worker / getToken（失败忽略）。
    if (briefRunId) void client.cancelMailAgentRun(briefRunId).catch(() => undefined);
    if (backgroundRunId && backgroundRunId !== briefRunId) {
      void client.cancelMailAgentRun(backgroundRunId).catch(() => undefined);
    }
    void reason;
  }, [client]);

  const runBriefScan = useCallback(async (scanRequest: { mailboxesToScan: string[]; scanMode: string }, reason = "manual") => {
    const { mailboxesToScan, scanMode } = scanRequest;
    const scanGeneration = ++briefScanGenerationRef.current;
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
    let cancelled = false;
    try {
      const failures: string[] = [];
      for (let index = 0; index < mailboxesToScan.length; index += 1) {
        if (scanGeneration !== briefScanGenerationRef.current) {
          cancelled = true;
          break;
        }
        const mailbox = mailboxesToScan[index];
        const runScanPlan = await loadScanPlanForRun(mailbox);
        if (scanGeneration !== briefScanGenerationRef.current) {
          cancelled = true;
          break;
        }
        const runId = `bg_${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`;
        briefScanRunIdRef.current = runId;
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
        if (scanGeneration !== briefScanGenerationRef.current) {
          cancelled = true;
          void client.cancelMailAgentRun(runId).catch(() => undefined);
          break;
        }
        const applyBriefStatus = (status: RunStatus) => {
          if (scanGeneration !== briefScanGenerationRef.current) return;
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
            if (scanGeneration !== briefScanGenerationRef.current) {
              cancelled = true;
              break;
            }
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
            if (scanGeneration !== briefScanGenerationRef.current) {
              cancelled = true;
              break;
            }
            applyBriefStatus(result);
            collectWarnings(result);
            const nextCardsVersion = Number(result.cards_version || 0);
            if (Number(result.cards_added || 0) > 0 || nextCardsVersion > cardsVersion) {
              cardsVersion = nextCardsVersion;
              void loadActiveCards(undefined, "all", { timeoutMs: 55_000 });
            }
            if (
              result.status === "done"
              || result.status === "failed"
              || result.needs_continue === false
              || result.stage === "cancelled"
              || /cancelled/i.test(String(result.error || ""))
            ) {
              if (result.stage === "cancelled" || /cancelled/i.test(String(result.error || ""))) {
                cancelled = true;
              }
              break;
            }
          }
        } finally {
          window.clearInterval(pollTimer);
          if (scanGeneration === briefScanGenerationRef.current) {
            await refreshBriefStatus();
          }
          if (briefScanRunIdRef.current === runId) briefScanRunIdRef.current = "";
        }
        if (cancelled || scanGeneration !== briefScanGenerationRef.current) {
          cancelled = true;
          break;
        }
        if (result.status === "failed" || result.error) {
          if (!(result.stage === "cancelled" || /cancelled/i.test(String(result.error || "")))) {
            failures.push(`${mailbox}: ${result.error || "Scan failed"}`);
          }
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
      if (cancelled || scanGeneration !== briefScanGenerationRef.current) {
        return;
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
      if (scanGeneration !== briefScanGenerationRef.current) return;
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
      if (scanGeneration === briefScanGenerationRef.current) {
        setState((s) => ({ ...s, isPreparingScan: false, isScanning: false }));
      }
    }
  }, [client, loadActiveCards, loadRunHistory, loadScanPlanForRun, showToast, state.llmProvider, state.mailbox, state.storageProvider]);

  const loadMailboxes = useCallback(async (storageOverride?: string): Promise<{ mailboxes: MailboxInfo[]; selected: string[]; primary: string }> => {
    const provider = storageOverride ?? state.storageProvider;
    try {
      const payload = await client.listMailboxes(provider);
      const mailboxes = Array.isArray(payload.mailboxes) ? payload.mailboxes : [];
      const credentialsMessage = connectedAccountsStatusMessage(payload.credentials_status);
      if (credentialsMessage) showToast(credentialsMessage);
      const selection = resolveMailboxSelection({
        mailboxes,
        selected: (Array.isArray(payload.selected) && payload.selected.length
          ? payload.selected
          : mailboxes.filter((item) => item.selected !== false).map((item) => item.email)),
        fallback: state.mailbox,
      });
      const { primary, selected, mailboxes: normalizedMailboxes } = selection;
      void cacheMailboxes(normalizedMailboxes);
      if (primary) void setSelectedMailbox(primary);
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
  }, [client, showToast, state.mailbox, state.storageProvider]);

  const loadMailboxRegistry = useCallback(async (storageOverride?: string): Promise<{ mailboxes: MailboxInfo[]; selected: string[]; primary: string }> => {
    const provider = storageOverride ?? state.storageProvider;
    try {
      const payload = await client.getMailboxRegistry(provider);
      const mailboxes = Array.isArray(payload.mailboxes) ? payload.mailboxes : [];
      const selection = resolveMailboxSelection({
        mailboxes,
        selected: (Array.isArray(payload.selected) && payload.selected.length
          ? payload.selected
          : mailboxes.filter((item) => item.selected !== false).map((item) => item.email)),
        fallback: state.mailbox,
      });
      const { primary, selected, mailboxes: normalizedMailboxes } = selection;
      void cacheMailboxes(normalizedMailboxes);
      if (primary) void setSelectedMailbox(primary);
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
        const restoredMailbox = normalizedMailbox(await migrateSelectedMailboxFromLocalStorage(MAILBOX_STORAGE_KEY));
        const bootMailbox = restoredMailbox || normalizedMailbox(state.mailbox);
        if (restoredMailbox && restoredMailbox !== normalizedMailbox(state.mailbox)) {
          setState((s) => ({
            ...s,
            mailbox: restoredMailbox,
            selectedMailboxes: [restoredMailbox],
            briefMailboxFilter: [restoredMailbox],
          }));
        }
        // 先读 display_range，再按该天数预热，避免先 30 天闪一下再清空加载 7 天
        let rangeDays = clampInboxSettings(state.inboxSettings).display_range_days;
        if (bootMailbox) {
          const bootSettings = await loadInboxSettings(bootMailbox);
          if (bootSettings?.display_range_days) rangeDays = bootSettings.display_range_days;
        }
        let inboxAvailable = bootMailbox
          ? await preloadMailboxSnapshot(bootMailbox, rangeDays)
          : false;
        const mailboxState = await loadMailboxRegistry();
        let currentMailbox = mailboxState.primary || bootMailbox;
        if (!currentMailbox) {
          const mailbox = await discoverMailbox();
          currentMailbox = mailbox || state.mailbox;
        }
        if (currentMailbox && currentMailbox !== bootMailbox) {
          const switchedSettings = await loadInboxSettings(currentMailbox);
          if (switchedSettings?.display_range_days) {
            rangeDays = switchedSettings.display_range_days;
          }
          inboxAvailable = await preloadMailboxSnapshot(currentMailbox, rangeDays, true);
        }
        void loadMailboxes().then(async (discoveredState) => {
          const discoveredPrimary = normalizedMailbox(discoveredState.primary);
          if (!discoveredPrimary || discoveredPrimary === currentMailbox) return;
          const discoveredSettings = await loadInboxSettings(discoveredPrimary);
          const discoveredDays = discoveredSettings?.display_range_days || rangeDays;
          void preloadMailboxSnapshot(discoveredPrimary, discoveredDays, true);
          void loadScanPlan(discoveredPrimary);
        }).catch(() => undefined);
        const authResult = await client.checkAnyGmailAuth();
        const systemAuthorized = Boolean(authResult?.authorized);
        const authWarning = (authResult as Record<string, unknown> | null | undefined)?.warning as string | undefined;
        setState((s) => ({ ...s, gmailAuthStatus: { checked: true, authorized: systemAuthorized, source: authResult?.source || "none" } }));
        if (authWarning) showToast(`Auth notice: ${authWarning}`);
        // mailbox 就绪后，Gmail 探测使用当前邮箱。
        mailboxForGmailCheckRef.current = currentMailbox || mailboxForGmailCheckRef.current;
        if (!systemAuthorized) {
          setState((s) => ({
            ...s,
            loading: false,
            inboxLoading: false,
            inboxError: inboxAvailable
              ? ""
              : `Connect Gmail to load the last ${rangeDays} days of email.`,
          }));
          return;
        }
        await Promise.all([
          loadRunHistory(),
          loadCustomPlans(),
          loadScanPlan(currentMailbox),
          // settings 已在预热前加载；此处再拉一次保证 etag / 与当前邮箱对齐（不再触发 30 天预热）
          loadInboxSettings(currentMailbox),
        ]);
        // Brief 管线已下线：启动时清空 cards / processed / scan_state，不再加载 Attention Cards。
        try {
          const targets = (state.selectedMailboxes?.length
            ? state.selectedMailboxes
            : currentMailbox
              ? [currentMailbox]
              : []
          ).map((m) => String(m || "").trim()).filter(Boolean);
          await Promise.all(
            targets.map((mailbox) =>
              client.resetMailboxScanHistory(mailbox, state.storageProvider).catch(() => undefined),
            ),
          );
          setState((s) => ({
            ...s,
            cards: [],
            allCards: [],
            scanState: null,
            cleanupBundle: null,
            actionCount: 0,
          }));
        } catch {
          setState((s) => ({ ...s, cards: [], allCards: [], scanState: null, actionCount: 0 }));
        }
        // 初始化请求释放后再运行端到端延迟探测，避免启动阶段挤占 Executa worker 与 Host 反向 RPC。
        void refreshConnectivityStatus();
        console.info(`[inbox-startup] initialize elapsed_ms=${Math.round(performance.now() - startedAt)} mailbox=${currentMailbox || ""} range_days=${rangeDays}`);
      }
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      showToast(`Init failed: ${msg}`);
      setState((s) => ({ ...s, loading: false, inboxLoading: false, inboxError: msg }));
    }
  }, [client, discoverMailbox, getRuntime, loadCustomPlans, loadInboxSettings, loadMailboxRegistry, loadMailboxes, loadRunHistory, loadScanPlan, preloadMailboxSnapshot, refreshConnectivityStatus, showToast, state.mailbox, state.selectedMailboxes, state.storageProvider]);

  // 初次连接失败不会再永久缓存 mock runtime；前台保持每 5 秒尝试一次完整初始化，
  // 成功后 effect 自动停止。业务 mutation 不在此处重放，仍需用户再次确认。
  useEffect(() => {
    if (state.runtime.connected || state.runtime.mode !== "mock") return;
    const timer = window.setTimeout(() => { void initialize(); }, 5_000);
    return () => window.clearTimeout(timer);
  }, [initialize, state.runtime.connected, state.runtime.mode]);

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
    setMailDetailOpen(open, messageId = "") {
      const nextMessageId = open ? String(messageId || "").trim() : "";
      setState((s) => (
        s.mailDetailOpen === open && s.mailDetailMessageId === nextMessageId
          ? s
          : { ...s, mailDetailOpen: open, mailDetailMessageId: nextMessageId }
      ));
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
    openSettings(focusSavedPrompts = false) {
      setState((s) => ({
        ...s,
        settingsOpen: true,
        settingsFocusRequest: focusSavedPrompts
          ? s.settingsFocusRequest + 1
          : s.settingsFocusRequest,
      }));
      void loadInboxSettings();
    },
    closeSettings() { setState((s) => ({ ...s, settingsOpen: false })); },
    loadInboxSettings,
    saveInboxSettings,
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
      return loadMailboxes();
    },
    async switchMailbox(mailbox) {
      const primary = normalizedMailbox(mailbox);
      if (!primary || primary === normalizedMailbox(state.mailbox)) {
        setState((s) => ({ ...s, selectedMailboxes: primary ? [primary] : s.selectedMailboxes, briefMailboxFilter: primary ? [primary] : s.briefMailboxFilter }));
        return;
      }
      // 立刻停上一邮箱扫描，释放 getToken / Gmail。
      stopActiveScans("switched mailbox");
      if (deferredInboxLoadTimer.current) {
        window.clearTimeout(deferredInboxLoadTimer.current);
        deferredInboxLoadTimer.current = null;
      }
      snapshotRequestMailbox.current = primary;
      snapshotPromise.current = null;
      const target = state.mailboxes.find((item) => normalizedMailbox(item.email) === primary);
      showAccountSwitchNotice(primary, target?.avatar_url);
      // 瞬间切 UI：列表先换成目标邮箱空壳，随后用本地缓存秒填（无缓存则保持空）。
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
          inboxLoading: false,
          inboxSnapshotLoading: true,
          inboxError: "",
          inboxMessages: [],
          inboxSnapshotMessages: [],
          inboxDraftMessages: [],
          inboxSnapshotComplete: false,
        };
      });
      void setSelectedMailbox(primary);
      const rangeDays = clampInboxSettings(state.inboxSettings).display_range_days;

      // 注册表 / 设置 / Scan plan 不挡首屏；失败不回滚邮箱。
      void (async () => {
        try {
          for (const item of state.mailboxes) {
            if (normalizedMailbox(item.email) === primary || item.selected === false) continue;
            await client.setMailboxSelected(item.email, false, state.storageProvider);
          }
          const payload = await client.setMailboxSelected(primary, true, state.storageProvider);
          if (snapshotRequestMailbox.current !== primary) return;
          const returnedMailboxes = Array.isArray(payload.mailboxes) ? payload.mailboxes : state.mailboxes;
          const mailboxes = returnedMailboxes.map((item) => ({
            ...item,
            selected: normalizedMailbox(item.email) === primary,
          }));
          void cacheMailboxes(mailboxes);
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
          }));
          await loadInboxSettings(primary);
          if (snapshotRequestMailbox.current !== primary) return;
          await loadScanPlan(primary);
        } catch (error) {
          if (snapshotRequestMailbox.current !== primary) return;
          const message = error instanceof Error ? error.message : String(error);
          const hardAuthFailed = /revoked|reconnect|not granted|unavailable through Connected accounts/i.test(message);
          setState((s) => ({
            ...s,
            mailbox: primary,
            selectedMailboxes: [primary],
            briefMailboxFilter: [primary],
            inboxLoading: false,
            inboxSnapshotLoading: false,
            inboxError: message,
            mailboxes: s.mailboxes.map((item) => ({
              ...item,
              selected: normalizedMailbox(item.email) === primary,
              ...(normalizedMailbox(item.email) === primary && hardAuthFailed
                ? { authorized: false, last_error: "Reconnect Gmail to continue." }
                : {}),
            })),
          }));
          if (hardAuthFailed) {
            showToast("This Gmail authorization expired. Reconnect the account and try again.");
          }
        }
      })();

      // 首屏只读本地缓存，绝不在切换路径上 list_inbox_emails。
      // 只等待第一页，避免缓存分页或 APS 延迟拖慢邮箱切换。
      try {
        const cached = await client.listCachedEmails(primary, rangeDays, 100, "all", 0);
        if (snapshotRequestMailbox.current !== primary) return;
        applyInboxSnapshotPayload(cached);
        void loadInboxThreadDrafts(primary).catch(() => undefined);
      } catch {
        // 缓存读失败也保持目标邮箱；后台 sync 再试。
        if (snapshotRequestMailbox.current === primary) {
          setState((s) => ({ ...s, inboxLoading: false, inboxSnapshotLoading: false, inboxSnapshotComplete: true }));
        }
      }
      if (snapshotRequestMailbox.current !== primary) return;
      setState((s) => ({
        ...s,
        inboxLoading: false,
        inboxSnapshotComplete: true,
      }));
      // 后台继续读缓存并执行 History 增量同步；resync 时才可能回源 Gmail，且不阻塞切换。
      void preloadMailboxSnapshot(primary, rangeDays, true, { skipLiveGmail: true }).catch(() => undefined);
      void silentSyncInbox(rangeDays, primary);
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
    async loadInboxEmails(category = "inbox", days = 30, force = false) {
      // 强制刷新时统一预热 All mail 快照
      if (force && (category === "inbox" || category === "all")) {
        return preloadMailboxSnapshot(undefined, days, true);
      }
      return loadInboxEmails(undefined, category, days, force);
    },
    refreshInboxEmails,
    silentSyncInbox,
    clearInboxCacheAndReload,
    expandInboxFeedWindow,
    async loadCachedInboxEmails(category = "inbox", days = 30, offset = 0, append = false): Promise<InboxPageResult> {
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
        const messages = Array.isArray(cached.messages) ? cached.messages : [];
        const count = messages.length;
        return {
          ok: true,
          count,
          hasMore: Boolean(cached.has_more),
          nextOffset: Number(cached.next_offset ?? offset + count),
          messages,
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
        // 加载更多固定扩 All mail 缓存；分类由前端标签投影
        const page = await client.listGmailEmailsPage(
          mailbox,
          days,
          100,
          "all",
          pageToken,
          pageOffset,
          excludeMessageIds,
        );
        if (requestId !== inboxRequestSequence.current) {
          return { ok: false, count: 0, hasMore: false, pageToken, pageOffset };
        }
        applyInboxSnapshotPayload(page, { append: true });
        const messages = Array.isArray(page.messages) ? page.messages : [];
        const count = messages.length;
        return {
          ok: true,
          count,
          hasMore: Boolean(page.has_more),
          pageToken: String(page.page_token || ""),
          pageOffset: Number(page.page_offset || 0),
          messages,
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
    preloadMailboxSnapshot: (force = false) =>
      preloadMailboxSnapshot(
        undefined,
        clampInboxSettings(state.inboxSettings).display_range_days,
        force,
      ),
    async loadInboxEmailBody(messageId, mailboxOverride) {
      const mailbox = normalizedMailbox(mailboxOverride || state.selectedMailboxes[0] || state.mailbox);
      if (!messageId || !mailbox) return "";
      const payload = await client.getInboxEmail(mailbox, messageId);
      return String(payload.message?.body_text || payload.message?.body_preview || payload.message?.snippet || "");
    },
    async loadInboxThreadPage(mailbox, threadId, options = {}) {
      const normalized = normalizedMailbox(mailbox);
      const page = await client.getInboxThreadPage(normalized, threadId, options);
      const attachmentCount = (page.messages || []).reduce(
        (count, message) => Math.max(count, Array.isArray(message.attachments) ? message.attachments.length : 0),
        0,
      );
      if (attachmentCount > 0) {
        setState((s) => ({
          ...s,
          inboxMessages: patchInboxThreadAttachmentSummary(s.inboxMessages, normalized, threadId, attachmentCount),
          inboxSnapshotMessages: patchInboxThreadAttachmentSummary(s.inboxSnapshotMessages, normalized, threadId, attachmentCount),
        }));
        for (const [key, cached] of inboxFeedCache.current) {
          if (!key.startsWith(`${normalized}|`)) continue;
          inboxFeedCache.current.set(key, {
            ...cached,
            payload: {
              ...cached.payload,
              messages: patchInboxThreadAttachmentSummary(cached.payload.messages || [], normalized, threadId, attachmentCount),
            },
          });
        }
      }
      return page;
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
      return loadInboxThreadDrafts(mailbox, limit);
    },
    async saveInboxThreadDraft(mailbox, threadId, body, bodyHtml, ifMatch, message, attachments) {
      const normalized = normalizedMailbox(mailbox);
      const result = await client.saveInboxThreadDraft(normalized, threadId, body, bodyHtml, ifMatch, message, attachments);
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
    async beginStageOutgoingAttachment(mailbox, args) {
      return client.beginStageOutgoingAttachment(normalizedMailbox(mailbox), args);
    },
    async completeStageOutgoingAttachment(mailbox, storageKey, size, mimeType) {
      return client.completeStageOutgoingAttachment(normalizedMailbox(mailbox), storageKey, size, mimeType);
    },
    async deleteStagedOutgoingAttachment(mailbox, storageKey) {
      await client.deleteStagedOutgoingAttachment(normalizedMailbox(mailbox), storageKey);
    },
    async prepareStagedOutgoingAttachmentAccess(mailbox, storageKey, filename, mimeType) {
      return client.prepareStagedOutgoingAttachmentAccess(
        normalizedMailbox(mailbox),
        storageKey,
        filename,
        mimeType,
      );
    },
    async modifyInboxMessageLabels(mailbox, messageIds, addLabelIds = [], removeLabelIds = []) {
      // Gmail 成功后再改本地 label 样式，避免乐观更新与远端不一致
      const normalized = normalizedMailbox(mailbox);
      if (!normalized || !messageIds.length) return;
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
          starred: labels.has("STARRED"),
        };
      };
      const result = await client.modifyMessageLabels(normalized, messageIds, addLabelIds, removeLabelIds);
      if (!result.ok) throw new Error(result.error || "Failed to modify message labels");
      setState((s) => ({
        ...s,
        inboxMessages: patchInboxMessageList(s.inboxMessages, messageIds, patchMessage),
        inboxSnapshotMessages: patchInboxMessageList(s.inboxSnapshotMessages, messageIds, patchMessage),
      }));
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
      if (!state.runtime.connected) {
        showToast("LLM is offline. Please try again when it reconnects.");
        return null;
      }
      if (
        !context?.mailbox ||
        (context.kind === "gmail_thread" && !context.thread_id) ||
        !request.visiblePrompt.trim() ||
        aiGenerationRun.current
      ) return null;
      const generationRun = {
        runId: createId("generation"),
        cancelled: false,
        controller: new AbortController(),
      };
      aiGenerationRun.current = generationRun;
      const isCurrentGeneration = () => aiGenerationRun.current === generationRun && !generationRun.cancelled;
      const conversationId = request.forceNewConversation ? createId("chat") : state.aiChatConversationId || createId("chat");
      const userMessage: AiChatMessage = request.retryUserMessage ?? {
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
      const requestedArtifact = request.expectedArtifact
        || (context.kind === "compose"
          ? "compose_draft"
          : (/\bsummar(?:ize|ise|y|ization|isation)\b/i.test(request.visiblePrompt) ? "summary" : "draft_reply"));
      const isDraftRequest = requestedArtifact === "draft_reply" || requestedArtifact === "send_plan" || requestedArtifact === "compose_draft";
      const thinkingStartedAt = new Date().toISOString();
      const pendingMessage: AiChatMessage = {
        id: createId("msg"),
        role: "assistant",
        content: "Thinking...",
        timestamp: thinkingStartedAt,
        thinkingStartedAt,
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
        const started = context.kind === "compose"
          ? await client.startComposeMailPrompt({
              mailbox: normalizedMailbox(context.mailbox),
              draft: {
                recipients: context.recipients,
                subject: context.subject,
                body: context.body,
              },
              visible_prompt: request.visiblePrompt,
              expected_artifact: requestedArtifact,
              ai_provider: state.llmProvider,
              storage_provider: state.storageProvider,
              run_id: generationRun.runId,
            })
          : await (async () => {
              const mailbox = normalizedMailbox(context.mailbox);
              const scanScope = await loadScanPlanForRun(mailbox);
              // 详情页与侧栏共用 start_ai_turn；artifact 类型是显式前端意图，
              // 不再由后端 Router 根据提示词关键词猜测。
              const uiContext = {
                ...buildAiTurnUiContext({
                  mailbox,
                  selectedMailboxes: state.selectedMailboxes,
                  conversationId,
                  scanPlan: state.scanPlan,
                  displayRangeDays: state.inboxSettings?.display_range_days,
                  currentMailContext: context,
                  languageHint: prefersChinese(request.visiblePrompt) ? "zh" : "en",
                  messages: messagesWithUser,
                }),
                requested_artifact: requestedArtifact,
              };
              return client.startAiTurn({
                user_text: buildRevisionPrompt(request.visiblePrompt, request.draftToRevise),
                mailbox,
                ui_context: uiContext,
                conversation_id: conversationId,
                primary_count: scanScope.max_messages,
                max_messages: scanScope.max_messages,
                scan_window_days: scanScope.scan_window_days,
                ai_provider: state.llmProvider,
                storage_provider: state.storageProvider,
                run_id: generationRun.runId,
                // 首次 invoke 在平台 60 秒边界前返回 run_id，剩余阶段走轮询。
                wait_timeout_seconds: 45,
              });
            })();
        if (!isCurrentGeneration()) return null;
        const completed = started.status === "done" && started.result
          ? started
          : await waitForToolRunResult(client, started.run_id || "", undefined, generationRun.controller.signal);
        if (!isCurrentGeneration()) return null;
        if (completed.status === "failed" || completed.error) {
          throw new Error(completed.error || "Mail prompt failed");
        }
        const payload = (completed.result || {}) as unknown as MailPromptRunResult;
        const summaryTitle = String(payload.thread_title || request.contextTitle || "").trim();
        const finalMessages: AiChatMessage[] = [
          ...messagesWithUser,
          {
            id: createId("msg"),
            role: "assistant",
            content: payload.assistant_text || "I reviewed the thread.",
            timestamp: new Date().toISOString(),
            thinkingStartedAt: pendingMessage.thinkingStartedAt,
            kind: "mail_context",
            artifact: isDraftRequest && payload.artifact
              ? { ...payload.artifact, source_prompt: request.visiblePrompt }
              : null,
            replyGaps: context.kind === "compose" ? payload.compose_gaps : payload.reply_gaps,
            mailContext: context,
            mailSummaryLink: !isDraftRequest && context.kind === "gmail_thread" ? {
              label: summaryTitle || "Current thread",
              mailbox: normalizedMailbox(context.mailbox),
              thread_id: context.thread_id,
              message_id: context.anchor_message_id || context.latest_message_id,
            } : undefined,
            fallbackUsed: Boolean(payload.fallback_used),
            sourcePrompt: request.visiblePrompt,
            assistantFollowupText: payload.assistant_followup_text,
          },
        ];
        upsertAiConversationHistory(conversationId, finalMessages, { kind: "chat", query: request.visiblePrompt });
        return payload;
      } catch (error) {
        if (!isCurrentGeneration() || isAbortError(error)) return null;
        const message = sanitizeToolError(error, request.visiblePrompt) || (isDraftRequest ? "Anna couldn't finish that draft." : "Anna couldn't finish that summary.");
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
    async sendInboxThreadReply({
      mailbox,
      threadId,
      to,
      body,
      bodyHtml,
      cc,
      bcc,
      replyMode = "reply_to_sender",
      dryRun = false,
      attachments,
    }) {
      return client.replyFromAsk({
        mailbox: normalizedMailbox(mailbox),
        thread_id: threadId,
        to_addr: to,
        body,
        body_html: bodyHtml,
        cc_addr: (cc || []).filter(Boolean).join(", "),
        bcc_addr: (bcc || []).filter(Boolean).join(", "),
        reply_mode: replyMode,
        dry_run: dryRun,
        attachments: attachments || [],
      });
    },
    async searchComposeContacts(mailbox, query) {
      const result = await client.searchComposeContacts(normalizedMailbox(mailbox), query, 10, state.storageProvider);
      return { contacts: result.contacts || [], permissionRequired: Boolean(result.permission_required) };
    },
    async listComposeDrafts(mailbox) {
      return client.listComposeDrafts(normalizedMailbox(mailbox), 100, state.storageProvider);
    },
    async saveComposeDraft(mailbox, draft, ifMatch) {
      const result = await client.saveComposeDraft(normalizedMailbox(mailbox), draft, ifMatch, state.storageProvider);
      return { ...result.draft, etag: result.etag || result.draft.etag };
    },
    async deleteComposeDraft(mailbox, draftId) {
      await client.deleteComposeDraft(normalizedMailbox(mailbox), draftId, state.storageProvider);
    },
    async sendComposeEmails(mailbox, messages) {
      const result = await client.sendComposeEmails(normalizedMailbox(mailbox), messages, state.storageProvider);
      return result.results || [];
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
      // 先等 Gmail 成功再改本地样式，避免 STARS/TODOS 时间线仅前端生效、重同步后还原
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
      const result = await client.setMessageStarred(mailbox, messageId, starred);
      if (!result.ok) throw new Error(result.error || "Failed to update star");
      setState((s) => ({
        ...s,
        inboxMessages: s.inboxMessages.map((item) => patchMessage(item, starred) || item),
        inboxSnapshotMessages: s.inboxSnapshotMessages.map((item) => patchMessage(item, starred) || item),
      }));
    },
    async markInboxRead(messageId) {
      // 同时查 snapshot：STARS/TODOS 邮件可能不在 inboxMessages（无 INBOX 标签）
      const message = state.inboxMessages.find((item) => item.id === messageId)
        || state.inboxSnapshotMessages.find((item) => item.id === messageId);
      const mailbox = normalizedMailbox(message?.mailbox || state.selectedMailboxes[0] || state.mailbox);
      if (!messageId || !mailbox) return;
      try {
        const result = await client.markReadFromAsk(mailbox, [messageId]);
        if (!result.ok) throw new Error(result.error || "Failed to mark email as read");
        setState((s) => ({
          ...s,
          inboxMessages: s.inboxMessages.map((item) => item.id === messageId
            ? { ...item, unread: false, label_ids: (item.label_ids || []).filter((label) => String(label).toUpperCase() !== "UNREAD") }
            : item),
          inboxSnapshotMessages: s.inboxSnapshotMessages.map((item) => item.id === messageId
            ? { ...item, unread: false, label_ids: (item.label_ids || []).filter((label) => String(label).toUpperCase() !== "UNREAD") }
            : item),
        }));
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
        throw error;
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
    async batchInboxActions(messageIds, action) {
      const ids = Array.from(new Set((messageIds || []).map(String).filter(Boolean))).slice(0, 50);
      if (!ids.length) return { ok: false, count: 0 };
      // 按 mailbox 分组（多账号场景）；无本地副本时回退当前邮箱，避免 STARS/TODOS 批量静默失败
      const byMailbox = new Map<string, string[]>();
      const fallbackMailbox = normalizedMailbox(state.selectedMailboxes[0] || state.mailbox);
      for (const id of ids) {
        const message = state.inboxMessages.find((item) => item.id === id)
          || state.inboxSnapshotMessages.find((item) => item.id === id);
        const mailbox = normalizedMailbox(message?.mailbox || fallbackMailbox);
        if (!mailbox) continue;
        const list = byMailbox.get(mailbox) || [];
        list.push(id);
        byMailbox.set(mailbox, list);
      }
      if (!byMailbox.size) return { ok: false, count: 0 };

      const prevInbox = state.inboxMessages;
      const prevSnapshot = state.inboxSnapshotMessages;
      const idSet = new Set(ids);
      const originalById = new Map<string, InboxMessage>();
      for (const message of [...prevSnapshot, ...prevInbox]) {
        if (idSet.has(message.id)) originalById.set(message.id, message);
      }

      const idsWithOriginalLabel = (messageIds: string[], label: string) =>
        messageIds.filter((id) => {
          const original = originalById.get(id);
          const hasLabel = (original?.label_ids || []).some(
            (item) => String(item).toUpperCase() === label,
          );
          return (
            hasLabel ||
            (label === "UNREAD" && Boolean(original?.unread)) ||
            (label === "STARRED" && Boolean(original?.starred))
          );
        });

      const patchLocal = (message: InboxMessage) => {
        if (!idSet.has(message.id)) return message;
        const labels = new Set((message.label_ids || []).map((label) => String(label).toUpperCase()));
        if (action === "mark_read") labels.delete("UNREAD");
        if (action === "mark_unread") labels.add("UNREAD");
        if (action === "star") labels.add("STARRED");
        if (action === "unstar") labels.delete("STARRED");
        if (action === "archive" || action === "mark_done") labels.delete("INBOX");
        if (action === "trash") {
          labels.delete("INBOX");
          labels.add("TRASH");
        }
        return {
          ...message,
          label_ids: [...labels],
          unread: labels.has("UNREAD"),
          starred: labels.has("STARRED"),
          important: labels.has("IMPORTANT"),
        };
      };

      // Gmail 成功后再改本地样式；失败保持原状
      try {
        for (const [mailbox, groupIds] of byMailbox) {
          if (action === "trash") {
            const result = await client.trashFromAsk(mailbox, groupIds);
            if (!result.ok) throw new Error(result.error || "Failed to trash messages");
          } else if (action === "mark_read") {
            const result = await client.markReadFromAsk(mailbox, groupIds);
            if (!result.ok) throw new Error(result.error || "Failed to mark as read");
          } else if (action === "mark_unread") {
            const result = await client.modifyMessageLabels(mailbox, groupIds, ["UNREAD"], []);
            if (!result.ok) throw new Error(result.error || "Failed to mark as unread");
          } else if (action === "star") {
            const result = await client.modifyMessageLabels(mailbox, groupIds, ["STARRED"], []);
            if (!result.ok) throw new Error(result.error || "Failed to star");
          } else if (action === "unstar") {
            const result = await client.modifyMessageLabels(mailbox, groupIds, [], ["STARRED"]);
            if (!result.ok) throw new Error(result.error || "Failed to unstar");
          } else if (action === "archive" || action === "mark_done") {
            // 移出收件箱：去掉 INBOX；Done 额外 mark_read
            if (action === "mark_done") {
              const result = await client.markReadFromAsk(mailbox, groupIds);
              if (!result.ok) throw new Error(result.error || "Failed to mark as read");
            }
            const result = await client.modifyMessageLabels(mailbox, groupIds, [], ["INBOX"]);
            if (!result.ok) throw new Error(result.error || "Failed to archive");
          }
        }
        setState((s) => ({
          ...s,
          inboxMessages: action === "trash" || action === "archive" || action === "mark_done"
            ? s.inboxMessages.filter((item) => !idSet.has(item.id))
            : s.inboxMessages.map(patchLocal),
          inboxSnapshotMessages: s.inboxSnapshotMessages.map(patchLocal),
        }));
        const n = ids.length;
        const undo = async (): Promise<boolean> => {
          try {
            for (const [mailbox, groupIds] of byMailbox) {
              const restoreLabel = async (label: "UNREAD" | "STARRED") => {
                const originallyLabeled = idsWithOriginalLabel(groupIds, label);
                const originallyUnlabeled = groupIds.filter(
                  (id) => !originallyLabeled.includes(id),
                );
                if (originallyLabeled.length) {
                  const result = await client.modifyMessageLabels(mailbox, originallyLabeled, [label], []);
                  if (!result.ok) throw new Error(result.error || `Failed to restore ${label}`);
                }
                if (originallyUnlabeled.length) {
                  const result = await client.modifyMessageLabels(mailbox, originallyUnlabeled, [], [label]);
                  if (!result.ok) throw new Error(result.error || `Failed to restore ${label}`);
                }
              };

              if (action === "mark_read" || action === "mark_unread") {
                await restoreLabel("UNREAD");
              } else if (action === "star" || action === "unstar") {
                await restoreLabel("STARRED");
              } else if (action === "trash") {
                const result = await client.modifyMessageLabels(mailbox, groupIds, ["INBOX"], ["TRASH"]);
                if (!result.ok) throw new Error(result.error || "Failed to restore messages from trash");
              } else if (action === "archive" || action === "mark_done") {
                const result = await client.modifyMessageLabels(mailbox, groupIds, ["INBOX"], []);
                if (!result.ok) throw new Error(result.error || "Failed to restore messages to inbox");
                if (action === "mark_done") await restoreLabel("UNREAD");
              }
            }
            setState((current) => ({
              ...current,
              inboxMessages: prevInbox,
              inboxSnapshotMessages: prevSnapshot,
            }));
            showToast(`Restored ${n}.`);
            return true;
          } catch (error) {
            showToast(error instanceof Error ? error.message : String(error));
            return false;
          }
        };
        return { ok: true, count: n, undo };
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
        return { ok: false, count: 0 };
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
    async startScan(_reason = "manual", _mailboxOverride?: string) {
      // Brief 已下线：请用 AI 侧栏（organize / 需回复）完成扫描。
      showToast("Brief scan is retired. Use the AI sidebar to organize or find mail that needs a reply.");
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
      // Abort 只停前端消费；Host Agent run 必须显式 cancel，否则 RPC 仍会继续。
      const conversationId = state.aiChatConversationId;
      if (conversationId) {
        void cancelAiAgentTurn(conversationId).catch((error) => {
          console.warn("[agent.session] cancel failed:", error);
        });
      }
      setState((s) => {
        const stoppedAt = new Date().toISOString();
        const messages = s.aiChatMessages.map((message) => message.pending
          ? { ...message, pending: false, kind: "stopped" as const, content: "Generation stopped.", timestamp: stoppedAt }
          : message);
        const lastUser = [...messages].reverse().find((message) => message.role === "user");
        const chatId = s.aiChatConversationId;
        if (!lastUser || !chatId) {
          return { ...s, aiChatMessages: messages, aiChatLoading: false, isCustomScanning: false, scanStatus: "", customRunProgress: null };
        }
        const kind = lastUser.kind === "scan" ? "scan" as const : "chat" as const;
        const timestamp = stoppedAt;
        const entry: AskHistoryEntry = {
          conversationId: chatId,
          kind,
          query: lastUser.content,
          result: syntheticChatResult(messages),
          timestamp,
          messages,
        };
        const nextHistory = pruneAskHistoryEntries([
          entry,
          ...s.askHistory.filter((item) => item.conversationId !== chatId),
        ]);
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
      const previousConversationId = state.aiChatConversationId;
      const run = aiGenerationRun.current;
      if (run) {
        run.cancelled = true;
        run.controller.abort();
        aiGenerationRun.current = null;
      }
      if (previousConversationId) {
        void clearAiAgentSession(previousConversationId);
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
    resumeAiConversation(index: number) {
      const entry = state.askHistory[index];
      const pendingRun = entry?.pendingRun;
      const messages = entry?.messages || [];
      if (!pendingRun?.runId || state.isCustomScanning || state.aiChatLoading || aiGenerationRun.current) return;
      let lastUserIndex = -1;
      for (let messageIndex = messages.length - 1; messageIndex >= 0; messageIndex -= 1) {
        if (messages[messageIndex].role === "user") {
          lastUserIndex = messageIndex;
          break;
        }
      }
      if (lastUserIndex < 0) return;
      void actions.sendAiChatMessage({
        prompt: pendingRun.question || messages[lastUserIndex].content,
        baseMessages: messages.slice(0, lastUserIndex),
        retryUserMessage: messages[lastUserIndex],
        resumeRunId: pendingRun.runId,
      });
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
        void clearAiAgentSession(entry.conversationId || "");
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
    resolveAiClarification(messageId, actionId) {
      setState((s) => ({
        ...s,
        aiChatMessages: s.aiChatMessages.map((message) =>
          message.id === messageId && message.clarification?.status === "pending"
            ? {
                ...message,
                clarification: {
                  ...message.clarification,
                  status: "resolved",
                  resolved_action: actionId,
                },
              }
            : message,
        ),
      }));
    },
    async sendAiChatMessage(options = {}) {
      const userRequest = String(options.prompt ?? state.customScanInput).trim();
      if (!state.runtime.connected) {
        showToast("LLM is offline. Please try again when it reconnects.");
        return;
      }
      if (!userRequest || state.isCustomScanning || state.aiChatLoading || aiGenerationRun.current) return;
      const conversationId = state.aiChatConversationId || createId("chat");
      const baseMessages = options.baseMessages
        ?? (state.aiChatConversationId === conversationId ? state.aiChatMessages : []);
      // 侧栏选型交由 Host Agent；本地 start_ai_turn Router 不再参与该路径。
      const generationRun = {
        runId: createId("generation"),
        cancelled: false,
        controller: new AbortController(),
      };
      aiGenerationRun.current = generationRun;
      const isCurrentGeneration = () => aiGenerationRun.current === generationRun && !generationRun.cancelled;
      const userMessage: AiChatMessage = options.retryUserMessage ?? {
        id: createId("msg"),
        role: "user",
        content: userRequest,
        timestamp: new Date().toISOString(),
        kind: "chat",
      };
      const messagesWithUser = [...baseMessages, userMessage];
      const thinkingStartedAt = new Date().toISOString();
      const pendingMessage: AiChatMessage = {
        id: createId("msg"),
        role: "assistant",
        content: "thinking",
        timestamp: thinkingStartedAt,
        thinkingStartedAt,
        kind: "status",
        pending: true,
      };
      setState((s) => ({
        ...s,
        customScanInput: "",
        aiChatConversationId: conversationId,
        aiChatMessages: [...messagesWithUser, pendingMessage],
        aiChatLoading: true,
        isCustomScanning: true,
        scanError: "",
        scanStatus: "Understanding request...",
        customRunProgress: {
          runId: "",
          question: userRequest,
          status: "queued",
          stage: "routing",
          stageKey: "planning",
          progress: {},
          partial: {},
          startedAt: "",
        },
      }));
      let runId = "";
      try {
        const scanMailbox = selectedOrPrimary(state.selectedMailboxes, state.mailbox);
        const uiContext = buildAiTurnUiContext({
          mailbox: state.mailbox,
          selectedMailboxes: state.selectedMailboxes,
          conversationId,
          scanPlan: state.scanPlan,
          displayRangeDays: state.inboxSettings?.display_range_days,
          currentMailContext: options.currentMailContext,
          languageHint: prefersChinese(userRequest) ? "zh" : "en",
          messages: messagesWithUser,
          savedPromptId: options.savedPromptId,
          selectedThreads: options.selectedThreads,
          routingIntent: options.routingIntent,
          inboxListContext: options.inboxListContext,
        });
        let streamed = "";
        const agentTurn = await runAiAgentTurn(
          state.runtime.client,
          conversationId,
          buildAiAgentContent(userRequest, uiContext),
          {
            signal: generationRun.controller.signal,
            onText: (piece) => {
              if (!isCurrentGeneration()) return;
              streamed += piece;
              const snapshot = stripTerminalDoneMarker(streamed);
              setState((s) => ({
                ...s,
                aiChatMessages: s.aiChatMessages.map((message) => message.id === pendingMessage.id
                  ? { ...message, content: snapshot || "…", kind: "chat", pending: true }
                  : message),
                scanStatus: "Generating reply...",
                customRunProgress: {
                  runId: "agent_session",
                  question: userRequest,
                  status: "running",
                  stage: "agent",
                  stageKey: "answer",
                  progress: {},
                  partial: {},
                  startedAt: thinkingStartedAt,
                },
              }));
            },
            onToolOutcome: (outcome) => {
              if (!isCurrentGeneration()) return;
              const bgRunId = String(outcome.run_id || "").trim();
              if (bgRunId) runId = bgRunId;
              const stage = String(outcome.stage || "");
              const scanQ = String(
                outcome.scan_query
                || (outcome.progress && typeof outcome.progress === "object"
                  ? (outcome.progress as Record<string, unknown>).scan_query
                  : "")
                || "",
              ).trim();
              if (stage || scanQ) {
                setState((s) => ({
                  ...s,
                  scanStatus: stage ? `Working: ${stage}` : s.scanStatus,
                  customRunProgress: {
                    runId: bgRunId || s.customRunProgress?.runId || "agent_session",
                    question: userRequest,
                    status: "running",
                    stage: stage || s.customRunProgress?.stage || "agent",
                    stageKey: stage === "search" ? "search" : stage === "plan" ? "planning" : "answer",
                    progress: {
                      ...(typeof outcome.progress === "object" && outcome.progress ? outcome.progress as object : {}),
                      ...(scanQ ? { scan_query: scanQ } : {}),
                    },
                    partial: {},
                    startedAt: thinkingStartedAt,
                  },
                }));
              }
            },
          },
        );
        // Host 若误调 start_ai_turn：工具只返回 running，需前端轮询 get_mail_agent_run 拿最终 result/scan_query。
        let toolOutcomes = agentTurn.toolOutcomes;
        const backgroundRunId = findBackgroundRunId(toolOutcomes) || runId;
        if (backgroundRunId && client) {
          runId = backgroundRunId;
          let lastStatus: RunStatus | null = null;
          for (let poll = 0; poll < 80; poll += 1) {
            if (!isCurrentGeneration() || generationRun.controller.signal.aborted) break;
            lastStatus = await client.getRun(backgroundRunId);
            if (!isCurrentGeneration()) break;
            const stage = String(lastStatus.stage || "");
            const progress = (lastStatus.progress || {}) as Record<string, unknown>;
            const scanQ = String(progress.scan_query || "").trim();
            setState((s) => ({
              ...s,
              scanStatus: stage ? `Working: ${stage}` : s.scanStatus,
              customRunProgress: {
                runId: backgroundRunId,
                question: userRequest,
                status: String(lastStatus?.status || "running"),
                stage: stage || "agent",
                stageKey: stage === "search" ? "search" : stage === "plan" || stage === "routing" ? "planning" : "answer",
                progress: { ...progress, ...(scanQ ? { scan_query: scanQ } : {}) },
                partial: (lastStatus?.partial || {}) as Record<string, unknown>,
                startedAt: thinkingStartedAt,
              },
            }));
            if (
              lastStatus.status === "done"
              || lastStatus.status === "failed"
              || lastStatus.needs_continue === false
            ) {
              break;
            }
            await abortableSleep(POLL_INTERVAL_MS, generationRun.controller.signal);
          }
          if (lastStatus?.status === "failed") {
            throw new Error(formatRunDiagnostics(lastStatus, lastStatus.error || "AI turn failed"));
          }
          if (lastStatus?.result && typeof lastStatus.result === "object") {
            toolOutcomes = [
              ...toolOutcomes,
              {
                kind: String((lastStatus.result as Record<string, unknown>).kind || "scan"),
                status: "done",
                run_id: backgroundRunId,
                result: lastStatus.result as Record<string, unknown>,
                scan_query: String((lastStatus.result as Record<string, unknown>).scan_query || "").trim(),
                scan_source: "cache",
                assistant_text: String(
                  (lastStatus.result as Record<string, unknown>).assistant_text
                  || (lastStatus.result as Record<string, unknown>).summary
                  || "",
                ),
              },
            ];
          }
        }
        const completed = {
          status: "done",
          error: "",
          result: agentOutcomePayload(toolOutcomes, agentTurn.text || streamed),
        } as Pick<RunStatus, "status" | "error" | "result">;
        if (!isCurrentGeneration()) return;
        if (completed.status === "failed" || completed.error) {
          throw new Error(formatRunDiagnostics(completed, completed.error || "AI turn failed"));
        }
        const payload = (completed.result || {}) as Record<string, unknown>;
        const kind = String(payload.kind || "chat");
        const assistantText = stripTerminalDoneMarker(
          String(payload.assistant_text || payload.summary || "").trim(),
        )
          || "";
        const { scanQuery, scanSource } = payloadScanQuery(payload);
        // 仅真实检索过才写入消息，驱动 Thinking 后可点 query chip
        const scanFields = scanQuery
          ? { scanQuery, scanSource: scanSource || "cache" }
          : {};

        const parseMailContext = (): AiMailContextRef | undefined => {
          const mailCtx = payload.mail_context && typeof payload.mail_context === "object"
            ? payload.mail_context as Record<string, unknown>
            : null;
          if (mailCtx && String(mailCtx.kind) === "thread") {
            return {
              kind: "gmail_thread",
              mailbox: String(mailCtx.mailbox || scanMailbox),
              thread_id: String(mailCtx.thread_id || ""),
              anchor_message_id: String(mailCtx.message_id || ""),
              latest_message_id: String(mailCtx.message_id || ""),
            };
          }
          return options.currentMailContext || undefined;
        };

        const parseDraftReply = (raw: unknown): import("../types/mail").DraftReplyArtifact | null => {
          if (!raw || typeof raw !== "object") return null;
          const art = raw as Record<string, unknown>;
          if (String(art.type || "") !== "draft_reply") return null;
          return {
            type: "draft_reply",
            mailbox: String(art.mailbox || scanMailbox),
            thread_id: String(art.thread_id || ""),
            body: String(art.body || ""),
            source_prompt: String(art.source_prompt || userRequest),
            message_id: String(art.message_id || "") || undefined,
            subject: String(art.subject || "") || undefined,
          };
        };

        const parseArtifact = (): AiChatMessage["artifact"] => {
          const raw = payload.artifact;
          if (!raw || typeof raw !== "object") return null;
          const art = raw as Record<string, unknown>;
          const type = String(art.type || "");
          if (type === "draft_reply") {
            return parseDraftReply(raw);
          }
          if (type === "compose_draft") {
            return {
              type: "compose_draft",
              mailbox: String(art.mailbox || scanMailbox),
              body: String(art.body || ""),
              source_prompt: String(art.source_prompt || userRequest),
              mode: String(art.mode || "insert") === "replace" ? "replace" : "insert",
              subject: String(art.subject || "") || undefined,
            };
          }
          return null;
        };

        const parseArtifacts = (): import("../types/mail").DraftReplyArtifact[] | undefined => {
          const rawList = payload.artifacts;
          if (!Array.isArray(rawList)) return undefined;
          const list = rawList
            .map((item) => parseDraftReply(item))
            .filter((item): item is import("../types/mail").DraftReplyArtifact => Boolean(item && item.body));
          return list.length ? list : undefined;
        };

        const parseClarification = (): AiClarificationPayload | undefined => {
          const raw = payload.clarification;
          if (!raw || typeof raw !== "object") return undefined;
          const value = raw as Record<string, unknown>;
          const actions = Array.isArray(value.actions)
            ? value.actions
              .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
              .map((item) => ({ id: String(item.id || ""), label: String(item.label || "") }))
              .filter((item) => item.id && item.label)
              .slice(0, 4)
            : [];
          return {
            original_input: String(value.original_input || userRequest),
            question: String(value.question || assistantText),
            actions,
            freeform_enabled: Boolean(value.freeform_enabled),
            status: "pending",
          };
        };

        if (kind === "scan") {
          await loadActiveCards();
          if (!isCurrentGeneration()) return;
          await loadRunHistory();
          if (!isCurrentGeneration()) return;
          await loadCustomPlans();
          if (!isCurrentGeneration()) return;
          const result = buildCustomRunResult(runId, {
            ...payload,
            ...(scanQuery ? { scan_query: scanQuery, scan_source: scanSource || "cache" } : {}),
          });
          const finalMessages: AiChatMessage[] = [
            ...messagesWithUser,
            {
              ...pendingMessage,
              pending: false,
              kind: "scan",
              content: result.summary || result.plan_description || assistantText,
              result,
              sourcePrompt: userRequest,
              timestamp: new Date().toISOString(),
              ...scanFields,
            },
          ];
          upsertAiConversationHistory(conversationId, finalMessages, { kind: "scan", query: userRequest, result });
        } else if (kind === "clarify") {
          const finalMessages: AiChatMessage[] = [
            ...messagesWithUser,
            {
              ...pendingMessage,
              pending: false,
              kind: "clarify",
              content: assistantText,
              clarification: parseClarification(),
              timestamp: new Date().toISOString(),
            },
          ];
          upsertAiConversationHistory(conversationId, finalMessages, { kind: "chat", query: userRequest });
        } else if (kind === "draft") {
          const artifacts = parseArtifacts();
          const primary = parseArtifact() || (artifacts && artifacts[0]) || null;
          const finalMessages: AiChatMessage[] = [
            ...messagesWithUser,
            {
              ...pendingMessage,
              pending: false,
              kind: "draft",
              content: assistantText,
              artifact: primary,
              artifacts,
              mailContext: parseMailContext(),
              sourcePrompt: userRequest,
              timestamp: new Date().toISOString(),
              ...scanFields,
            },
          ];
          upsertAiConversationHistory(conversationId, finalMessages, { kind: "chat", query: userRequest });
        } else if (kind === "propose") {
          const raw = payload.proposed_actions;
          const proposed = raw && typeof raw === "object" ? raw as Record<string, unknown> : null;
          const itemsRaw = proposed && Array.isArray(proposed.items) ? proposed.items : [];
          const finalMessages: AiChatMessage[] = [
            ...messagesWithUser,
            {
              ...pendingMessage,
              pending: false,
              kind: "propose",
              content: assistantText,
              proposedActions: proposed
                ? {
                    step_index: Number(proposed.step_index || 1),
                    step_title: String(proposed.step_title || ""),
                    rationale: String(proposed.rationale || ""),
                    primary_action: String(proposed.primary_action || "mark_done"),
                    allowed_actions: Array.isArray(proposed.allowed_actions)
                      ? proposed.allowed_actions.map(String)
                      : ["mark_done", "archive", "trash"],
                    items: itemsRaw
                      .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
                      .map((item) => ({
                        mailbox: String(item.mailbox || scanMailbox),
                        message_id: String(item.message_id || ""),
                        thread_id: String(item.thread_id || ""),
                        subject: String(item.subject || ""),
                        default_selected: item.default_selected !== false,
                      })),
                    requires_user_confirmation: true,
                    followup_after_apply: String(proposed.followup_after_apply || ""),
                    followup_after_skip: String(proposed.followup_after_skip || ""),
                    followup_after_dismiss: String(proposed.followup_after_dismiss || ""),
                    continue_prompt: String(proposed.continue_prompt || ""),
                    language: String(proposed.language || "") || undefined,
                    recommendation_groups: Array.isArray(proposed.recommendation_groups)
                      ? proposed.recommendation_groups
                        .filter((g): g is Record<string, unknown> => Boolean(g) && typeof g === "object")
                        .map((g) => ({
                          title: String(g.title || ""),
                          subjects: Array.isArray(g.subjects) ? g.subjects.map(String) : [],
                          items: Array.isArray(g.items)
                            ? g.items
                              .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
                              .map((item) => ({
                                mailbox: String(item.mailbox || scanMailbox),
                                message_id: String(item.message_id || ""),
                                thread_id: String(item.thread_id || ""),
                                subject: String(item.subject || ""),
                              }))
                            : undefined,
                        }))
                      : undefined,
                  }
                : null,
              timestamp: new Date().toISOString(),
              ...scanFields,
            },
          ];
          upsertAiConversationHistory(conversationId, finalMessages, { kind: "chat", query: userRequest });
        } else if (kind === "mail_context") {
          const finalMessages: AiChatMessage[] = [
            ...messagesWithUser,
            {
              ...pendingMessage,
              pending: false,
              kind: "mail_context",
              content: assistantText,
              mailContext: parseMailContext(),
              timestamp: new Date().toISOString(),
              ...scanFields,
            },
          ];
          upsertAiConversationHistory(conversationId, finalMessages, { kind: "chat", query: userRequest });
        } else {
          // search 工具本身不是最终消息类型；Host 回答后归为 chat，并保留 scan chip
          const messageKind =
            kind === "memory" ? "memory" : kind === "error" ? "error" : "chat";
          const finalMessages: AiChatMessage[] = [
            ...messagesWithUser,
            {
              ...pendingMessage,
              pending: false,
              kind: messageKind,
              content: assistantText,
              timestamp: new Date().toISOString(),
              ...scanFields,
            },
          ];
          upsertAiConversationHistory(conversationId, finalMessages, { kind: "chat", query: userRequest });
        }
        setState((s) => ({ ...s, scanStatus: "", customRunProgress: null }));
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
        upsertAiConversationHistory(conversationId, failedMessages, {
          kind: "chat",
          query: userRequest,
          pendingRun: runId && isTransientConnectionError(error)
            ? { runId, question: userRequest }
            : undefined,
        });
        setState((s) => ({
          ...s,
          scanError: message,
          customRunProgress: s.customRunProgress
            ? { ...s.customRunProgress, status: "failed", stageKey: "failed" }
            : s.customRunProgress,
        }));
        showToast(message);
      } finally {
        if (aiGenerationRun.current === generationRun) {
          aiGenerationRun.current = null;
          setState((s) => ({ ...s, isCustomScanning: false, aiChatLoading: false }));
        }
      }
    },
    retryAiMessage(messageId) {
      if (!state.runtime.connected) {
        showToast("LLM is offline. Please try again when it reconnects.");
        return;
      }
      if (state.isCustomScanning || state.aiChatLoading || aiGenerationRun.current) return;
      const errorIndex = state.aiChatMessages.findIndex((message) => message.id === messageId && message.kind === "error");
      if (errorIndex < 0) return;
      const userIndex = state.aiChatMessages
        .slice(0, errorIndex)
        .map((message, index) => ({ message, index }))
        .reverse()
        .find(({ message }) => message.role === "user")?.index;
      if (userIndex === undefined) {
        showToast("This message can't be retried.");
        return;
      }
      const userMessage = state.aiChatMessages[userIndex];
      void actions.sendAiChatMessage({
        prompt: userMessage.content,
        currentMailContext: userMessage.mailContext,
        baseMessages: state.aiChatMessages.slice(0, userIndex),
        retryUserMessage: userMessage,
      });
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
        const scanMailbox = selectedOrPrimary(state.selectedMailboxes, state.mailbox);
        const scanScope = await loadScanPlanForRun(scanMailbox);
        const runId = `cs_${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`;
        activeBackgroundRunIdRef.current = runId;
        const scanPromise = client.startCustomScan({
          user_request: userRequest,
          mailbox: scanMailbox,
          primary_count: scanScope.max_messages,
          max_messages: scanScope.max_messages,
          scan_window_days: scanScope.scan_window_days,
          ai_provider: state.llmProvider,
          storage_provider: state.storageProvider,
          run_id: runId,
          wait_timeout_seconds: 60,
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
      } catch (error) {
        const message = sanitizeToolError(error, userRequest);
        setState((s) => ({ ...s, scanError: message, customRunProgress: s.customRunProgress ? { ...s.customRunProgress, status: "failed", stageKey: "failed" } : s.customRunProgress }));
        if (!isTransientConnectionError(error)) showToast(message);
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
        const scanMailbox = selectedOrPrimary(state.selectedMailboxes, state.mailbox);
        const scanScope = await loadScanPlanForRun(scanMailbox);
        const runId = `rr_${crypto.randomUUID().replace(/-/g, "").slice(0, 12)}`;
        activeBackgroundRunIdRef.current = runId;
        const scanPromise = client.reRunCustomScan({
          plan_id: planId,
          mailbox: scanMailbox,
          primary_count: scanScope.max_messages,
          max_messages: scanScope.max_messages,
          scan_window_days: scanScope.scan_window_days,
          ai_provider: state.llmProvider,
          storage_provider: state.storageProvider,
          run_id: runId,
          wait_timeout_seconds: 60,
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
        if (!isTransientConnectionError(error)) showToast(message);
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
      try {
        await actions.resetMailboxScanHistory(normalized);
        setState((s) => ({
          ...s,
          isPreparingScan: false,
          isScanning: false,
          cards: [],
          allCards: [],
          scanState: null,
          cleanupBundle: null,
          actionCount: 0,
          scanStatus: "",
        }));
        showToast("Brief data cleared. Use the AI sidebar for inbox scan.");
      } catch (error) {
        setState((s) => ({ ...s, isPreparingScan: false }));
        throw error;
      }
    },
    async deleteMailboxData(mailbox) {
      try {
        const normalized = normalizedMailbox(mailbox);
        const result = await client.deleteMailboxData(normalized);
        if (result.ok) {
          await clearMailboxDatabase(normalized);
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
    async applyProposedActions({ action, items }) {
      try {
        const result = await client.applyProposedActions({ action, items });
        if (!result.success && result.error) {
          showToast(result.error);
          return { success: false };
        }
        const count = items.length;
        // 文案语言由调用方 toast 覆盖；此处保持中性短提示
        const label = action === "trash"
          ? (count === 1 ? "Moved 1 email to trash." : `Moved ${count} emails to trash.`)
          : action === "archive"
            ? (count === 1 ? "Archived 1 email." : `Archived ${count} emails.`)
            : (count === 1 ? "Marked 1 email as done." : `Marked ${count} emails as done.`);
        showToast(label);
        const localDone = result.local_done || (result.requires_local_done ? items : []);
        // 通知 Inbox 工作台写入本地 Done 标记（与 HomeView Done 语义对齐）
        if (typeof window !== "undefined" && localDone.length && (action === "mark_done" || action === "archive")) {
          window.dispatchEvent(new CustomEvent("anna-inbox-local-done", { detail: { items: localDone } }));
        }
        return {
          success: result.success !== false,
          local_done: localDone,
        };
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
        return { success: false };
      }
    },
    async listSavedPrompts() {
      try {
        const result = await client.listSavedPrompts();
        return (result.prompts || []).map((item) => ({
          id: String(item.id || ""),
          title: String(item.title || ""),
          body: String(item.body || ""),
        })).filter((item) => item.id);
      } catch {
        return [];
      }
    },
    async saveSavedPrompt(args) {
      try {
        const result = await client.saveSavedPrompt(args);
        if (!result.success) {
          showToast(result.error || "Failed to save prompt.");
          return false;
        }
        showToast("Prompt saved.");
        return true;
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
        return false;
      }
    },
    async deleteSavedPrompt(promptId) {
      try {
        await client.deleteSavedPrompt(promptId);
        showToast("Prompt deleted.");
        return true;
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
        return false;
      }
    },
    async listAiMemories() {
      try {
        const result = await client.listAiMemories();
        return (result.memories || []).map((item) => ({
          id: String(item.id || ""),
          text: String(item.text || ""),
        })).filter((item) => item.id);
      } catch {
        return [];
      }
    },
    async addAiMemory(text) {
      try {
        const result = await client.addAiMemory(text, "settings");
        if (!result.success) {
          showToast(result.error || "Failed to add memory.");
          return false;
        }
        showToast("Memory saved.");
        return true;
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
        return false;
      }
    },
    async deleteAiMemory(memoryId) {
      try {
        await client.deleteAiMemory(memoryId);
        showToast("Memory deleted.");
        return true;
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
        return false;
      }
    },
    refreshSamplingStatus,
    refreshGmailApiStatus,
    refreshConnectivityStatus,
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
