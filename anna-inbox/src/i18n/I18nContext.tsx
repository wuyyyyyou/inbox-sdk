import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  DEFAULT_LOCALE,
  readStoredLocale,
  writeStoredLocale,
  type Locale,
} from "./locale";
import { enMessages, type MessageKey } from "./messages.en";
import { zhCNMessages } from "./messages.zh-CN";

type MessageParams = Record<string, string | number>;

export type TranslateFn = (key: MessageKey, params?: MessageParams) => string;

interface I18nContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  toggleLocale: () => void;
  t: TranslateFn;
}

const CATALOG: Record<Locale, Record<MessageKey, string>> = {
  "en-US": enMessages,
  "zh-CN": zhCNMessages,
};

function interpolate(template: string, params?: MessageParams): string {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (_, name: string) => {
    const value = params[name];
    return value === undefined || value === null ? "" : String(value);
  });
}

function translate(locale: Locale, key: MessageKey, params?: MessageParams): string {
  const table = CATALOG[locale] || CATALOG[DEFAULT_LOCALE];
  const template = table[key] ?? enMessages[key] ?? String(key);
  return interpolate(template, params);
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => readStoredLocale());

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    writeStoredLocale(next);
  }, []);

  const toggleLocale = useCallback(() => {
    setLocaleState((current) => {
      const next: Locale = current === "zh-CN" ? "en-US" : "zh-CN";
      writeStoredLocale(next);
      return next;
    });
  }, []);

  useEffect(() => {
    if (typeof document === "undefined") return;
    document.documentElement.lang = locale;
  }, [locale]);

  const value = useMemo<I18nContextValue>(() => ({
    locale,
    setLocale,
    toggleLocale,
    t: (key, params) => translate(locale, key, params),
  }), [locale, setLocale, toggleLocale]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error("useI18n must be used inside I18nProvider");
  return ctx;
}

/** 在 Provider 外或测试中可安全使用的英文回退翻译。 */
export function tFallback(key: MessageKey, params?: MessageParams): string {
  return translate(DEFAULT_LOCALE, key, params);
}
