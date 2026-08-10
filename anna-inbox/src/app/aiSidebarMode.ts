/** 侧栏 AI 路径：local = start_ai_turn 本地 Router + Sampling；host 仅用于开发兼容。 */

export type AiSidebarMode = "host" | "local";

/** localStorage 覆盖键；设为 local / host，空或非法则使用默认 local。 */
export const AI_SIDEBAR_MODE_STORAGE_KEY = "anna-inbox-ai-sidebar-mode";

export function normalizeAiSidebarMode(value: unknown): AiSidebarMode | null {
  const raw = String(value || "").trim().toLowerCase();
  if (raw === "local" || raw === "host") return raw;
  return null;
}

/**
 * 解析有效侧栏模式：localStorage 覆盖优先，否则始终使用 local。
 *
 * backendMode 保留为兼容参数，但后端返回 host 不得改变默认路径；host
 * 只能通过隐藏的 localStorage 开关显式启用。
 */
export function resolveAiSidebarMode(_backendMode?: unknown): AiSidebarMode {
  if (typeof window !== "undefined") {
    try {
      const fromStorage = normalizeAiSidebarMode(
        window.localStorage.getItem(AI_SIDEBAR_MODE_STORAGE_KEY),
      );
      if (fromStorage) return fromStorage;
    } catch {
      // localStorage 不可用时忽略覆盖
    }
  }
  return "local";
}
