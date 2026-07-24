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

/** 列表字段签名：用于判断刷新后内容是否实质变化。 */
export function inboxMessageContentKey(message: InboxMessage) {
  return [
    message.id || "",
    message.thread_id || "",
    message.mailbox || "",
    message.internal_date || "",
    message.date || "",
    message.from || "",
    message.to || "",
    message.subject || "",
    message.latest_subject || "",
    message.snippet || "",
    message.body_preview || "",
    message.draft_body || "",
    message.draft_local ? "1" : "0",
    (message.label_ids || []).join("\u0002"),
    message.unread ? "1" : "0",
    message.important ? "1" : "0",
    message.starred ? "1" : "0",
    message.has_attachment ? "1" : "0",
    String(message.attachment_count ?? ""),
    message.body_cached ? "1" : "0",
  ].join("\u0001");
}

export function sameInboxMessageContent(left: InboxMessage, right: InboxMessage) {
  return inboxMessageContentKey(left) === inboxMessageContentKey(right);
}

/**
 * 按 id 合并邮件列表：未变化的条目保留原对象引用，减少行级重渲染。
 * 后到的 incoming 覆盖同 id；仅新增/变更时写入新对象。
 */
export function mergeInboxMessagesById(
  existing: InboxMessage[],
  incoming: InboxMessage[],
): InboxMessage[] {
  if (!incoming.length) return existing;
  const byId = new Map<string, InboxMessage>();
  for (const message of existing) {
    if (message.id) byId.set(message.id, message);
  }
  let mutated = false;
  for (const message of incoming) {
    if (!message.id) continue;
    const prev = byId.get(message.id);
    if (prev && sameInboxMessageContent(prev, message)) continue;
    byId.set(message.id, message);
    mutated = true;
  }
  if (!mutated && byId.size === existing.length) {
    // 无新增且内容一致：仍可能需要保持调用方得到排序后的视图；若顺序已正确可复用
    let orderMatches = existing.length === byId.size;
    if (orderMatches) {
      const sorted = sortInboxMessagesDesc([...byId.values()]);
      for (let i = 0; i < existing.length; i += 1) {
        if (existing[i] !== sorted[i]) {
          orderMatches = false;
          break;
        }
      }
      if (orderMatches) return existing;
      return sorted;
    }
  }
  return sortInboxMessagesDesc([...byId.values()]);
}

/**
 * Soft reload 收尾：删除「当前同步窗口内」且不在 keepIds 的邮件。
  * 窗口外（触底续页 / 扩时间窗）的旧邮件予以保留，避免静默刷新误清。
 */
export function pruneInboxMessagesToCacheWindow(
  messages: InboxMessage[],
  keepIds: Set<string>,
  days: number,
): InboxMessage[] {
  if (!messages.length) return messages;
  const windowDays = Number(days) || 0;
  const cutoffMs = windowDays > 0 ? Date.now() - windowDays * 24 * 60 * 60 * 1000 : 0;
  let removed = false;
  const next = messages.filter((message) => {
    if (!message.id) return false;
    if (keepIds.has(message.id)) return true;
    if (cutoffMs > 0) {
      const ts = inboxMessageTimestamp(message);
      // 无可靠时间戳时保守保留，避免误删
      if (!ts || ts < cutoffMs) return true;
    }
    removed = true;
    return false;
  });
  return removed ? next : messages;
}
