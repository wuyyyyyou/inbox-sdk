export const MAILBOX_STORAGE_KEY = "anna-inbox-mailbox";
export const DEFAULT_MODE = "default_secretary";
export const POLL_INTERVAL_MS = 2500;
export const POLL_LIMIT = 240;
export const CUSTOM_SCAN_MESSAGE_LIMIT = 50;
/** 侧栏是否走后端 start_ai_turn；localStorage 设 anna-inbox-use-ai-turn=0 可回退 aiRoute。 */
export const AI_TURN_FEATURE_KEY = "anna-inbox-use-ai-turn";

export function isAiTurnEnabled(): boolean {
  try {
    const raw = String(window.localStorage.getItem(AI_TURN_FEATURE_KEY) || "1").trim().toLowerCase();
    return raw !== "0" && raw !== "false" && raw !== "off";
  } catch {
    return true;
  }
}

export const CATEGORY_TABS = [
  { id: "all", label: "All" },
  { id: "reply", label: "Needs reply" },
  { id: "review", label: "Needs review" },
  { id: "cleanup", label: "Cleanup" },
] as const;

export const CATEGORY_NOTE: Record<string, string> = {
  reply: "People waiting for your response.",
  review: "No reply needed, but worth a quick look.",
  cleanup: "Low-signal mail Anna grouped away from your action queue.",
};

export function getSavedMailbox(): string {
  try {
    return localStorage.getItem(MAILBOX_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function isValidMailbox(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(value || "").trim());
}

export function requestForMode(mode: string): string {
  return {
    default_secretary: "Brief this mailbox and surface only emails that need attention.",
    creator_opportunity: "Review creator and partnership opportunities that need follow-up.",
    security_billing: "Check recent security, account, billing, and subscription notices.",
  }[mode] || "Brief this mailbox and surface only emails that need attention.";
}

export function modeLabel(mode: string): string {
  return {
    default_secretary: "Default secretary",
    creator_opportunity: "Creator opportunity",
    security_billing: "Security & billing",
  }[mode] || mode || "Default secretary";
}
