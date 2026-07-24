/** 侧栏 AI 路径：host = Host Agent Session；local = start_ai_turn 本地 Router + Sampling。 */

export type AiSidebarMode = "host" | "local";

/** localStorage 覆盖键；设为 local / host，空或非法则回退后端默认。 */
export const AI_SIDEBAR_MODE_STORAGE_KEY = "anna-inbox-ai-sidebar-mode";

export function normalizeAiSidebarMode(value: unknown): AiSidebarMode | null {
  const raw = String(value || "").trim().toLowerCase();
  if (raw === "local" || raw === "host") return raw;
  return null;
}

/**
 * 解析有效侧栏模式：localStorage 覆盖优先，否则用后端 env 默认，再否则 host。
 */
export function resolveAiSidebarMode(backendMode: unknown = "host"): AiSidebarMode {
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
  return normalizeAiSidebarMode(backendMode) || "host";
}
