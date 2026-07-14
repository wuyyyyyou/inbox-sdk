import { useState } from "react";
import type { InboxSettings } from "../../types/mail";
import { SplitsManager } from "./SplitsManager";

export function SettingsView({ settings, loading, error, onChange, onBack }: { settings: InboxSettings; loading: boolean; error: string; onChange: (patch: Partial<InboxSettings>) => Promise<boolean>; onBack: () => void }) {
  const [splitsOpen, setSplitsOpen] = useState(false);
  return <div className="settings-content">
    <header><h2 className="drawer-title">Settings</h2><button className="icon-btn" type="button" aria-label="Close settings" onClick={onBack}>×</button></header>
    {error ? <p role="alert">{error}</p> : null}
    <section><h2>Display range</h2><p>Emails shown in your inbox.</p>{[7, 30, 60].map((days) => <label key={days}><input type="radio" checked={settings.display_range_days === days} onChange={() => onChange({ display_range_days: days as 7 | 30 | 60 })} />{days} days</label>)}</section>
    <section><h2>Time sections</h2><p>Emails in your inbox are grouped by time period</p><label><input type="radio" checked={settings.time_section_mode === "detailed"} onChange={() => onChange({ time_section_mode: "detailed" })} />Today, Yesterday, Last 7 days, months</label><label><input type="radio" checked={settings.time_section_mode === "recent_then_months"} onChange={() => onChange({ time_section_mode: "recent_then_months" })} />Last 7 days, months</label><label><input type="radio" checked={settings.time_section_mode === "months_only"} onChange={() => onChange({ time_section_mode: "months_only" })} />Months</label></section>
    <section><h2>Important priority</h2>{(["stars", "todos"] as const).map((kind) => <div key={kind}><h3>{kind === "stars" ? "Stars" : "Todos"}</h3><label><input type="checkbox" checked={settings[`${kind}_enabled`]} onChange={(e) => onChange({ [`${kind}_enabled`]: e.target.checked })} />Show at the top of Important</label><select value={settings[`${kind}_limit`]} onChange={(e) => onChange({ [`${kind}_limit`]: Number(e.target.value) })}>{[5, 10, 20, 50].map((limit) => <option key={limit} value={limit}>{limit} threads</option>)}</select></div>)}</section>
    <section><h2>Splits</h2><p>Divide your inbox into tabs for different types of emails.</p><button type="button" onClick={() => setSplitsOpen(true)}>Manage splits</button></section>
    {loading ? <p>Saving settings…</p> : null}
    <SplitsManager open={splitsOpen} settings={settings} onChange={onChange} onClose={() => setSplitsOpen(false)} />
  </div>;
}
