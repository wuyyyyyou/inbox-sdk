import { useState } from "react";
import { modeLabel } from "../../app/constants";
import { useApp } from "../../app/AppContext";
import { formatBeijingTimestamp } from "../../shared/format";
import type { RunHistoryEntry } from "../../types/mail";

function RestoreIcon() {
  return (
    <svg className="history-restore-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M3 7v6h6" />
      <path d="M21 17a9 9 0 0 0-15.4-6.4L3 13" />
    </svg>
  );
}

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
  const windowLabel = timeRange !== "auto"
    ? ({ last_24h: "1 day", last_7d: "7 days", unread_backlog: "30 days", since_last: "since last scan" }[timeRange] || timeRange)
    : totalScans === 0 ? "7 days (first scan)" : "adaptive";
  const storageLabel = state.storageProvider === "aps" ? "APS (Anna Persistent Storage)" : "local JSON files";
  const mailboxes = state.mailboxes.length
    ? state.mailboxes
    : state.mailbox ? [{ email: state.mailbox, provider: "gmail", authorized: state.gmailAuthStatus.authorized, selected: true }] : [];
  return (
    <aside className={`drawer ${state.sourcesOpen ? "is-open" : ""}`} aria-label="Sources drawer">
      <div className="drawer-head">
        <div><h2 className="drawer-title">Sources</h2><p className="drawer-copy">Connected mailbox and scan strategy.</p></div>
        <button className="icon-btn" onClick={actions.closeDrawers}>x</button>
      </div>
      <div className="drawer-body">
        <section className="config-block">
          <h3>Mailboxes</h3>
          <div className="mailbox-list">
            {mailboxes.map((mailbox) => {
              const email = mailbox.email;
              const selected = state.selectedMailboxes.includes(email) || Boolean(mailbox.selected && !state.selectedMailboxes.length);
              const initials = (email || "A").charAt(0).toUpperCase();
              return (
                <article key={email} className={`source-card mailbox-card ${selected ? "is-selected" : ""}`}>
                  <label className="mailbox-source-row">
                    <input type="checkbox" checked={selected} onChange={(event) => void actions.setMailboxSelected(email, event.target.checked)} />
                    <span className="source-icon">{initials}</span>
                    <span className="mailbox-source-main">
                      <span className="source-name">{email}</span>
                      <span className="source-meta">
                        {(mailbox.provider || "gmail").toUpperCase()} · {mailbox.authorized === false ? "Re-auth needed" : "Connected"} · {modeLabel(state.strategyMode)}
                      </span>
                      <span className="source-meta">
                        {mailbox.last_scan_at ? `Last scan ${formatBeijingTimestamp(mailbox.last_scan_at)}` : "No scan yet"}
                        {typeof mailbox.card_count === "number" ? ` · ${mailbox.card_count} cards` : ""}
                      </span>
                      {mailbox.last_error ? <span className="source-meta is-error">{mailbox.last_error}</span> : null}
                    </span>
                  </label>
                </article>
              );
            })}
          </div>
          {!state.selectedMailboxes.length ? <p className="assistant-copy is-error">Select at least one mailbox to run Brief.</p> : null}
        </section>
        <section className="config-block">
          <h3>Scan window</h3>
          <ul className="config-list">
            <li>Range: <strong>{windowLabel}</strong></li>
            {totalScans > 0 ? <li>Incremental: stops at last processed message time.</li> : <li>First scan covers the full window above.</li>}
            <li>Max messages per scan: <strong>{maxMessages}</strong></li>
          </ul>
        </section>
        <section className="config-block">
          <h3>Storage</h3>
          <ul className="config-list">
            <li>Storage: {storageLabel}</li>
            {plan.include_newsletters ? <li>Including newsletters</li> : null}
            {plan.include_promotions ? <li>Including promotions</li> : null}
          </ul>
        </section>
      </div>
    </aside>
  );
}

const HISTORY_GROUPS = [
  { key: "brief", label: "Brief scans", icon: "📋" },
  { key: "ask", label: "Ask", icon: "🔍" },
  { key: "reply", label: "Replied", icon: "✉️" },
  { key: "handled", label: "Handled manually", icon: "✅" },
  { key: "no_action", label: "No action needed", icon: "✔️" },
  { key: "snooze", label: "Snoozed", icon: "🔕" },
  { key: "cleanup", label: "Cleaned up", icon: "📭" },
  { key: "trash", label: "Trashed", icon: "🗑️" },
  { key: "restore", label: "Restored", icon: "↩️" },
];

function classifyEntry(run: RunHistoryEntry): string {
  if (run.entry_type !== "card_action") {
    return run.strategy ? "brief" : "ask";
  }
  const action = run.action || "";
  if (action === "reply" || action === "reply_from_ask") return "reply";
  if (action === "handled_manually") return "handled";
  if (action === "no_action_needed") return "no_action";
  if (action === "snooze") return "snooze";
  if (action === "cleanup_read" || action === "mark_read_from_ask") return "cleanup";
  if (action === "trash_from_ask") return "trash";
  if (action === "restore") return "restore";
  return "other";
}

const ACTION_TITLES: Record<string, string> = {
  reply: "Replied", reply_from_ask: "Replied (Ask)",
  handled_manually: "Handled", no_action_needed: "No action",
  snooze: "Snoozed", cleanup_read: "Cleanup",
  mark_read_from_ask: "Marked read (Ask)", trash_from_ask: "Trashed (Ask)",
  restore: "Restored",
};

const RESTORABLE_ACTIONS = new Set(["snooze", "handled_manually", "no_action_needed", "cleanup_read"]);

function HistoryDrawer() {
  const { state, actions } = useApp();
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  // Entries that have been restored — hidden from list immediately
  const [restoredRunIds, setRestoredRunIds] = useState<Set<string>>(new Set());

  // Group entries
  const grouped: Record<string, RunHistoryEntry[]> = {};
  for (const run of state.history) {
    const gk = classifyEntry(run);
    (grouped[gk] ||= []).push(run);
  }

  const toggleGroup = (key: string) => setCollapsed((c) => ({ ...c, [key]: !c[key] }));

  const entryKey = (run: RunHistoryEntry) => run.run_id || `${run.ts}-${run.request}`;

  const toggleExpand = (run: RunHistoryEntry) => {
    const key = entryKey(run);
    setExpanded((c) => ({ ...c, [key]: !c[key] }));
  };

  // ── Snooze sub-filter ──────────────────────────────────────────

  const SNOOZE_FILTERS = [
    { key: "tomorrow", label: "Tomorrow" },
    { key: "next_week", label: "Next Week" },
    { key: "dont_prioritize", label: "Don't Prioritize" },
  ];
  const [snoozeFilter, setSnoozeFilter] = useState<string>("tomorrow");

  const _filterVisible = (entries: RunHistoryEntry[]) => {
    const byFilter = new Map<string, RunHistoryEntry[]>();
    for (const e of entries) {
      const k = e.detail || "";
      if (!byFilter.has(k)) byFilter.set(k, []);
      byFilter.get(k)!.push(e);
    }
    return { byFilter, selected: byFilter.get(snoozeFilter) || [] };
  };

  const handleRestore = (run: RunHistoryEntry) => {
    if (!run.card_id) return;
    setRestoredRunIds((prev) => new Set(prev).add(entryKey(run)));
    void actions.restoreCard(run.card_id, run.mailbox);
  };

  const renderEntry = (run: RunHistoryEntry, hideDetail?: boolean) => {
    const isCard = run.entry_type === "card_action";
    const isExpanded = expanded[entryKey(run)] || false;
    const hasCardContext = !!(run.card_from || run.card_subject || run.card_summary || run.card_body);
    const canRestore = isCard && RESTORABLE_ACTIONS.has(run.action || "") && !!run.card_id;

    const title = isCard
      ? (run.card_title || run.card_id || "")
      : (run.request || run.strategy || "Mailbox scan");

    return (
      <div className="history-entry" key={entryKey(run)}>
        <div
          className={`history-entry-row ${isCard ? "is-expandable" : ""}`}
          onClick={isCard ? () => toggleExpand(run) : undefined}
        >
          <span className="history-entry-time">{formatBeijingTimestamp(run.ts)}</span>
          <span className="history-entry-text">{title}</span>
          {run.detail && !hideDetail ? <span className="history-entry-detail">{run.detail}</span> : null}
          {run.result && !isCard ? <span className="history-entry-detail">{run.result}</span> : null}
          {canRestore ? (
            <button
              className="history-restore-btn"
              type="button"
              title="Restore"
              aria-label="Restore card"
              onClick={(e) => { e.stopPropagation(); handleRestore(run); }}
            >
              <RestoreIcon />
            </button>
          ) : null}
          {isCard ? <span className="history-entry-arrow">{isExpanded ? "▾" : "▸"}</span> : null}
        </div>
        {isExpanded ? (
          <div className="history-entry-body">
            {hasCardContext ? (
              <>
                {run.card_from ? <div className="history-entry-line"><span className="history-entry-label">From</span> {run.card_from}</div> : null}
                {run.card_subject ? <div className="history-entry-line"><span className="history-entry-label">Subject</span> {run.card_subject}</div> : null}
                {run.card_summary ? <div className="history-entry-line"><span className="history-entry-label">Summary</span> {run.card_summary}</div> : null}
                {(!run.card_summary && run.card_body) ? <div className="history-entry-line">{run.card_body}</div> : null}
              </>
            ) : (
              <div className="history-entry-line">Card details will appear here after the next mail scan.</div>
            )}
          </div>
        ) : null}
      </div>
    );
  };

  const visibleGroups = HISTORY_GROUPS.filter((g) => (grouped[g.key]?.length || 0) > 0);
  const hasAny = visibleGroups.length > 0;

  return (
    <aside className={`drawer ${state.historyOpen ? "is-open" : ""}`} aria-label="History drawer">
      <div className="drawer-head">
        <div><h2 className="drawer-title">History</h2><p className="drawer-copy">All scans and card actions you've taken.</p></div>
        <button className="icon-btn" onClick={actions.closeDrawers}>×</button>
      </div>
      <div className="drawer-body">
        {!hasAny ? <p className="assistant-copy">No history yet.</p> : null}
        {visibleGroups.map((g) => {
          const allEntries = (grouped[g.key] || []).filter((e) => !restoredRunIds.has(entryKey(e)));
          const isCollapsed = collapsed[g.key] || false;
          const isSnooze = g.key === "snooze";
          const { byFilter, selected } = isSnooze ? _filterVisible(allEntries) : { byFilter: new Map(), selected: allEntries };
          const displayed = isSnooze ? selected : allEntries;

          return (
            <section className="history-group" key={g.key}>
              <button className="history-group-header" onClick={() => toggleGroup(g.key)}>
                <span className="history-group-icon">{g.icon}</span>
                <span className="history-group-label">{g.label}</span>
                <span className="history-group-count">{allEntries.length}</span>
                <span className="history-group-arrow">{isCollapsed ? "▸" : "▾"}</span>
              </button>
              {!isCollapsed ? (
                <div className="history-group-body">
                  {isSnooze ? (
                    <div className="history-subfilter-row">
                      {SNOOZE_FILTERS.map((f) => {
                        const count = byFilter.get(f.key)?.length || 0;
                        return (
                          <button
                            key={f.key}
                            className={`preset-chip ${snoozeFilter === f.key ? "is-active" : ""}`}
                            onClick={() => setSnoozeFilter(f.key)}
                          >
                            {f.label}{count ? ` (${count})` : ""}
                          </button>
                        );
                      })}
                    </div>
                  ) : null}
                  {displayed.map((e) => renderEntry(e, isSnooze))}
                </div>
              ) : null}
            </section>
          );
        })}
      </div>
    </aside>
  );
}

function ScanPlanDrawer() {
  const { state, actions } = useApp();
  const plan = state.scanPlan || {};
  const rangeLabels: Record<string, string> = { auto: "Adaptive (auto)", since_last: "Since last brief", last_24h: "Last 24 hours", last_7d: "Last 7 days", unread_backlog: "Unread backlog" };
  return (
    <aside className={`drawer ${state.scanPlanOpen ? "is-open" : ""}`} aria-label="Scan plan drawer">
      <div className="drawer-head">
        <div><h2 className="drawer-title">Next Scan</h2><p className="drawer-copy">Configure when and how Anna scans your inbox.</p></div>
        <button className="icon-btn" onClick={actions.closeDrawers}>×</button>
      </div>
      <div className="drawer-body">
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
