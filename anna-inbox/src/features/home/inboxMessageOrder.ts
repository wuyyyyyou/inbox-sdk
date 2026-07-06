import type { InboxMessage } from "../../types/mail";

function inboxMessageTimestamp(message: InboxMessage) {
  const value = Number(message.internal_date || 0);
  return Number.isFinite(value) ? value : 0;
}

export function compareInboxMessagesDesc(left: InboxMessage, right: InboxMessage) {
  const timeDiff = inboxMessageTimestamp(right) - inboxMessageTimestamp(left);
  if (timeDiff !== 0) return timeDiff;
  const dateDiff = String(right.date || "").localeCompare(String(left.date || ""));
  if (dateDiff !== 0) return dateDiff;
  return String(right.id || "").localeCompare(String(left.id || ""));
}

export function sortInboxMessagesDesc<T extends InboxMessage>(messages: T[]) {
  return [...messages].sort(compareInboxMessagesDesc);
}
