import { useEffect, useRef, useState } from "react";
import type { InboxAutoSyncSeconds, InboxSettings, LlmStatusPollSeconds } from "../../types/mail";
import { useApp } from "../../app/AppContext";
import { AUTO_SYNC_OPTIONS, LLM_STATUS_POLL_OPTIONS } from "./inboxSettings";
import { SplitsManager } from "./SplitsManager";

const LLM_POLL_LABELS: Record<LlmStatusPollSeconds, string> = {
  0: "Off",
  30: "30 seconds",
  60: "60 seconds",
  120: "2 minutes",
  300: "5 minutes",
};

const AUTO_SYNC_LABELS: Record<InboxAutoSyncSeconds, string> = {
  0: "Off",
  15: "15 seconds",
  30: "30 seconds",
  60: "60 seconds",
  120: "2 minutes",
};

function TrashIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13" />
    </svg>
  );
}

export function SettingsView({ settings, loading, error, focusSavedPromptsRequest, onChange, onBack }: { settings: InboxSettings; loading: boolean; error: string; focusSavedPromptsRequest: number; onChange: (patch: Partial<InboxSettings>) => Promise<boolean>; onBack: () => void }) {
  const { actions } = useApp();
  const [splitsOpen, setSplitsOpen] = useState(false);
  const [prompts, setPrompts] = useState<Array<{ id: string; title: string; body: string }>>([]);
  const [memories, setMemories] = useState<Array<{ id: string; text: string }>>([]);
  const [promptTitle, setPromptTitle] = useState("");
  const [promptBody, setPromptBody] = useState("");
  const [memoryText, setMemoryText] = useState("");
  const [personalizationLoading, setPersonalizationLoading] = useState(false);
  const settingsContentRef = useRef<HTMLDivElement | null>(null);
  const savedPromptsSectionRef = useRef<HTMLElement | null>(null);

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
    <header><h2 className="drawer-title">Settings</h2><button className="icon-btn" type="button" aria-label="Close settings" onClick={onBack}>×</button></header>
    {error ? <p role="alert">{error}</p> : null}
    <section><h2>Display range</h2><p>Emails shown in your inbox.</p>{[7, 30, 60].map((days) => <label key={days}><input type="radio" checked={settings.display_range_days === days} onChange={() => onChange({ display_range_days: days as 7 | 30 | 60 })} />{days} days</label>)}</section>
    <section>
      <h2>Initial list size</h2>
      <p>How many emails to show first. More stay available via Show more.</p>
      {[100, 200, 400].map((size) => (
        <label key={size}>
          <input
            type="radio"
            checked={settings.initial_list_size === size}
            onChange={() => onChange({ initial_list_size: size as 100 | 200 | 400 })}
          />
          {size} emails
        </label>
      ))}
    </section>
    <section><h2>Time sections</h2><p>Emails in your inbox are grouped by time period</p><label><input type="radio" checked={settings.time_section_mode === "detailed"} onChange={() => onChange({ time_section_mode: "detailed" })} />Today, Yesterday, Last 7 days, months</label><label><input type="radio" checked={settings.time_section_mode === "recent_then_months"} onChange={() => onChange({ time_section_mode: "recent_then_months" })} />Last 7 days, months</label><label><input type="radio" checked={settings.time_section_mode === "months_only"} onChange={() => onChange({ time_section_mode: "months_only" })} />Months</label></section>
    <section><h2>Important priority</h2>{(["stars", "todos"] as const).map((kind) => <div key={kind}><h3>{kind === "stars" ? "Stars" : "Todos"}</h3><label><input type="checkbox" checked={settings[`${kind}_enabled`]} onChange={(e) => onChange({ [`${kind}_enabled`]: e.target.checked })} />Show at the top of Important</label><select value={settings[`${kind}_limit`]} onChange={(e) => onChange({ [`${kind}_limit`]: Number(e.target.value) })}>{[5, 10, 20, 50].map((limit) => <option key={limit} value={limit}>{limit} emails</option>)}</select></div>)}</section>
    <section><h2>Splits</h2><p>Divide your inbox into tabs for different types of emails.</p><button className="settings-action-btn" type="button" onClick={() => setSplitsOpen(true)}>+ Manage splits</button></section>
    <section>
      <h2>Automatic refresh</h2>
      <p>Check Gmail changes while this mailbox is open. Refresh pauses while the app is hidden or Anna is working.</p>
      {AUTO_SYNC_OPTIONS.map((seconds) => (
        <label key={seconds}>
          <input
            type="radio"
            checked={settings.auto_sync_seconds === seconds}
            onChange={() => onChange({ auto_sync_seconds: seconds })}
          />
          {AUTO_SYNC_LABELS[seconds]}
        </label>
      ))}
    </section>
    <section>
      <h2>Connectivity check</h2>
      <p>How often to probe Anna LLM and Gmail API connectivity and latency in parallel.</p>
      {LLM_STATUS_POLL_OPTIONS.map((seconds) => (
        <label key={seconds}>
          <input
            type="radio"
            checked={settings.llm_status_poll_seconds === seconds}
            onChange={() => onChange({ llm_status_poll_seconds: seconds })}
          />
          {LLM_POLL_LABELS[seconds]}
        </label>
      ))}
    </section>
    <section className="settings-ai-personalization" ref={savedPromptsSectionRef}>
      <h2>AI Personalization</h2>
      <p>Personalize Anna with saved prompts and memory. Inbox organization requires your confirmation; Anna never creates calendar events or changes your inbox silently.</p>
      <h3>Saved prompts</h3>
      <input type="text" placeholder="Title (optional)" value={promptTitle} onChange={(e) => setPromptTitle(e.target.value)} />
      <textarea placeholder="Prompt body" rows={3} value={promptBody} onChange={(e) => setPromptBody(e.target.value)} />
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
        + Add prompt
      </button>
      <div className="settings-list">
        {prompts.length ? prompts.map((item) => (
          <div className="settings-row" key={item.id}>
            <div>
              <strong>{item.title || "Untitled"}</strong>
              <p>{item.body.slice(0, 160)}</p>
            </div>
            <button
              type="button"
              className="settings-delete-button"
              aria-label={`Delete ${item.title || "saved prompt"}`}
              data-tooltip="Delete"
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
        )) : <p className="ai-saved-prompts-empty">No saved prompts</p>}
      </div>
      <h3>Memory</h3>
      <p>Tell the assistant in chat: “Remember to…” or add a short preference here.</p>
      <textarea placeholder="e.g. Prefer short replies" rows={2} value={memoryText} onChange={(e) => setMemoryText(e.target.value)} />
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
        Add memory
      </button>
      <div className="settings-list">
        {memories.length ? memories.map((item) => (
          <div className="settings-row" key={item.id}>
            <p>{item.text}</p>
            <button
              type="button"
              className="settings-delete-button"
              aria-label="Delete memory"
              data-tooltip="Delete"
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
        )) : <p className="ai-saved-prompts-empty">No memories yet</p>}
      </div>
    </section>
    {loading || personalizationLoading ? <p>Saving settings…</p> : null}
    <SplitsManager open={splitsOpen} settings={settings} onChange={onChange} onClose={() => setSplitsOpen(false)} />
  </div>;
}
