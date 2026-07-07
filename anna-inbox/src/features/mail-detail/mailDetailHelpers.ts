import type {
  AttachmentDownloadPayload,
  DraftReplyArtifact,
  InboxThreadMessage,
  InboxThreadPagePayload,
  MailAttachmentMeta,
  QuickReplySuggestion,
} from "../../types/mail";
import { triggerBrowserDownload } from "../../shared/browserDownload";

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

export interface AddressParts {
  name: string;
  email: string;
}

export interface SnoozePreset {
  id: string;
  label: string;
  at: Date;
}

export function hasNewerThreadMessage(
  page: InboxThreadPagePayload | null,
  latestThreadMessageId: string,
  latestThreadInternalDate: string,
) {
  if (!page?.latest_message_id || !latestThreadMessageId || page.latest_message_id === latestThreadMessageId) return false;
  const loadedLatest = page.messages.find((item) => item.id === page.latest_message_id) || page.messages[page.messages.length - 1];
  const loadedTime = Number(loadedLatest?.internal_date || 0);
  const knownTime = Number(latestThreadInternalDate || 0);
  return Number.isFinite(loadedTime) && Number.isFinite(knownTime) && knownTime > loadedTime;
}

export function senderParts(value: unknown): AddressParts {
  const text = String(value ?? "");
  const match = text.match(/^\s*"?([^"<]+?)"?\s*<([^>]+)>/);
  if (match) return { name: match[1].trim(), email: match[2].trim() };
  if (text.includes("@")) return { name: text.split("@")[0].trim() || text.trim(), email: text.trim() };
  return { name: text.trim() || "Unknown sender", email: text.trim() };
}

export function splitAddresses(value: unknown): string[] {
  const text = String(value ?? "").trim();
  if (!text) return [];
  const parts: string[] = [];
  let start = 0;
  let quoted = false;
  let angleDepth = 0;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (character === "\"") quoted = !quoted;
    else if (!quoted && character === "<") angleDepth += 1;
    else if (!quoted && character === ">") angleDepth = Math.max(0, angleDepth - 1);
    else if (!quoted && angleDepth === 0 && character === ",") {
      parts.push(text.slice(start, index).trim());
      start = index + 1;
    }
  }
  parts.push(text.slice(start).trim());
  return parts.filter(Boolean);
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
  if (mime === "application/pdf" || extension === "pdf") return "pdf";
  if (
    mime.startsWith("image/")
    || ["png", "jpg", "jpeg", "gif", "webp"].includes(extension)
  ) return "image";
  if (
    mime.startsWith("text/")
    || mime === "application/json"
    || mime === "text/csv"
    || ["txt", "json", "csv", "log", "md"].includes(extension)
  ) return "text";
  if (
    mime.startsWith("audio/")
    || ["mp3", "wav"].includes(extension)
  ) return "audio";
  if (
    mime.startsWith("video/")
    || ["mp4", "webm"].includes(extension)
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
