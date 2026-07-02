import type { DraftReplyArtifact, InboxThreadMessage, MailAttachmentMeta, QuickReplySuggestion } from "../../types/mail";

export interface AddressParts {
  name: string;
  email: string;
}

export interface SnoozePreset {
  id: string;
  label: string;
  at: Date;
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

export function isPreviewableAttachment(attachment: MailAttachmentMeta) {
  const mime = String(attachment.mime_type || "").toLowerCase();
  return (
    mime.startsWith("image/")
    || mime === "application/pdf"
    || mime.startsWith("text/")
    || mime === "application/json"
    || mime === "text/csv"
    || mime.startsWith("audio/")
    || mime.startsWith("video/")
  );
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
