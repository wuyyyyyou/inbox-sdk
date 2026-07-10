export interface AddressParts {
  name: string;
  email: string;
}

export interface MailAvatarFallback {
  initial: string;
  tone: number;
  key: string;
}

const AUTOMATED_LOCAL_PARTS = new Set([
  "admin",
  "contact",
  "hello",
  "info",
  "mail",
  "mailer",
  "message",
  "messages",
  "news",
  "newsletter",
  "no-reply",
  "noreply",
  "notification",
  "notifications",
  "support",
  "team",
]);

const DOMAIN_NOISE_PARTS = new Set([
  "email",
  "emails",
  "mail",
  "mailer",
  "mailgun",
  "send",
  "sender",
  "smtp",
]);

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

function cleanLabel(value: unknown) {
  return String(value ?? "").replace(/^"+|"+$/g, "").trim();
}

function normalizedLocalPart(email: string) {
  return email.split("@")[0].trim().toLowerCase().replace(/[._\s]+/g, "-");
}

function automatedDomainLabel(email: string) {
  const domain = email.split("@")[1]?.trim().toLowerCase() || "";
  const registrable = domain.split(".").filter(Boolean).slice(0, -1).pop() || "";
  const parts = registrable.split("-").filter((part) => part && !DOMAIN_NOISE_PARTS.has(part));
  return parts[0] || registrable || "";
}

function isAutomatedLocalPart(local: string) {
  return AUTOMATED_LOCAL_PARTS.has(local)
    || /^do-?not-?reply$/.test(local)
    || /^no-?reply/.test(local)
    || /notification/.test(local);
}

function normalizeAvatarKey(value: string) {
  return value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "");
}

function hashString(value: string) {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = ((hash << 5) - hash + value.charCodeAt(index)) | 0;
  }
  return Math.abs(hash);
}

function initialFrom(label: string) {
  return (label.match(/[A-Za-z0-9]/)?.[0] || label.slice(0, 1) || "?").toUpperCase();
}

export function mailAvatarFallback(identity: unknown, preferredLabel?: unknown): MailAvatarFallback {
  const parts = senderParts(identity);
  const email = parts.email.trim().toLowerCase();
  const local = email ? normalizedLocalPart(email) : "";
  const parsedName = parts.name && parts.name !== parts.email ? parts.name : "";
  const explicitLabel = cleanLabel(preferredLabel);
  const label = explicitLabel || parsedName;
  const domainLabel = email && isAutomatedLocalPart(local) ? automatedDomainLabel(email) : "";
  const displayLabel = label || domainLabel || parts.name || local || parts.email || "?";
  const keySource = label || domainLabel || email || displayLabel;
  const key = normalizeAvatarKey(keySource) || "?";

  return {
    initial: initialFrom(displayLabel),
    tone: hashString(key) % 5,
    key,
  };
}
