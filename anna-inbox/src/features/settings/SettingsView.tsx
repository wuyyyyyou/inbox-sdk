import { useEffect, useRef, useState } from "react";
import type { InboxAutoSyncSeconds, InboxSettings, LlmStatusPollSeconds } from "../../types/mail";
import { useApp } from "../../app/AppContext";
import { useI18n, type Locale } from "../../i18n";
import { AUTO_SYNC_OPTIONS, LLM_STATUS_POLL_OPTIONS } from "./inboxSettings";
import { SplitsManager } from "./SplitsManager";

function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13" />
    </svg>
  );
}

export function SettingsView({ settings, loading, error, focusSavedPromptsRequest, onChange, onBack }: { settings: InboxSettings; loading: boolean; error: string; focusSavedPromptsRequest: number; onChange: (patch: Partial<InboxSettings>) => Promise<boolean>; onBack: () => void }) {
  const { actions } = useApp();
  const { locale, setLocale, t } = useI18n();
  const [splitsOpen, setSplitsOpen] = useState(false);
  const [prompts, setPrompts] = useState<Array<{ id: string; title: string; body: string }>>([]);
  const [memories, setMemories] = useState<Array<{ id: string; text: string }>>([]);
  const [promptTitle, setPromptTitle] = useState("");
  const [promptBody, setPromptBody] = useState("");
  const [memoryText, setMemoryText] = useState("");
  const [personalizationLoading, setPersonalizationLoading] = useState(false);
  const [cacheClearing, setCacheClearing] = useState(false);
  const settingsContentRef = useRef<HTMLDivElement | null>(null);
  const savedPromptsSectionRef = useRef<HTMLElement | null>(null);

  const llmPollLabels: Record<LlmStatusPollSeconds, string> = {
    0: t("common.off"),
    30: t("settings.seconds", { count: 30 }),
    60: t("settings.seconds", { count: 60 }),
    120: t("settings.minutes", { count: 2 }),
    300: t("settings.minutes", { count: 5 }),
  };

  const autoSyncLabels: Record<InboxAutoSyncSeconds, string> = {
    0: t("common.off"),
    5: t("settings.seconds", { count: 5 }),
    15: t("settings.seconds", { count: 15 }),
    30: t("settings.seconds", { count: 30 }),
    60: t("settings.seconds", { count: 60 }),
  };

  const reloadPersonalization = async () => {
    setPersonalizationLoading(true);
    try {
      const [nextPrompts, nextMemories] = await Promise.all([
        actions.listSavedPrompts(),
        actions.listAiMemories(),
      ]);
      setPrompts(nextPrompts);
      setMemories(nextMemories);
    } finally {
      setPersonalizationLoading(false);
    }
  };

  useEffect(() => {
    void reloadPersonalization();
  }, []);

  useEffect(() => {
    if (!focusSavedPromptsRequest) return;
    const frame = window.requestAnimationFrame(() => {
      const settingsContent = settingsContentRef.current;
      const savedPromptsSection = savedPromptsSectionRef.current;
      if (!settingsContent || !savedPromptsSection) return;
      settingsContent.scrollTo({
        behavior: "smooth",
        top:
          savedPromptsSection.getBoundingClientRect().top -
          settingsContent.getBoundingClientRect().top +
          settingsContent.scrollTop -
          16,
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [focusSavedPromptsRequest]);

  return <div className="settings-content" ref={settingsContentRef}>
    <header><h2 className="drawer-title">{t("settings.title")}</h2><button className="icon-btn" type="button" aria-label={t("settings.close")} onClick={onBack}>×</button></header>
    {error ? <p role="alert">{error}</p> : null}
    <section>
      <h2>{t("settings.language")}</h2>
      <p>{t("settings.languageHint")}</p>
      <div className="settings-language-segmented" role="radiogroup" aria-label={t("settings.language")}>
        {([
          { id: "en-US" as Locale, label: t("settings.language.en") },
          { id: "zh-CN" as Locale, label: t("settings.language.zh") },
        ]).map((item) => (
          <button
            key={item.id}
            type="button"
            role="radio"
            aria-checked={locale === item.id}
            className={locale === item.id ? "is-active" : ""}
            onClick={() => setLocale(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>
    </section>
    <section><h2>{t("settings.displayRange")}</h2><p>{t("settings.displayRangeHint")}</p>{[7, 30, 60].map((days) => <label key={days}><input type="radio" checked={settings.display_range_days === days} onChange={() => onChange({ display_range_days: days as 7 | 30 | 60 })} />{t("settings.days", { days })}</label>)}</section>
    <section><h2>{t("settings.timeSections")}</h2><p>{t("settings.timeSectionsHint")}</p><label><input type="radio" checked={settings.time_section_mode === "detailed"} onChange={() => onChange({ time_section_mode: "detailed" })} />{t("settings.time.detailed")}</label><label><input type="radio" checked={settings.time_section_mode === "recent_then_months"} onChange={() => onChange({ time_section_mode: "recent_then_months" })} />{t("settings.time.recentThenMonths")}</label><label><input type="radio" checked={settings.time_section_mode === "months_only"} onChange={() => onChange({ time_section_mode: "months_only" })} />{t("settings.time.monthsOnly")}</label></section>
    <section><h2>{t("settings.importantPriority")}</h2>{(["stars", "todos"] as const).map((kind) => <div key={kind}><h3>{kind === "stars" ? t("settings.stars") : t("settings.todos")}</h3><label><input type="checkbox" checked={settings[`${kind}_enabled`]} onChange={(e) => onChange({ [`${kind}_enabled`]: e.target.checked })} />{t("settings.showAtTop")}</label><select value={settings[`${kind}_limit`]} onChange={(e) => onChange({ [`${kind}_limit`]: Number(e.target.value) })}>{[5, 10, 20, 50].map((limit) => <option key={limit} value={limit}>{t("settings.emailsCount", { count: limit })}</option>)}</select></div>)}</section>
    <section><h2>{t("settings.splits")}</h2><p>{t("settings.splitsHint")}</p><button className="settings-action-btn" type="button" onClick={() => setSplitsOpen(true)}>{t("settings.manageSplits")}</button></section>
    <section>
      <h2>{t("settings.autoRefresh")}</h2>
      <p>{t("settings.autoRefreshHint")}</p>
      {AUTO_SYNC_OPTIONS.map((seconds) => (
        <label key={seconds}>
          <input
            type="radio"
            checked={settings.auto_sync_seconds === seconds}
            onChange={() => onChange({ auto_sync_seconds: seconds })}
          />
          {autoSyncLabels[seconds]}
        </label>
      ))}
    </section>
    <section>
      <h2>{t("settings.mailboxCache")}</h2>
      <p>{t("settings.mailboxCacheHint")}</p>
      <button
        className="settings-action-btn"
        type="button"
        disabled={cacheClearing || loading}
        onClick={() => {
          void (async () => {
            setCacheClearing(true);
            try {
              await actions.clearInboxCacheAndReload(settings.display_range_days);
            } finally {
              setCacheClearing(false);
            }
          })();
        }}
      >
        {cacheClearing ? t("settings.clearingCache") : t("settings.clearCache")}
      </button>
    </section>
    <section>
      <h2>{t("settings.connectivity")}</h2>
      <p>{t("settings.connectivityHint")}</p>
      {LLM_STATUS_POLL_OPTIONS.map((seconds) => (
        <label key={seconds}>
          <input
            type="radio"
            checked={settings.llm_status_poll_seconds === seconds}
            onChange={() => onChange({ llm_status_poll_seconds: seconds })}
          />
          {llmPollLabels[seconds]}
        </label>
      ))}
    </section>
    <section className="settings-ai-personalization" ref={savedPromptsSectionRef}>
      <h2>{t("settings.aiPersonalization")}</h2>
      <p>{t("settings.aiPersonalizationHint")}</p>
      <h3>{t("settings.savedPrompts")}</h3>
      <input type="text" placeholder={t("settings.promptTitlePlaceholder")} value={promptTitle} onChange={(e) => setPromptTitle(e.target.value)} />
      <textarea placeholder={t("settings.promptBodyPlaceholder")} rows={3} value={promptBody} onChange={(e) => setPromptBody(e.target.value)} />
      <button
        className="settings-action-btn"
        type="button"
        disabled={!promptBody.trim() || personalizationLoading}
        onClick={() => {
          void (async () => {
            const ok = await actions.saveSavedPrompt({ title: promptTitle, body: promptBody });
            if (ok) {
              setPromptTitle("");
              setPromptBody("");
              await reloadPersonalization();
            }
          })();
        }}
      >
        {t("settings.addPrompt")}
      </button>
      <div className="settings-list">
        {prompts.length ? prompts.map((item) => (
          <div className="settings-row" key={item.id}>
            <div>
              <strong>{item.title || t("settings.untitled")}</strong>
              <p>{item.body.slice(0, 160)}</p>
            </div>
            <button
              type="button"
              className="settings-delete-button"
              aria-label={t("settings.deletePrompt", { title: item.title || t("settings.untitled") })}
              data-tooltip={t("settings.delete")}
              onClick={() => {
                void (async () => {
                  await actions.deleteSavedPrompt(item.id);
                  await reloadPersonalization();
                })();
              }}
            >
              <TrashIcon />
            </button>
          </div>
        )) : <p className="ai-saved-prompts-empty">{t("settings.noSavedPrompts")}</p>}
      </div>
      <h3>{t("settings.memory")}</h3>
      <p>{t("settings.memoryHint")}</p>
      <textarea placeholder={t("settings.memoryPlaceholder")} rows={2} value={memoryText} onChange={(e) => setMemoryText(e.target.value)} />
      <button
        className="settings-action-btn"
        type="button"
        disabled={!memoryText.trim() || personalizationLoading}
        onClick={() => {
          void (async () => {
            const ok = await actions.addAiMemory(memoryText.trim());
            if (ok) {
              setMemoryText("");
              await reloadPersonalization();
            }
          })();
        }}
      >
        {t("settings.addMemory")}
      </button>
      <div className="settings-list">
        {memories.length ? memories.map((item) => (
          <div className="settings-row" key={item.id}>
            <p>{item.text}</p>
            <button
              type="button"
              className="settings-delete-button"
              aria-label={t("settings.deleteMemory")}
              data-tooltip={t("settings.delete")}
              onClick={() => {
                void (async () => {
                  await actions.deleteAiMemory(item.id);
                  await reloadPersonalization();
                })();
              }}
            >
              <TrashIcon />
            </button>
          </div>
        )) : <p className="ai-saved-prompts-empty">{t("settings.noMemories")}</p>}
      </div>
    </section>
    {loading || personalizationLoading ? <p>{t("settings.saving")}</p> : null}
    <SplitsManager open={splitsOpen} settings={settings} onChange={onChange} onClose={() => setSplitsOpen(false)} />
  </div>;
}
