/** 应用支持的界面语言；独立于邮箱设置与 AI 回复语言。 */
export type Locale = "en-US" | "zh-CN";

export const LOCALE_STORAGE_KEY = "anna-inbox:locale:v1";

export const DEFAULT_LOCALE: Locale = "en-US";

/** 将任意输入收敛为受支持的 Locale。 */
export function normalizeLocale(value: unknown): Locale | null {
  const raw = String(value || "").trim().toLowerCase().replace(/_/g, "-");
  if (!raw) return null;
  if (raw === "en" || raw === "en-us" || raw.startsWith("en-")) return "en-US";
  if (raw === "zh" || raw === "zh-cn" || raw === "zh-hans" || raw === "zh-sg") return "zh-CN";
  // 首版无繁体资源：繁体浏览器语言回退英文，避免假装支持。
  if (raw.startsWith("zh-")) return null;
  return null;
}

/** 根据浏览器语言推断默认界面语言。 */
export function detectBrowserLocale(): Locale {
  if (typeof navigator === "undefined") return DEFAULT_LOCALE;
  const candidates = Array.isArray(navigator.languages) && navigator.languages.length
    ? navigator.languages
    : [navigator.language];
  for (const item of candidates) {
    const locale = normalizeLocale(item);
    if (locale) return locale;
  }
  return DEFAULT_LOCALE;
}

/** 同步读取已保存语言；无有效值时回退浏览器语言。 */
export function readStoredLocale(): Locale {
  if (typeof window === "undefined") return DEFAULT_LOCALE;
  try {
    const stored = normalizeLocale(window.localStorage.getItem(LOCALE_STORAGE_KEY));
    if (stored) return stored;
  } catch {
    /* ignore storage failures */
  }
  return detectBrowserLocale();
}

/** 持久化界面语言；失败时静默忽略。 */
export function writeStoredLocale(locale: Locale): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  } catch {
    /* ignore storage failures */
  }
}

/** 供 Intl 使用的 BCP 47 标签。 */
export function toIntlLocale(locale: Locale): string {
  return locale;
}
