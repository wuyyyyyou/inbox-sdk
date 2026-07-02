import { Fragment, useState } from "react";
import { modeLabel } from "../../app/constants";
import { useApp } from "../../app/AppContext";
import { formatBeijingTimestamp } from "../../shared/format";
import type { ContactMemorySummary, ContactThreadMemory, RunHistoryEntry } from "../../types/mail";

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
  const overlayOpen = state.sourcesOpen || state.historyOpen || state.memoryOpen || state.scanPlanOpen;
  return (
    <>
      <div className={`drawer-overlay ${overlayOpen ? "is-open" : ""}`} onClick={actions.closeDrawers} />
      <SourcesDrawer />
      <MemoryDrawer />
      <HistoryDrawer />
      <aside className="drawer" aria-label="Original message drawer" />
    </>
  );
}

function PerMailboxConfig() {
  const { state, actions } = useApp();
  const plan = state.scanPlan || {};
  const windowDays = plan.scan_window_days || 7;
  const maxMsgs = plan.max_messages || 100;
  const isCustomWindow = ![4, 7, 14].includes(windowDays);
  const isCustomMax = ![50, 100, 150].includes(maxMsgs);
  const [customWindow, setCustomWindow] = useState(isCustomWindow ? String(windowDays) : "");
  const [customMax, setCustomMax] = useState(isCustomMax ? String(maxMsgs) : "");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmFreshScan, setConfirmFreshScan] = useState(false);
  const [busyAction, setBusyAction] = useState<"" | "continue" | "reset" | "delete">("");
  const save = actions.saveScanPlanField;
  const mailboxEmail = state.configMailbox || state.mailbox;
  const scanInProgress = state.isScanning || state.isPreparingScan;
  const buttonsDisabled = Boolean(busyAction || scanInProgress || !state.runtime.connected);
  const clampInt = (value: number, min: number, max: number) => Math.min(max, Math.max(min, Math.round(value)));
  const saveWindowDays = (value: number) => {
    const next = clampInt(value, 1, 90);
    setCustomWindow([4, 7, 14].includes(next) ? "" : String(next));
    void save("scan_window_days", next);
  };
  const saveMaxEmails = (value: number) => {
    const next = clampInt(value, 10, 500);
    setCustomMax([50, 100, 150].includes(next) ? "" : String(next));
    void save("max_messages", next);
  };

  return (
    <div className="mailbox-config-body">
      <p className="mailbox-config-hint">Scan settings are strict for this mailbox.</p>
      <div className="mailbox-config-section">
        <h4 className="mailbox-config-label">Scan window</h4>
        <div className="preset-row preset-row-sm">
          {[4, 7, 14].map((d) => <button key={d} className={`preset-chip ${windowDays === d ? "is-active" : ""}`} onClick={() => saveWindowDays(d)}>{d}d</button>)}
          <button className="custom-step-btn" onClick={() => saveWindowDays(windowDays - 1)}>−</button>
          <input className={`custom-days-input inline${isCustomWindow ? " is-custom" : ""}`} type="number" min={1} max={90} placeholder="days" value={isCustomWindow ? String(windowDays) : customWindow} onChange={(e) => { setCustomWindow(e.target.value); if (e.target.value) saveWindowDays(Number(e.target.value)); }} />
          <button className="custom-step-btn" onClick={() => saveWindowDays(windowDays + 1)}>+</button>
        </div>
      </div>
      <div className="mailbox-config-section">
        <h4 className="mailbox-config-label">Max emails</h4>
        <div className="preset-row preset-row-sm">
          {[50, 100, 150].map((n) => <button key={n} className={`preset-chip ${maxMsgs === n ? "is-active" : ""}`} onClick={() => saveMaxEmails(n)}>{n}</button>)}
          <button className="custom-step-btn" onClick={() => saveMaxEmails(maxMsgs - 10)}>−</button>
          <input className={`custom-days-input inline${isCustomMax ? " is-custom" : ""}`} type="number" min={10} max={500} placeholder="emails" value={isCustomMax ? String(maxMsgs) : customMax} onChange={(e) => { setCustomMax(e.target.value); if (e.target.value) saveMaxEmails(Number(e.target.value)); }} />
          <button className="custom-step-btn" onClick={() => saveMaxEmails(maxMsgs + 10)}>+</button>
        </div>
      </div>
      <div className="mailbox-config-section">
        <div className="mailbox-scan-action-row">
          <button
            className="soft-btn compact danger mailbox-scan-action"
            disabled={buttonsDisabled}
            title={scanInProgress ? "A mailbox scan is already in progress." : "Delete Brief history for this mailbox and scan from scratch."}
            aria-label={scanInProgress ? "Scanning" : "Delete history and scan again"}
            aria-busy={scanInProgress}
            onClick={() => setConfirmFreshScan(true)}
          >
            {scanInProgress ? "Scanning…" : "Reset & Scan"}
          </button>
          <button
            className="primary-btn mailbox-scan-action"
            disabled={buttonsDisabled}
            title={scanInProgress ? "A mailbox scan is already in progress." : "Keep existing records and continue scanning new mail."}
            aria-label={scanInProgress ? "Scanning" : "Keep current records and continue scanning"}
            aria-busy={scanInProgress}
            onClick={() => {
              setBusyAction("continue");
              void actions.startScan("continue", mailboxEmail).finally(() => setBusyAction(""));
            }}
          >
            {scanInProgress ? "Scanning…" : "Continue Scan"}
          </button>
        </div>
      </div>
      <div style={{ marginTop: 12 }}>
        <div style={{ display: "flex", justifyContent: "center" }}>
          <button className="danger-btn mailbox-delete-data-btn" style={{ fontSize: 12, padding: "6px 14px", borderRadius: 8 }} disabled={buttonsDisabled} onClick={() => setConfirmDelete(true)}>Delete mailbox data</button>
        </div>
        <p className="drawer-copy" style={{ marginTop: 4 }}>Clears all cards, cache, contact memory, and scan history for this mailbox only.</p>
      </div>
      {confirmFreshScan ? (
        <div className="confirm-overlay" onClick={() => setConfirmFreshScan(false)}>
          <div className="confirm-dialog" onClick={(e) => e.stopPropagation()}>
            <p>This clears Brief cards, scan history, and processed markers for <strong>{mailboxEmail}</strong>, then starts a fresh scan. Scan settings, cache, and contact memory stay intact.</p>
            <div className="confirm-actions">
              <button className="soft-btn" disabled={busyAction === "reset"} onClick={() => setConfirmFreshScan(false)}>Cancel</button>
              <button
                className="primary-btn danger-btn"
                disabled={busyAction === "reset"}
                onClick={() => {
                  setConfirmFreshScan(false);
                  setBusyAction("reset");
                  void actions.resetAndStartScan(mailboxEmail).finally(() => setBusyAction(""));
                }}
              >
                Reset &amp; Scan
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {confirmDelete ? (
        <div className="confirm-overlay" onClick={() => setConfirmDelete(false)}>
          <div className="confirm-dialog" onClick={(e) => e.stopPropagation()}>
            <p>This will delete all data for <strong>{mailboxEmail}</strong>, including cards, cached emails, contact memory, scan history, and run records. Other mailboxes will not be affected. <strong>This cannot be undone.</strong></p>
            <div className="confirm-actions">
              <button className="soft-btn" disabled={busyAction === "delete"} onClick={() => setConfirmDelete(false)}>Cancel</button>
              <button
                className="primary-btn danger-btn"
                disabled={busyAction === "delete"}
                onClick={() => {
                  setConfirmDelete(false);
                  setBusyAction("delete");
                  void actions.deleteMailboxData(mailboxEmail).finally(() => setBusyAction(""));
                }}
              >
                Delete mailbox data
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function SourcesDrawer() {
  const { state, actions } = useApp();
  const [confirmReset, setConfirmReset] = useState(false);
  const storageLabel = state.storageProvider === "aps" ? "APS (Anna Persistent Storage)" : "local JSON files";
  const plan = state.scanPlan || {};
  const expanded = state.configMailbox;
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
              const selected = normalizeMailbox(state.mailbox) === normalizeMailbox(email);
              const initials = (email || "A").charAt(0).toUpperCase();
              const isExpanded = expanded === email;
              return (
                <Fragment key={email}>
                  <article key={email} className={`source-card mailbox-card ${selected ? "is-selected" : ""}`}>
                    <div className="mailbox-source-row" style={{ cursor: "pointer" }} onClick={() => { void actions.switchMailbox(email); }}>
                      <span className={`source-account-radio ${selected ? "is-selected" : ""}`} aria-hidden="true">{selected ? "✓" : ""}</span>
                      <span className="source-icon">{initials}</span>
                      <span className="mailbox-source-main">
                        <span className="source-name">{email}</span>
                        <span className="source-meta">
                          {(mailbox.provider || "gmail").toUpperCase()} · <span className={mailbox.authorized === false ? "auth-status is-off" : "auth-status is-on"}>{mailbox.authorized === false ? "Re-auth needed" : "Connected"}</span> · {modeLabel(state.strategyMode)}
                        </span>
                        <span className="source-meta">
                          {mailbox.last_scan_at ? `Last scan ${formatBeijingTimestamp(mailbox.last_scan_at)}` : "No scan yet"}
                          {typeof mailbox.card_count === "number" ? ` · ${mailbox.card_count} cards` : ""}
                        </span>
                        {mailbox.last_error ? <span className="source-meta is-error">{mailbox.last_error}</span> : null}
                      </span>
                      <button className="mailbox-config-arrow" title="Account settings" onClick={(event) => {
                        event.stopPropagation();
                        const next = isExpanded ? "" : email;
                        void actions.setConfigMailbox(next);
                      }}>{isExpanded ? "▼" : "◀"}</button>
                    </div>
                  </article>
                  {isExpanded ? <PerMailboxConfig /> : null}
                </Fragment>
              );
            })}
          </div>
        </section>
        <section className="config-block">
          <h3>Storage</h3>
          <ul className="config-list">
            <li>{storageLabel}</li>
          </ul>
          <div style={{ marginTop: 12 }}>
            <button className="danger-btn" style={{ fontSize: 12, padding: "6px 14px", borderRadius: 8 }} onClick={() => setConfirmReset(true)}>Reset all data</button>
            <p className="drawer-copy" style={{ marginTop: 4 }}>Clears all cards, history, cache, and scan state.</p>
          </div>
        </section>
        {confirmReset ? (
          <div className="confirm-overlay" onClick={() => setConfirmReset(false)}>
            <div className="confirm-dialog" onClick={(e) => e.stopPropagation()}>
              <p>This will delete all persistent data including cards, history, Gmail cache, and scan state. The app will reload. <strong>This cannot be undone.</strong></p>
              <div className="confirm-actions">
                <button className="soft-btn" onClick={() => setConfirmReset(false)}>Cancel</button>
                <button className="primary-btn danger-btn" onClick={() => { void actions.resetAllData(); }}>Reset everything</button>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </aside>
  );
}

function normalizeMailbox(mailbox: string | undefined): string {
  return String(mailbox || "").trim().toLowerCase();
}

function memoryKey(item: ContactMemorySummary): string {
  return `${String(item.mailbox || "").trim().toLowerCase()}::${String(item.contact_email || "").trim().toLowerCase()}`;
}

function statusLabel(status?: string): string {
  if (status === "open") return "Open";
  if (status === "waiting_for_them") return "Waiting";
  if (status === "closed") return "Closed";
  return "Unknown";
}

function normalizeMemoryText(value?: string): string {
  return String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
}

function isRepeatedMemoryText(value: string | undefined, seen: string[]): boolean {
  const normalized = normalizeMemoryText(value);
  if (!normalized) return true;
  return seen.some((item) => {
    const other = normalizeMemoryText(item);
    if (!other) return false;
    if (normalized === other) return true;
    if (normalized.length >= 24 && other.includes(normalized)) return true;
    return other.length >= 24 && normalized.includes(other);
  });
}

function cleanMemoryText(value?: string): string {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const tagStart = raw.search(/<\s*[a-z][^>]*>/i);
  if (tagStart > 24) return raw.slice(0, tagStart).trim();
  return raw
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<br\s*\/?>/gi, " ")
    .replace(/<\/p>|<\/div>|<\/li>|<\/tr>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, "\"")
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/\s+/g, " ")
    .trim();
}

function isGenericMemoryState(value?: string): boolean {
  const normalized = normalizeMemoryText(value);
  return normalized === "the contact may be waiting for the user."
    || normalized === "the user has replied and is waiting for the contact."
    || normalized === "no follow-up is currently needed."
    || normalized === "the current state is unclear.";
}

function MemoryDrawer() {
  const { state, actions } = useApp();
  const [expandedThreads, setExpandedThreads] = useState<Record<string, boolean>>({});
  const allContacts = state.contactMemories || [];
  const selected = state.selectedMemory;
  const selectedMailboxes = state.selectedMailboxes.length ? state.selectedMailboxes : [state.mailbox].filter(Boolean);

  // Mailbox filter
  const uniqueMailboxes = [...new Set(allContacts.map((c) => normalizeMailbox(c.mailbox)).filter(Boolean))].sort();
  const [memoryMailboxFilter, setMemoryMailboxFilter] = useState<string[]>([]);
  const activeMemoryFilter = memoryMailboxFilter.length ? memoryMailboxFilter : uniqueMailboxes;
  const memoryFilterSet = new Set(activeMemoryFilter);
  const contacts = allContacts.filter((c) => memoryFilterSet.has(normalizeMailbox(c.mailbox)));

  const selectedSummary = contacts.find((item) => memoryKey(item) === state.selectedMemoryKey);
  const totalThreads = contacts.reduce((sum, item) => sum + (item.thread_count || 0), 0);
  const totalMessages = contacts.reduce((sum, item) => sum + (item.message_count || 0), 0);

  const toggleMemoryMailbox = (mailbox: string) => {
    if (activeMemoryFilter.length === uniqueMailboxes.length) {
      setMemoryMailboxFilter(uniqueMailboxes.filter((m) => m !== mailbox));
    } else if (memoryFilterSet.has(mailbox)) {
      if (activeMemoryFilter.length === 1) return;
      setMemoryMailboxFilter(activeMemoryFilter.filter((m) => m !== mailbox));
    } else {
      setMemoryMailboxFilter([...activeMemoryFilter, mailbox]);
    }
  };

  const openContact = (item: ContactMemorySummary) => {
    if (memoryKey(item) === state.selectedMemoryKey) {
      actions.closeContactMemory();
      return;
    }
    void actions.openContactMemory(item.mailbox, item.contact_email);
  };

  const deleteSelected = () => {
    if (!selected) return;
    const label = selected.display_name || selected.contact_email;
    if (!window.confirm(`Delete memory for ${label}?`)) return;
    void actions.deleteContactMemory(selected.mailbox, selected.contact_email);
  };

  const clearAll = () => {
    const scope = selectedMailboxes.join(", ") || "selected mailboxes";
    if (!window.confirm(`Clear all contact memory for ${scope}?`)) return;
    void actions.clearContactMemories();
  };

  const toggleThread = (threadId: string) => {
    setExpandedThreads((current) => ({ ...current, [threadId]: !current[threadId] }));
  };

  const renderMemoryDetail = () => (
    <section className="memory-detail">
      <div className="memory-detail-head">
        <div>
          <h3>{selected?.display_name || selectedSummary?.display_name || selected?.contact_email || selectedSummary?.contact_email}</h3>
          <p>{selected?.contact_email || selectedSummary?.contact_email} -&gt; {selected?.mailbox || selectedSummary?.mailbox}</p>
        </div>
        <button className="soft-btn compact danger" onClick={deleteSelected} disabled={!selected}>Delete</button>
      </div>
      {selected?.threads?.length ? (
        <div className="memory-thread-list">
          {selected.threads.map((thread: ContactThreadMemory) => {
            const open = expandedThreads[thread.thread_id] || false;
            const summary = thread.thread_summary || {};
            const headline = cleanMemoryText(summary.summary || summary.current_state || "No summary");
            const detailSeen = [headline];
            const currentState = cleanMemoryText(summary.current_state || "");
            if (currentState) detailSeen.push(currentState);
            const cleanOpenLoop = cleanMemoryText(summary.open_loop || "");
            const openLoop = isGenericMemoryState(cleanOpenLoop) || isRepeatedMemoryText(cleanOpenLoop, detailSeen) ? "" : cleanOpenLoop;
            if (openLoop) detailSeen.push(openLoop);
            const visibleMessages = (thread.message_summaries || [])
              .map((message) => ({ ...message, summary: cleanMemoryText(message.summary || "") }))
              .filter((message) => {
                if (isRepeatedMemoryText(message.summary, detailSeen)) return false;
                detailSeen.push(message.summary || "");
                return true;
              });
            return (
              <article className="memory-thread" key={thread.thread_id}>
                <button className="memory-thread-head" onClick={() => toggleThread(thread.thread_id)}>
                  <span>
                    <span className="memory-thread-title">{thread.subject || "Untitled thread"}</span>
                    <span className="memory-thread-summary">{headline}</span>
                  </span>
                  <span className="memory-thread-side">
                    <span className={`memory-status-dot status-${summary.status || "unknown"}`} title={statusLabel(summary.status)} />
                    <span className={`memory-thread-toggle ${open ? "is-open" : ""}`} aria-hidden="true">{open ? "-" : "+"}</span>
                  </span>
                </button>
                {open ? (
                  <div className="memory-thread-body">
                    {openLoop ? <p><span>Open loop</span>{openLoop}</p> : null}
                    {visibleMessages.length ? (
                      <div className="memory-message-list">
                        {visibleMessages.map((message) => (
                          <div className="memory-message" key={message.message_id || `${message.date}-${message.summary}`}>
                            <span className="memory-message-meta">{message.direction || "inbound"} at {formatBeijingTimestamp(message.date || "")}</span>
                            <span>{message.summary || "No message summary"}</span>
                          </div>
                        ))}
                      </div>
                    ) : null}
                    {!openLoop && !visibleMessages.length ? <p className="memory-empty-detail">No extra detail beyond the thread summary.</p> : null}
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : (
        <p className="assistant-copy">{state.memoryLoading ? "Loading memory..." : "No thread memory for this contact."}</p>
      )}
    </section>
  );

  return (
    <aside className={`drawer memory-drawer ${state.memoryOpen ? "is-open" : ""}`} aria-label="Memory drawer">
      <div className="drawer-head">
        <div>
          <h2 className="drawer-title">Memory</h2>
          <p className="drawer-copy">Mailbox-scoped contact memory.</p>
        </div>
        <button className="icon-btn" onClick={actions.closeDrawers}>x</button>
      </div>
      <div className="drawer-body memory-body">
        <section className="memory-toolbar">
          <div>
            <div className="memory-count">{contacts.length} contacts</div>
            <div className="memory-meta">{totalThreads} threads · {totalMessages} messages</div>
          </div>
          <div className="memory-actions">
            <button className="soft-btn compact" onClick={() => void actions.loadContactMemories()} disabled={state.memoryLoading}>Refresh</button>
            <button className="soft-btn compact danger" onClick={clearAll} disabled={!contacts.length || state.memoryLoading}>Clear</button>
          </div>
        </section>

        {state.memoryError ? <div className="memory-error">{state.memoryError}</div> : null}
        {state.memoryLoading && !allContacts.length ? <p className="assistant-copy">Loading memory...</p> : null}
        {!state.memoryLoading && !allContacts.length && !state.memoryError ? <p className="assistant-copy">No contact memory yet.</p> : null}

        {uniqueMailboxes.length > 1 ? (
          <section className="memory-mailbox-filter" style={{ display: "flex", gap: 6, flexWrap: "wrap", padding: "0 0 10px" }}>
            {uniqueMailboxes.map((mb) => {
              const active = memoryFilterSet.has(mb);
              const count = allContacts.filter((c) => normalizeMailbox(c.mailbox) === mb).length;
              return (
                <button key={mb} className={`preset-chip${active ? " is-active" : ""}`} style={{ fontSize: 11 }} onClick={() => toggleMemoryMailbox(mb)}>
                  {mb} ({count})
                </button>
              );
            })}
          </section>
        ) : null}

        <section className="memory-list">
          {contacts.map((item) => {
            const active = memoryKey(item) === state.selectedMemoryKey;
            const name = item.display_name || item.contact_email;
            return (
              <div className="memory-contact-block" key={memoryKey(item)}>
                <button className={`memory-contact ${active ? "is-active" : ""}`} onClick={() => openContact(item)}>
                  <span className="memory-avatar">{name.charAt(0).toUpperCase()}</span>
                  <span className="memory-contact-main">
                    <span className="memory-contact-title">{name}</span>
                    <span className="memory-contact-email">{item.contact_email}</span>
                    <span className="memory-contact-subject">{item.latest_subject || "No subject"}</span>
                  </span>
                  <span className="memory-contact-side">
                    <span className="memory-contact-count">{item.thread_count || 0}t</span>
                    <span className={`memory-contact-toggle ${active ? "is-open" : ""}`} aria-hidden="true">{active ? "-" : "+"}</span>
                  </span>
                </button>
                {active ? renderMemoryDetail() : null}
              </div>
            );
          })}
        </section>
      </div>
    </aside>
  );
}

const HISTORY_GROUPS = [
  { key: "brief", label: "Brief scans", icon: "📋" },
  { key: "ask", label: "Ask", icon: "🔍" },
  { key: "reply", label: "Replied", icon: "✉️" },
  { key: "read", label: "Read", icon: "📖" },
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
  if (action === "read") return "read";
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
  read: "Read",
  handled_manually: "Handled", no_action_needed: "No action",
  snooze: "Snoozed", cleanup_read: "Cleanup",
  mark_read_from_ask: "Marked read (Ask)", trash_from_ask: "Trashed (Ask)",
  restore: "Restored",
};

const RESTORABLE_ACTIONS = new Set(["read", "snooze", "handled_manually", "no_action_needed", "cleanup_read"]);

function HistoryDrawer() {
  const { state, actions } = useApp();
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const restoredCardIds = state.restoredCardIds;

  // Mailbox filter
  const allHistory = state.history || [];
  const historyMailboxes = [...new Set(allHistory.map((r) => normalizeMailbox(r.mailbox)).filter(Boolean))].sort();
  const [historyMailboxFilter, setHistoryMailboxFilter] = useState<string[]>([]);
  const activeHistoryFilter = historyMailboxFilter.length ? historyMailboxFilter : historyMailboxes;
  const historyFilterSet = new Set(activeHistoryFilter);

  // Group entries (filtered by mailbox)
  const grouped: Record<string, RunHistoryEntry[]> = {};
  for (const run of allHistory) {
    if (!historyFilterSet.has(normalizeMailbox(run.mailbox))) continue;
    const gk = classifyEntry(run);
    (grouped[gk] ||= []).push(run);
  }

  const toggleHistoryMailbox = (mailbox: string) => {
    if (activeHistoryFilter.length === historyMailboxes.length) {
      setHistoryMailboxFilter(historyMailboxes.filter((m) => m !== mailbox));
    } else if (historyFilterSet.has(mailbox)) {
      if (activeHistoryFilter.length === 1) return;
      setHistoryMailboxFilter(activeHistoryFilter.filter((m) => m !== mailbox));
    } else {
      setHistoryMailboxFilter([...activeHistoryFilter, mailbox]);
    }
  };

  const toggleGroup = (key: string) => setCollapsed((c) => ({ ...c, [key]: !c[key] }));

  const entryKey = (run: RunHistoryEntry) => run.run_id || `${run.ts}-${run.request}`;

  // Persisted restore check: find card_ids that have a "restore" entry AFTER
  // a restorable entry → that older entry is consumed (persists across refresh)
  const consumedEntryIds = new Set<string>();
  const lastRestoreTs: Record<string, string> = {};
  for (const r of state.history) {
    if (r.entry_type === "card_action" && r.action === "restore" && r.card_id && r.ts) {
      if (!lastRestoreTs[r.card_id] || r.ts > lastRestoreTs[r.card_id]) {
        lastRestoreTs[r.card_id] = r.ts;
      }
    }
  }
  for (const r of state.history) {
    if (r.entry_type === "card_action" && RESTORABLE_ACTIONS.has(r.action || "") && r.card_id && r.ts) {
      const lastR = lastRestoreTs[r.card_id];
      if (lastR && r.ts < lastR) {
        consumedEntryIds.add(entryKey(r));
      }
    }
  }

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
    void actions.restoreCard(run.card_id, run.mailbox);
  };

  const scanSummary = (run: RunHistoryEntry) => {
    if (!run.result || run.entry_type === "card_action") return null;
    const parts = run.result.split(", ");
    const metrics: { value: string; label: string; color: string }[] = [];
    for (const p of parts) {
      const text = p.trim();
      const scanned = text.match(/^Scanned\s+(\d+)\s+emails?/i);
      const counted = text.match(/^(\d+)\s+(.+)$/);
      if (scanned) metrics.push({ value: scanned[1], label: "emails scanned", color: "scanned" });
      else if (text.includes("needs reply") && counted) metrics.push({ value: counted[1], label: "needs reply", color: "reply" });
      else if (text.includes("needs review") && counted) metrics.push({ value: counted[1], label: "needs review", color: "review" });
      else if (text.includes("cleanup") && counted) metrics.push({ value: counted[1], label: "cleanup", color: "cleanup" });
    }
    if (!metrics.length) return null;
    return (
      <div className="history-scan-card">
        <div className="history-scan-metrics">
          {metrics.map((metric, i) => (
            <span key={i} className={`history-scan-metric history-scan-metric-${metric.color}`}>
              <span className="history-scan-value">{metric.value}</span>
              <span className="history-scan-label">{metric.label}</span>
            </span>
          ))}
        </div>
        {run.mailbox ? <div className="history-scan-mailbox">Mailbox · {run.mailbox}</div> : null}
      </div>
    );
  };

  const renderEntry = (run: RunHistoryEntry, hideDetail?: boolean) => {
    const isCard = run.entry_type === "card_action";
    const isExpanded = expanded[entryKey(run)] || false;
    const hasCardContext = !!(run.card_from || run.card_subject || run.card_summary || run.card_body);
    const canRestore = isCard && RESTORABLE_ACTIONS.has(run.action || "") && !!run.card_id && !restoredCardIds.has(run.card_id) && !consumedEntryIds.has(entryKey(run));

    const title = isCard
      ? (run.card_title || run.card_id || "")
      : (run.request || run.strategy || "Mailbox scan");
    const scanBlock = !isCard ? scanSummary(run) : null;
    const scanLines = !isCard
      ? (scanBlock ? [] : [run.result, run.summary, run.mailbox ? `Mailbox: ${run.mailbox}` : ""].filter(Boolean))
      : [];

    return (
      <div className="history-entry" key={entryKey(run)}>
        <div
          className={`history-entry-row ${isCard ? "is-expandable" : ""} ${scanBlock ? "has-tags" : ""}`}
          onClick={isCard ? () => toggleExpand(run) : undefined}
        >
          <span className="history-entry-time">{formatBeijingTimestamp(run.ts)}</span>
          <span className="history-entry-text">{title}</span>
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
        {scanBlock}
        {scanLines.length ? (
          <div className="history-entry-body is-scan">
            {scanLines.map((line, index) => <div className="history-entry-line" key={index}>{line}</div>)}
          </div>
        ) : null}
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
        {historyMailboxes.length > 1 ? (
          <section className="history-mailbox-filter" style={{ display: "flex", gap: 6, flexWrap: "wrap", padding: "0 0 10px" }}>
            {historyMailboxes.map((mb) => {
              const active = historyFilterSet.has(mb);
              const count = allHistory.filter((r) => normalizeMailbox(r.mailbox) === mb).length;
              return (
                <button key={mb} className={`preset-chip${active ? " is-active" : ""}`} style={{ fontSize: 11 }} onClick={() => toggleHistoryMailbox(mb)}>
                  {mb} ({count})
                </button>
              );
            })}
          </section>
        ) : null}
        {!hasAny ? <p className="assistant-copy">No history yet.</p> : null}
        {visibleGroups.map((g) => {
          const allEntries = (grouped[g.key] || []).filter((e) => e.action === "restore" || (!restoredCardIds.has(e.card_id || "") && !consumedEntryIds.has(entryKey(e))));
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
