import type { InboxMessage, InboxSettings } from "../../types/mail";
import { matchInboxQuery, parseInboxQuery } from "../search/inboxQuery";
import { senderParts } from "../../shared/mailIdentity";

/** Settings 中暴露的 LLM 状态轮询档位（秒） */
export const LLM_STATUS_POLL_OPTIONS = [0, 30, 60, 120, 300] as const;

/** 列表首屏展示条数选项 */
export const INITIAL_LIST_SIZE_OPTIONS = [100, 200, 400] as const;

export const DEFAULT_INBOX_SETTINGS: InboxSettings = {
  mailbox: "",
  display_range_days: 30,
  time_section_mode: "detailed",
  stars_enabled: true,
  stars_limit: 10,
  todos_enabled: true,
  todos_limit: 10,
  llm_status_poll_seconds: 60,
  initial_list_size: 100,
  custom_categories: [],
};

export type InboxSettingsPatch = Omit<Partial<InboxSettings>, "display_range_days"> & {
  display_range_days?: number;
};
export type ImportantGroupKind = "stars" | "todos" | "important";

export type ImportantMessageGroup = {
  kind: ImportantGroupKind;
  messages: InboxMessage[];
};

export type InboxSplitGroup = {
  label: string;
  messages: InboxMessage[];
};

export type InboxSplitMessages = {
  important: InboxMessage[];
  other: InboxMessage[];
  custom: Record<string, InboxMessage[]>;
};

function clampLimit(value: unknown, fallback: number) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(1, Math.min(50, Math.round(parsed)));
}

/** 统一收敛来自接口和表单的设置，避免错误配置影响主页渲染。 */
export function clampInboxSettings(
  value: InboxSettingsPatch | undefined,
): InboxSettings {
  const input = value || {};
  const displayRange = Number(input.display_range_days);
  const timeMode = input.time_section_mode;
  return {
    mailbox: String(input.mailbox || ""),
    display_range_days: displayRange === 7 || displayRange === 30 || displayRange === 60
      ? displayRange
      : DEFAULT_INBOX_SETTINGS.display_range_days,
    time_section_mode: timeMode === "recent_then_months" || timeMode === "months_only" || timeMode === "detailed"
      ? timeMode
      : DEFAULT_INBOX_SETTINGS.time_section_mode,
    stars_enabled: typeof input.stars_enabled === "boolean" ? input.stars_enabled : DEFAULT_INBOX_SETTINGS.stars_enabled,
    stars_limit: clampLimit(input.stars_limit, DEFAULT_INBOX_SETTINGS.stars_limit),
    todos_enabled: typeof input.todos_enabled === "boolean" ? input.todos_enabled : DEFAULT_INBOX_SETTINGS.todos_enabled,
    todos_limit: clampLimit(input.todos_limit, DEFAULT_INBOX_SETTINGS.todos_limit),
    llm_status_poll_seconds: (LLM_STATUS_POLL_OPTIONS as readonly number[]).includes(Number(input.llm_status_poll_seconds))
      ? (Number(input.llm_status_poll_seconds) as InboxSettings["llm_status_poll_seconds"])
      : DEFAULT_INBOX_SETTINGS.llm_status_poll_seconds,
    initial_list_size: (INITIAL_LIST_SIZE_OPTIONS as readonly number[]).includes(Number(input.initial_list_size))
      ? (Number(input.initial_list_size) as InboxSettings["initial_list_size"])
      : DEFAULT_INBOX_SETTINGS.initial_list_size,
    custom_categories: Array.isArray(input.custom_categories) ? input.custom_categories
      .filter((category) => category && typeof category.id === "string" && typeof category.name === "string" && typeof category.query === "string")
      .map((category) => {
        const bundlingBehavior: "default" | "by_sender" | "none" = category.bundling_behavior === "by_sender" || category.bundling_behavior === "none"
          ? category.bundling_behavior
          : "default";
        return {
          id: category.id.trim(),
          name: category.name.trim(),
          query: category.query.trim(),
          hide_when_empty: category.hide_when_empty === true,
          bundling_behavior: bundlingBehavior,
        };
      })
      .filter((category, index, categories) => category.id && category.name && category.query && categories.findIndex((item) => item.id === category.id) === index)
      .slice(0, 20) : [],
    updated_at: input.updated_at,
  };
}

/** 将 Important 邮件划分为置顶区和其余区，并确保同一邮件只出现一次。 */
export function splitImportantMessages(
  messages: InboxMessage[],
  settings: InboxSettings,
  todoIds: ReadonlySet<string>,
): ImportantMessageGroup[] {
  const shown = new Set<string>();
  const take = (candidates: InboxMessage[], limit: number) => candidates.filter((message) => {
    if (!message.id || shown.has(message.id)) return false;
    shown.add(message.id);
    return true;
  }).slice(0, limit);
  const groups: ImportantMessageGroup[] = [];
  if (settings.stars_enabled) {
    const starred = take(messages.filter((message) => Boolean(message.starred || message.label_ids?.includes("STARRED"))), settings.stars_limit);
    if (starred.length) groups.push({ kind: "stars", messages: starred });
  }
  if (settings.todos_enabled) {
    const todos = take(messages.filter((message) => todoIds.has(message.id)), settings.todos_limit);
    if (todos.length) groups.push({ kind: "todos", messages: todos });
  }
  const remaining = messages.filter((message) => !shown.has(message.id));
  if (remaining.length) groups.push({ kind: "important", messages: remaining });
  return groups;
}

/** 判断邮件是否属于固定的 Important Split，规则与首页已有标签保持一致。 */
function isInboxSplitImportant(message: InboxMessage): boolean {
  return Boolean(message.important || message.label_ids?.includes("IMPORTANT"));
}

/**
 * 将自定义 Split 独立匹配；Other 仅保留未进入任何其他 Split 的邮件。
 * @param customMatchMessages 可选：自定义 Split 的匹配源（如含 todos/snoozed），
 *   与 is:unread 搜索对齐；Important/Other 仍只基于 messages。
 */
export function splitInboxMessages(
  messages: InboxMessage[],
  settings: InboxSettings,
  customMatchMessages?: InboxMessage[],
): InboxSplitMessages {
  const custom: Record<string, InboxMessage[]> = {};
  const customMessageIds = new Set<string>();
  const categories = Array.isArray(settings.custom_categories) ? settings.custom_categories : [];
  // 自定义 Split 可用更广数据源（Inbox + Todos + Snoozed），与 is:unread 一致
  const customSource = customMatchMessages || messages;
  for (const split of categories) {
    const parsed = parseInboxQuery(split.query);
    const matched = parsed.expression && !parsed.error
      ? customSource.filter((message) => matchInboxQuery(message, parsed))
      : [];
    custom[split.id] = matched;
    matched.forEach((message) => customMessageIds.add(message.id));
  }
  return {
    important: messages.filter(isInboxSplitImportant),
    other: messages.filter((message) => !isInboxSplitImportant(message) && !customMessageIds.has(message.id)),
    custom,
  };
}

function splitMessageDate(message: InboxMessage): Date | null {
  const raw = message.internal_date || message.date || "";
  const milliseconds = /^\d+$/u.test(String(raw)) ? Number(raw) : Date.parse(String(raw));
  return Number.isFinite(milliseconds) ? new Date(milliseconds) : null;
}

function defaultSplitGroupLabel(message: InboxMessage, mode: InboxSettings["time_section_mode"]): string {
  const date = splitMessageDate(message);
  if (!date) return "LAST 30 DAYS";
  const now = new Date();
  if (mode === "detailed" && date.toDateString() === now.toDateString()) return "TODAY";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (mode === "detailed" && date.toDateString() === yesterday.toDateString()) return "YESTERDAY";
  const daysAgo = Math.floor((now.getTime() - date.getTime()) / (24 * 60 * 60 * 1000));
  if (daysAgo < 7 && mode !== "months_only") return "LAST 7 DAYS";
  if (daysAgo < 30 && mode !== "months_only") return "EARLIER THIS MONTH";
  return `EARLIER IN ${date.toLocaleDateString("en-US", { month: "long" }).toUpperCase()}`;
}

/** 根据 Split 的 bundling 选项返回稳定的展示分组，调用方继续负责邮件排序。 */
export function groupSplitMessages(
  messages: InboxMessage[],
  behavior: "default" | "by_sender" | "none",
  timeSectionMode: InboxSettings["time_section_mode"],
): InboxSplitGroup[] {
  if (behavior === "none") return [{ label: "", messages }];
  const groups = new Map<string, InboxMessage[]>();
  for (const message of messages) {
    const sender = senderParts(message.from);
    const label = behavior === "by_sender"
      ? sender.name || sender.email || "Unknown sender"
      : defaultSplitGroupLabel(message, timeSectionMode);
    groups.set(label, [...(groups.get(label) || []), message]);
  }
  return [...groups.entries()].map(([label, groupedMessages]) => ({ label, messages: groupedMessages }));
}
