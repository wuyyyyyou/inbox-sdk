import { modeLabel } from "../../app/constants";
import { useApp } from "../../app/AppContext";
import { formatBeijingTimestamp } from "../../shared/format";
import { resolvedCards } from "../brief/cardHelpers";

export function Drawers() {
  const { state, actions } = useApp();
  const overlayOpen = state.sourcesOpen || state.historyOpen || state.scanPlanOpen;
  return (
    <>
      <div className={`drawer-overlay ${overlayOpen ? "is-open" : ""}`} onClick={actions.closeDrawers} />
      <SourcesDrawer />
      <HistoryDrawer />
      <ScanPlanDrawer />
      <aside className="drawer" aria-label="Original message drawer" />
    </>
  );
}

function SourcesDrawer() {
  const { state, actions } = useApp();
  const plan = state.scanPlan || {};
  const totalScans = Number(state.scanState?.total_scans || 0);
  const timeRange = plan.time_range || "auto";
  const maxMessages = plan.max_messages || 50;
  const schedule = plan.schedule || "manual";
  const windowLabel = timeRange !== "auto"
    ? ({ last_24h: "1 day", last_7d: "7 days", unread_backlog: "30 days", since_last: "since last scan" }[timeRange] || timeRange)
    : totalScans === 0 ? "7 days (first scan)" : "adaptive";
  const scheduleLabel = { manual: "manual", every_morning: "morning", every_afternoon: "afternoon", twice_daily: "2x/day", workdays: "workdays" }[schedule] || schedule;
  const storageLabel = state.storageProvider === "aps" ? "APS (Anna Persistent Storage)" : "local JSON files";
  const initials = (state.mailbox || "A").charAt(0).toUpperCase();
  return (
    <aside className={`drawer ${state.sourcesOpen ? "is-open" : ""}`} aria-label="Sources drawer">
      <div className="drawer-head">
        <div><h2 className="drawer-title">Sources</h2><p className="drawer-copy">Connected mailbox and scan strategy.</p></div>
        <button className="icon-btn" onClick={actions.closeDrawers}>x</button>
      </div>
      <div className="drawer-body">
        <article className="source-card mailbox-card is-selected">
          <div className="source-icon">{initials}</div>
          <div>
            <div className="source-name">{state.mailbox}</div>
            <div className="source-meta">Gmail source · {modeLabel(state.strategyMode)}</div>
          </div>
        </article>
        <section className="config-block">
          <h3>Scan window</h3>
          <ul className="config-list">
            <li>Range: <strong>{windowLabel}</strong></li>
            {totalScans > 0 ? <li>Incremental: stops at last processed message time.</li> : <li>First scan covers the full window above.</li>}
            <li>Max messages per scan: <strong>{maxMessages}</strong></li>
          </ul>
        </section>
        <section className="config-block">
          <h3>Schedule &amp; storage</h3>
          <ul className="config-list">
            <li>Schedule: <strong>{scheduleLabel}</strong></li>
            <li>Storage: {storageLabel}</li>
            {plan.include_newsletters ? <li>Including newsletters</li> : null}
            {plan.include_promotions ? <li>Including promotions</li> : null}
          </ul>
        </section>
      </div>
    </aside>
  );
}

function HistoryDrawer() {
  const { state, actions } = useApp();
  const resolved = resolvedCards(state.cards);
  const groups = {
    snoozed: resolved.filter((c) => c.status === "snoozed"),
    dismissed: resolved.filter((c) => c.status === "dismissed"),
    resolved: resolved.filter((c) => c.status === "resolved"),
  };
  const statusLabel = (card: { status?: string; snooze_until?: string; resolution?: string }) => {
    if (card.status === "snoozed") return card.snooze_until ? `Until ${formatBeijingTimestamp(card.snooze_until)}` : "Snoozed";
    if (card.status === "dismissed") return "Dismissed";
    const resolutionMap: Record<string, string> = { no_action_needed: "No action", handled_manually: "Handled", replied: "Replied", dont_prioritize: "Muted" };
    return resolutionMap[card.resolution || ""] || card.resolution || "Resolved";
  };
  const renderGroup = (label: string, cards: typeof resolved) => cards.length ? (
    <div className="resolved-group">
      <div className="resolved-group-title">{label}</div>
      {cards.map((card) => (
        <div className="resolved-card" key={card.id}>
          <div><div className="resolved-card-title">{card.title || "Untitled"}</div><div className="resolved-card-meta">{statusLabel(card)}</div></div>
          <button className="detail-back" onClick={() => void actions.restoreCard(card.id)}>Restore</button>
        </div>
      ))}
    </div>
  ) : null;
  return (
    <aside className={`drawer ${state.historyOpen ? "is-open" : ""}`} aria-label="History drawer">
      <div className="drawer-head">
        <div><h2 className="drawer-title">History</h2><p className="drawer-copy">Cards you've processed and past runs.</p></div>
        <button className="icon-btn" onClick={actions.closeDrawers}>×</button>
      </div>
      <div className="drawer-body">
        {resolved.length ? <section className="resolved-section">{renderGroup("Snoozed", groups.snoozed)}{renderGroup("Dismissed", groups.dismissed)}{renderGroup("Resolved", groups.resolved)}</section> : <p className="assistant-copy">No processed cards yet.</p>}
        {state.history.length ? (
          <section style={{ marginTop: 18 }}>
            <div className="resolved-group-title" style={{ marginBottom: 8 }}>Past runs</div>
            {state.history.map((run) => (
              <article className="source-card" key={run.run_id || `${run.ts}-${run.request}`}>
                <div className="source-icon">A</div>
                <div><div className="source-name">{run.request || run.strategy || "Mailbox scan"}</div><div className="source-meta">{formatBeijingTimestamp(run.ts) || "-"} · {run.result || ""}</div></div>
              </article>
            ))}
          </section>
        ) : null}
      </div>
    </aside>
  );
}

function ScanPlanDrawer() {
  const { state, actions } = useApp();
  const plan = state.scanPlan || {};
  const rangeLabels: Record<string, string> = { auto: "Adaptive (auto)", since_last: "Since last brief", last_24h: "Last 24 hours", last_7d: "Last 7 days", unread_backlog: "Unread backlog" };
  const scheduleLabels: Record<string, string> = { manual: "Manual only", every_morning: "Every morning", every_afternoon: "Every afternoon", twice_daily: "Twice a day", workdays: "Workdays only" };
  return (
    <aside className={`drawer ${state.scanPlanOpen ? "is-open" : ""}`} aria-label="Scan plan drawer">
      <div className="drawer-head">
        <div><h2 className="drawer-title">Next Scan</h2><p className="drawer-copy">Configure when and how Anna scans your inbox.</p></div>
        <button className="icon-btn" onClick={actions.closeDrawers}>×</button>
      </div>
      <div className="drawer-body">
        <PlanButtonGroup title="Schedule" entries={scheduleLabels} current={plan.schedule} onSet={(v) => void actions.saveScanPlanField("schedule", v)} />
        <PlanButtonGroup title="Time range" entries={rangeLabels} current={plan.time_range} onSet={(v) => void actions.saveScanPlanField("time_range", v)} />
        <section className="config-block">
          <h3>Messages per scan</h3>
          <div className="preset-row preset-row-sm">
            {[50, 100, 200].map((n) => <button key={n} className={`preset-chip ${plan.max_messages === n ? "is-active" : ""}`} onClick={() => void actions.saveScanPlanField("max_messages", n)}>{n}</button>)}
          </div>
        </section>
        <section className="config-block">
          <h3>Include</h3>
          <div className="preset-row preset-row-sm">
            <button className={`preset-chip ${plan.include_newsletters ? "is-active" : ""}`} onClick={() => void actions.saveScanPlanField("include_newsletters", !plan.include_newsletters)}>Newsletters</button>
            <button className={`preset-chip ${plan.include_promotions ? "is-active" : ""}`} onClick={() => void actions.saveScanPlanField("include_promotions", !plan.include_promotions)}>Promotions</button>
          </div>
        </section>
        {plan.updated_at ? <p className="scan-plan-saved-text">Saved {formatBeijingTimestamp(plan.updated_at)}</p> : null}
      </div>
    </aside>
  );
}

function PlanButtonGroup({ title, entries, current, onSet }: { title: string; entries: Record<string, string>; current?: string; onSet: (value: string) => void }) {
  return (
    <section className="config-block">
      <h3>{title}</h3>
      <div className="preset-row preset-row-sm">
        {Object.entries(entries).map(([value, label]) => <button key={value} className={`preset-chip ${current === value ? "is-active" : ""}`} onClick={() => onSet(value)}>{label}</button>)}
      </div>
    </section>
  );
}
