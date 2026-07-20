import type {
  AttachmentDownloadPayload,
  DraftReplyArtifact,
  InboxThreadMessage,
  MailAttachmentMeta,
  QuickReplySuggestion,
} from "../../types/mail";
import { triggerBrowserDownload } from "../../shared/browserDownload";
import {
  senderParts,
  splitAddresses,
  type AddressParts,
} from "../../shared/mailIdentity";

export { senderParts, splitAddresses };
export type { AddressParts };

export type AttachmentKind = "pdf" | "image" | "text" | "audio" | "video" | "download";

export interface ResolvedAttachmentAccess {
  kind: "url" | "blob";
  url: string;
  mimeType: string;
  filename: string;
  externalPreview: boolean;
  sourceUrl?: string;
  revoke?: () => void;
}

export interface SnoozePreset {
  id: string;
  label: string;
  at: Date;
}

export function parseMessageDate(value?: string) {
  if (!value) return null;
  const millis = Number(value);
  const date = Number.isFinite(millis) && millis > 0 ? new Date(millis) : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatAbsoluteDateTime(value?: string) {
  const date = parseMessageDate(value);
  if (!date) return "";
  return date.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function formatAttachmentSize(size?: number) {
  const bytes = Number(size || 0);
  if (bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

function attachmentExtension(filename: string) {
  const match = String(filename || "").trim().toLowerCase().match(/\.([a-z0-9]+)$/);
  return match?.[1] || "";
}

export function normalizeAttachmentKind(attachment: Pick<MailAttachmentMeta, "filename" | "mime_type">): AttachmentKind {
  const mime = String(attachment.mime_type || "").trim().toLowerCase();
  const extension = attachmentExtension(attachment.filename);
  const useExtensionFallback = !mime || mime === "application/octet-stream";
  if (mime === "application/pdf" || (useExtensionFallback && extension === "pdf")) return "pdf";
  if (
    mime.startsWith("image/")
    || (useExtensionFallback && ["png", "jpg", "jpeg", "gif", "webp"].includes(extension))
  ) return "image";
  if (
    mime.startsWith("text/")
    || mime === "application/json"
    || mime === "text/csv"
    || (useExtensionFallback && ["txt", "json", "csv", "log", "md"].includes(extension))
  ) return "text";
  if (
    mime.startsWith("audio/")
    || (useExtensionFallback && ["mp3", "wav"].includes(extension))
  ) return "audio";
  if (
    mime.startsWith("video/")
    || (useExtensionFallback && ["mp4", "webm"].includes(extension))
  ) return "video";
  return "download";
}

export function isPreviewableAttachment(attachment: MailAttachmentMeta) {
  return normalizeAttachmentKind(attachment) !== "download";
}

export function estimateAttachmentPreviewMemory(attachment: Pick<MailAttachmentMeta, "filename" | "mime_type" | "size">) {
  const megabyte = 1024 * 1024;
  const size = Math.max(0, Number(attachment.size || 0));
  switch (normalizeAttachmentKind(attachment)) {
    case "pdf":
      return Math.max(12 * megabyte, Math.min(48 * megabyte, size * 12));
    case "image":
      return Math.max(4 * megabyte, Math.min(32 * megabyte, size * 4));
    case "audio":
    case "video":
      return 4 * megabyte;
    case "text":
      return Math.min(size, 512 * 1024);
    default:
      return 0;
  }
}

export function resolveAttachmentAccess(payload: AttachmentDownloadPayload): ResolvedAttachmentAccess {
  const filename = String(payload.filename || "attachment");
  const mimeType = String(payload.mime_type || "application/octet-stream");
  if (payload.ok === false) {
    throw new Error(String(payload.error || "Attachment access failed."));
  }
  if (payload.delivery === "inline" && payload.content_b64) {
    const binary = window.atob(payload.content_b64);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    const blobUrl = URL.createObjectURL(new Blob([bytes], { type: mimeType }));
    return {
      kind: "blob",
      url: blobUrl,
      mimeType,
      filename,
      externalPreview: false,
      sourceUrl: "",
      revoke: () => URL.revokeObjectURL(blobUrl),
    };
  }
  const requestedMode = String(payload.mode || "download");
  const preferredUrl = requestedMode === "preview"
    ? String(payload.preview_url || payload.download_url || "")
    : String(payload.download_url || payload.preview_url || "");
  if (preferredUrl) {
    return {
      kind: "url",
      url: preferredUrl,
      mimeType,
      filename,
      externalPreview: requestedMode === "preview" && !payload.preview_url && Boolean(payload.download_url),
      sourceUrl: preferredUrl,
    };
  }
  throw new Error(String(payload.error || "Attachment content is unavailable."));
}

export async function materializeAttachmentAccess(access: ResolvedAttachmentAccess): Promise<ResolvedAttachmentAccess> {
  if (access.kind === "blob") return access;
  const response = await fetch(access.url);
  if (!response.ok) {
    throw new Error(`Attachment fetch failed with ${response.status}`);
  }
  const responseBlob = await response.blob();
  const blob = responseBlob.type || !access.mimeType
    ? responseBlob
    : new Blob([await responseBlob.arrayBuffer()], { type: access.mimeType });
  const blobUrl = URL.createObjectURL(blob);
  return {
    kind: "blob",
    url: blobUrl,
    mimeType: blob.type || access.mimeType,
    filename: access.filename,
    externalPreview: access.externalPreview,
    sourceUrl: access.sourceUrl || access.url,
    revoke: () => URL.revokeObjectURL(blobUrl),
  };
}

export function triggerAttachmentDownload(
  access: ResolvedAttachmentAccess,
  filename?: string,
  ownerDocument: Document = document,
) {
  triggerBrowserDownload(access.url, filename || access.filename || "attachment", ownerDocument);
}

export function deriveReplyToAddress(message: InboxThreadMessage | undefined, mailbox: string) {
  if (!message) return "";
  const normalizedMailbox = mailbox.trim().toLowerCase();
  const from = senderParts(message.from);
  if (from.email && from.email.toLowerCase() !== normalizedMailbox) return from.email;
  const recipients = splitAddresses(message.to).map(senderParts);
  return recipients.find((item) => item.email.toLowerCase() !== normalizedMailbox)?.email || from.email || "";
}

/** 界面语言：后期接中英文切换；当前先跟浏览器语言。 */
export function uiPrefersChinese() {
  if (typeof navigator === "undefined") return false;
  const lang = String(navigator.language || "").toLowerCase();
  return lang.startsWith("zh");
}

/** 生成转发主题：已有 Fwd:/转发：前缀则不重复添加。 */
export function buildForwardSubject(subject?: string) {
  const raw = String(subject || "").trim() || "(no subject)";
  if (/^(fwd|fw)\s*:/i.test(raw) || /^转发\s*[:：]/.test(raw)) return raw;
  return uiPrefersChinese() ? `转发：${raw}` : `Fwd: ${raw}`;
}

const FORWARD_BLOCK_RE =
  /(?:^|\n)(?:-{2,}\s*(?:Forwarded message|转发的邮件)\s*-{2,})[\s\S]*$/i;

/** 去掉正文中的自动转发引用块，保留用户写的说明。 */
export function stripForwardedMessageBlock(body: string) {
  return String(body || "").replace(FORWARD_BLOCK_RE, "").replace(/\s+$/u, "");
}

function forwardHeaderMeta(
  message: Pick<InboxThreadMessage, "from" | "to" | "cc" | "subject" | "internal_date">,
) {
  const zh = uiPrefersChinese();
  const header = zh ? "---------- 转发的邮件 ---------" : "---------- Forwarded message ---------";
  const labels = zh
    ? { from: "发件人", date: "日期", subject: "主题", to: "收件人", cc: "抄送" }
    : { from: "From", date: "Date", subject: "Subject", to: "To", cc: "Cc" };
  const date = formatAbsoluteDateTime(message.internal_date) || "";
  return {
    header,
    lines: [
      `${labels.from}: ${message.from || ""}`,
      date ? `${labels.date}: ${date}` : "",
      `${labels.subject}: ${message.subject || "(no subject)"}`,
      `${labels.to}: ${message.to || ""}`,
      message.cc ? `${labels.cc}: ${message.cc}` : "",
    ].filter(Boolean),
  };
}

function htmlToPlainFallback(html: string) {
  return String(html || "")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&amp;/gi, "&")
    .replace(/\s+\n/g, "\n")
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}

function escapeHtml(value: string) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function plainNoteToHtml(note: string) {
  const escaped = escapeHtml(note).replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  if (!escaped.trim()) return "";
  return escaped
    .split("\n")
    .map((line) => (line ? `<div>${line}</div>` : "<div><br></div>"))
    .join("");
}

/** 构造标准转发引用块（纯文本，不含用户说明；供编辑栏预览）。 */
export function buildForwardedMessageBlock(
  message: Pick<InboxThreadMessage, "from" | "to" | "cc" | "subject" | "internal_date" | "body_text" | "body_html"> | undefined,
) {
  if (!message) return "";
  const { header, lines } = forwardHeaderMeta(message);
  const bodyText = String(message.body_text || "").trim()
    || htmlToPlainFallback(String(message.body_html || ""));
  return [header, ...lines, "", bodyText].join("\n");
}

/** 用户说明 + 转发引用块（纯文本编辑栏用）。 */
export function buildForwardDraftBody(
  message: Pick<InboxThreadMessage, "from" | "to" | "cc" | "subject" | "internal_date" | "body_text" | "body_html"> | undefined,
  userNote = "",
) {
  const note = stripForwardedMessageBlock(userNote).trimEnd();
  const block = buildForwardedMessageBlock(message);
  if (!block) return note;
  if (!note) return `\n\n${block}`;
  return `${note}\n\n${block}`;
}

/**
 * 发送用正文：优先保留原信 HTML 格式。
 * - body: multipart 的 text/plain 回退
 * - body_html: text/html，内嵌原信 HTML
 */
export function buildForwardSendBodies(
  message: Pick<InboxThreadMessage, "from" | "to" | "cc" | "subject" | "internal_date" | "body_text" | "body_html"> | undefined,
  userNote = "",
  userNoteHtml = "",
): { body: string; body_html?: string } {
  const note = stripForwardedMessageBlock(userNote).trimEnd();
  const plain = buildForwardDraftBody(message, note);
  if (!message) return { body: plain };

  const originalHtml = String(message.body_html || "").trim();
  if (!originalHtml) return { body: plain };

  const { header, lines } = forwardHeaderMeta(message);
  const metaHtml = [header, ...lines]
    .map((line) => `<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#222;">${escapeHtml(line)}</div>`)
    .join("");
  const noteHtml = userNoteHtml.trim() || plainNoteToHtml(note);
  const body_html = [
    noteHtml,
    noteHtml ? "<br>" : "",
    `<div class="gmail_quote">`,
    metaHtml,
    `<br>`,
    originalHtml,
    `</div>`,
  ].filter(Boolean).join("\n");

  return { body: plain, body_html };
}

/** 已保存转发 HTML 含原信引用块；恢复编辑器时只取用户可编辑的说明部分。 */
export function extractForwardNoteHtml(value: string | undefined) {
  const html = String(value || "");
  const quoteStart = html.indexOf('<div class="gmail_quote">');
  return quoteStart >= 0 ? html.slice(0, quoteStart).trim() : html.trim();
}

export function isOutboundMessageForMailbox(from: string | undefined, mailbox: string) {
  const normalizedMailbox = mailbox.trim().toLowerCase();
  if (!normalizedMailbox) return false;
  return senderParts(from).email.trim().toLowerCase() === normalizedMailbox;
}

export function buildQuickReplyPrompt(suggestion: QuickReplySuggestion) {
  return `Reply to this email and expand on "${suggestion.label}". Intent: ${suggestion.intent}`;
}

export function matchesDraftArtifact(mailbox: string, threadId: string, artifact: DraftReplyArtifact | null | undefined) {
  if (!artifact) return false;
  return artifact.type === "draft_reply"
    && artifact.mailbox.trim().toLowerCase() === mailbox.trim().toLowerCase()
    && artifact.thread_id === threadId;
}

export function resolveMessageThreadId(message: { id?: string; thread_id?: string } | null | undefined) {
  return message?.thread_id || message?.id || "";
}

export function mergeDraftArtifactBody(current: string, generated: string, mode: "append" | "replace") {
  if (mode === "replace" || !current.trim()) return generated;
  return `${current.trimEnd()}\n\n${generated}`;
}

export function nextWeekend(now: Date) {
  const base = new Date(now);
  const weekday = base.getDay();
  const daysUntilSaturday = (6 - weekday + 7) % 7 || 7;
  base.setDate(base.getDate() + daysUntilSaturday);
  base.setHours(9, 0, 0, 0);
  return base;
}

export function nextWeek(now: Date) {
  const base = new Date(now);
  const weekday = base.getDay();
  const daysUntilMonday = ((8 - weekday) % 7) || 7;
  base.setDate(base.getDate() + daysUntilMonday);
  base.setHours(9, 0, 0, 0);
  return base;
}

export function tomorrowMorning(now: Date) {
  const base = new Date(now);
  base.setDate(base.getDate() + 1);
  base.setHours(9, 0, 0, 0);
  return base;
}

export function buildSnoozePresets(now = new Date()): SnoozePreset[] {
  return [
    { id: "tomorrow", label: "Tomorrow", at: tomorrowMorning(now) },
    { id: "weekend", label: "This weekend", at: nextWeekend(now) },
    { id: "next_week", label: "Next week", at: nextWeek(now) },
  ];
}

export function parseSnoozeInput(input: string, now = new Date()) {
  const text = input.trim().toLowerCase();
  if (!text) return null;
  if (text === "tomorrow") return tomorrowMorning(now);
  if (text === "this weekend" || text === "weekend") return nextWeekend(now);
  if (text === "next week") return nextWeek(now);
  const hourMatch = text.match(/^(\d+)\s*(hour|hours|hr|hrs)$/);
  if (hourMatch) {
    const next = new Date(now);
    next.setHours(next.getHours() + Number(hourMatch[1]));
    return next;
  }
  const dayMatch = text.match(/^(\d+)\s*(day|days)$/);
  if (dayMatch) {
    const next = new Date(now);
    next.setDate(next.getDate() + Number(dayMatch[1]));
    return next;
  }
  const parsed = new Date(input);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}
