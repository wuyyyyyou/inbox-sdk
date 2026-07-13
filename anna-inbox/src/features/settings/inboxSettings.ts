import type { InboxMessage, InboxSettings } from "../../types/mail";

export const DEFAULT_INBOX_SETTINGS: InboxSettings = {
  mailbox: "",
  display_range_days: 30,
  time_section_mode: "detailed",
  stars_enabled: true,
  stars_limit: 10,
  todos_enabled: true,
  todos_limit: 10,
};

export type InboxSettingsPatch = Omit<Partial<InboxSettings>, "display_range_days"> & {
  display_range_days?: number;
};
export type ImportantGroupKind = "stars" | "todos" | "important";

export type ImportantMessageGroup = {
  kind: ImportantGroupKind;
  messages: InboxMessage[];
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
