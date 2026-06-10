import { useState } from "react";
import { CATEGORY_NOTE, CATEGORY_TABS } from "../../app/constants";
import { useApp } from "../../app/AppContext";
import { scanProgressLabel, SCAN_STEPS } from "./runHelpers";
import type { CleanupMessage, FrontendCard } from "../../types/mail";
import { formatBeijingTimestamp } from "../../shared/format";
import {
  filteredCards,
  isMainCard,
  lowerCards,
  mainCards,
  normalizeRecommendation,
  primaryAction,
  visibleCards,
} from "./cardHelpers";

function CardDetails({ card }: { card: FrontendCard }) {
  const details = card.details || {};
  const original = card.original || {};
  const body = String(original.body || "").trim();
  return (
    <div className="attention-details">
      <div className="attention-detail-grid">
        <div className="detail-line"><strong>Needs</strong>{details.needs || "Review"}</div>
        <div className="detail-line"><strong>Latest activity</strong>{details.latestActivity || ""}</div>
        <div className="detail-line"><strong>Anna reviewed</strong>{details.reviewed || ""}</div>
        <div className="detail-line"><strong>Mailbox</strong>{details.mailbox || ""}</div>
      </div>
      {body ? (
        <div className="original-email-block">
          {original.thread ? <div className="original-email-subject">{original.thread}</div> : null}
          <pre className="original-email-body">{body}</pre>
        </div>
      ) : null}
    </div>
  );
}

function AttentionCard({ card }: { card: FrontendCard }) {
  const { state, actions } = useApp();
  const key = card.uiKey || card.id;
  const mailbox = card.details?.mailbox || "";
  const expanded = Boolean(state.expandedDetails[key]);
  const snoozeOpen = state.snoozeMenuCardId === key;
  const status = state.statusByCardId[key];
  const action = primaryAction(card);
  const recommendation = normalizeRecommendation(card.recommendation);
  const isResolved = card.status === "resolved" || card.status === "snoozed";
  const resolutionLabel: Record<string, string> = { replied: "Replied", no_action_needed: "No action", handled_manually: "Handled", dismissed: "Dismissed" };
  const resolvedLabel = card.status === "snoozed" ? "Snoozed" : (resolutionLabel[card.resolution || ""] || "Resolved");
  return (
    <article className={`attention-item-card ${expanded ? "is-expanded" : ""} ${isResolved ? "is-resolved" : ""}`}>
      <div className="attention-card-head">
        <h2 className="attention-title"><span className={`priority-badge priority-${card.priority || "low"}`}>{card.priority || "low"}</span>{card.title || "Email thread needs review"}</h2>
        {state.selectedMailboxes.length > 1 && mailbox ? (
          <span className="attention-card-mailbox"><span className="mailbox-dot" />{mailbox}</span>
        ) : null}
        <button className="card-details-btn" onClick={() => actions.toggleDetails(key)}>{expanded ? "Hide" : "Details"}</button>
      </div>
      <p className="attention-field">{card.summary || ""}</p>
      <p className="attention-field attention-recommendation">
        <span className="suggested-prefix">Suggested: </span>{recommendation.replace(/^Suggested:\s*/i, "")}
      </p>
      <div className="proposal-actions">
        {isResolved ? (
          <span className="resolution-row">
            {card.resolution !== "replied" ? (
              <button className="history-restore-btn" title="Restore" aria-label="Restore card" onClick={(e) => { e.stopPropagation(); void actions.restoreCard(card.id, card.details?.mailbox); }}>
                <svg className="history-restore-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15.4-6.4L3 13"/></svg>
              </button>
            ) : null}
            <span className="resolution-label">{resolvedLabel}</span>
          </span>
        ) : (
          <>
            <span className="snooze-wrap">
              <button className="soft-btn" aria-expanded={snoozeOpen} onClick={() => actions.toggleSnoozeMenu(key)}>Snooze</button>
              {snoozeOpen ? (
                <>
                  <span className="snooze-backdrop" onClick={() => actions.toggleSnoozeMenu(key)} />
                  <span className="snooze-menu" role="menu">
                    <button className="snooze-option" role="menuitem" onClick={() => { actions.toggleSnoozeMenu(key); void actions.snoozeCard(key, "tomorrow"); }}>Tomorrow</button>
                    <button className="snooze-option" role="menuitem" onClick={() => { actions.toggleSnoozeMenu(key); void actions.snoozeCard(key, "next-week"); }}>Next week</button>
                    <button className="snooze-option" role="menuitem" onClick={() => { actions.toggleSnoozeMenu(key); actions.openSnoozeReasons?.(key); }}>Don't prioritize threads like this</button>
                  </span>
                </>
              ) : null}
            </span>
            <button className="primary-btn" onClick={() => void actions.openCard(key)}>{action.label}</button>
          </>
        )}
      </div>
      {expanded ? <CardDetails card={card} /> : null}
      {status ? <div className="attention-status">{status}</div> : null}
    </article>
  );
}

function LowValueEmailCard({ msg, index, cardId }: { msg: CleanupMessage; index: number; cardId: string }) {
  const { state } = useApp();
  const readState = state.cleanupReadState[cardId];
  const isRead = Boolean(readState && readState.readMsgIndices.includes(index));
  const displayDate = msg.date ? formatBeijingTimestamp(Number(msg.date)) : "";
  return (
    <div className={`low-value-email-card ${isRead ? "is-read" : ""}`}>
      {isRead ? <span className="low-value-email-check">✓</span> : null}
      <div className="low-value-email-header">
        <span className="low-value-email-from">{msg.from_addr || "Unknown"}</span>
        {displayDate ? <span className="low-value-email-date">{displayDate}</span> : null}
      </div>
      <div className="low-value-email-subject">{msg.subject || "(no subject)"}</div>
      {msg.snippet ? <div className="low-value-email-snippet">{msg.snippet}</div> : null}
      <div className="low-value-email-meta">
        <span className="low-value-email-type">low priority</span>
        {msg.reason ? <span className="low-value-email-reason">{msg.reason}</span> : null}
      </div>
    </div>
  );
}

function CleanupBundleCard({ card }: { card: FrontendCard }) {
  const { state, actions } = useApp();
  const key = card.uiKey || card.id;
  const expanded = Boolean(state.expandedDetails[key]);
  const readState = state.cleanupReadState[key];
  const isAllRead = readState?.read;
  const messages = Array.isArray(card.bundledMessages) ? card.bundledMessages : [];
  const count = card.bundledCount || messages.length;
  const readCount = readState ? readState.readMsgIndices.length : 0;
  return (
    <article className={`cleanup-bundle-card ${expanded ? "is-expanded" : ""} ${isAllRead ? "is-all-read" : ""}`}>
      <div className="cleanup-bundle-top">
        <button className="cleanup-bundle-header" onClick={() => actions.toggleDetails(key)}>
          <span className="cleanup-bundle-icon">{expanded ? "▾" : "▸"}</span>
          <span className="cleanup-bundle-info">
            <span className="cleanup-bundle-title">{card.title}</span>
            <span className="cleanup-bundle-hint">{card.summary}</span>
          </span>
          <span className="cleanup-bundle-count">{isAllRead ? "✓" : `${readCount}/${count}`}</span>
        </button>
        <button className="soft-btn" disabled={Boolean(isAllRead || state.markingReadIds[key])} onClick={() => void actions.markCleanupAsRead(key)}>
          {state.markingReadIds[key] ? "Marking..." : isAllRead ? "All read" : "Mark all read"}
        </button>
      </div>
      {expanded ? <div className="cleanup-bundle-body">{messages.map((msg, i) => <LowValueEmailCard key={`${key}-${i}`} msg={msg} index={i} cardId={key} />)}</div> : null}
    </article>
  );
}

function CleanupIndividualCard({ msg, index, cardId }: { msg: CleanupMessage; index: number; cardId: string }) {
  const { state } = useApp();
  const readState = state.cleanupReadState[cardId];
  const isRead = Boolean(readState && readState.readMsgIndices.includes(index));
  const displayDate = msg.date ? formatBeijingTimestamp(Number(msg.date)) : "";
  return (
    <article className={`cleanup-single-card ${isRead ? "is-read" : ""}`}>
      <div className="cleanup-single-header">
        <span className="cleanup-single-from">{msg.from_addr || "Unknown"}</span>
        {displayDate ? <span className="cleanup-single-date">{displayDate}</span> : null}
      </div>
      <div className="cleanup-single-subject">{msg.subject || "(no subject)"}</div>
      {msg.snippet ? <div className="cleanup-single-snippet">{msg.snippet}</div> : null}
      <div className="low-value-email-meta">
        <span className="low-value-email-type">low priority</span>
        {msg.reason ? <span className="low-value-email-reason">{msg.reason}</span> : null}
      </div>
    </article>
  );
}

function LowerPrioritySection({ cards }: { cards: FrontendCard[] }) {
  const { state, actions } = useApp();
  if (!cards.length) return null;
  return (
    <section className="lower-priority">
      <button className="lower-priority-toggle" aria-expanded={state.lowerPriorityOpen} onClick={actions.toggleLowerPriority}>
        <span>
          <p className="lower-priority-title">Quiet for now</p>
          <p className="lower-priority-copy">{cards.length} lower-priority item{cards.length === 1 ? "" : "s"} from the same scan.</p>
        </span>
        <span className="attention-pill">{state.lowerPriorityOpen ? "Hide" : "Show"}</span>
      </button>
      {state.lowerPriorityOpen ? <div className="attention-queue">{cards.map((card) => <AttentionCard key={card.uiKey || card.id} card={card} />)}</div> : null}
    </section>
  );
}

function normalizeMailbox(mailbox: string): string {
  return String(mailbox || "").trim().toLowerCase();
}

function cardMailbox(card: FrontendCard): string {
  return normalizeMailbox(card.details?.mailbox || "");
}

function MailboxFilter({ allCards }: { allCards: FrontendCard[] }) {
  const { state, actions } = useApp();
  const [open, setOpen] = useState(false);
  const enabledMailboxes = (state.selectedMailboxes.length ? state.selectedMailboxes : [state.mailbox]).map(normalizeMailbox).filter(Boolean);
  if (enabledMailboxes.length <= 1) return null;
  const enabledSet = new Set(enabledMailboxes);
  const active = (state.briefMailboxFilter.length ? state.briefMailboxFilter : enabledMailboxes).map(normalizeMailbox).filter((mailbox) => enabledSet.has(mailbox));
  const activeMailboxes = active.length ? active : enabledMailboxes;
  const activeSet = new Set(activeMailboxes);
  const allSelected = activeSet.size === enabledMailboxes.length;
  const counts = new Map<string, number>();
  for (const card of visibleCards(allCards)) {
    const mailbox = cardMailbox(card);
    if (enabledSet.has(mailbox)) counts.set(mailbox, (counts.get(mailbox) || 0) + 1);
  }
  const selectedCount = allSelected ? enabledMailboxes.length : activeMailboxes.length;

  const toggleMailbox = (mailbox: string) => {
    if (allSelected) {
      actions.setBriefMailboxFilter(enabledMailboxes.filter((m) => m !== mailbox));
      return;
    }
    if (activeSet.has(mailbox)) {
      if (activeMailboxes.length === 1) return;
      actions.setBriefMailboxFilter(activeMailboxes.filter((item) => item !== mailbox));
      return;
    }
    actions.setBriefMailboxFilter([...activeMailboxes, mailbox]);
  };

  return (
    <div className="mailbox-dropdown">
      <button className="mailbox-dropdown-trigger" onClick={() => setOpen((v: boolean) => !v)}>
        {selectedCount} mailbox{selectedCount !== 1 ? "es" : ""} <span className="mailbox-dropdown-arrow">▾</span>
      </button>
      {open ? (
        <>
          <div className="mailbox-dropdown-backdrop" onClick={() => setOpen(false)} />
          <div className="mailbox-dropdown-panel">
            {enabledMailboxes.map((mailbox) => {
              const checked = allSelected || activeSet.has(mailbox);
              return (
                <label key={mailbox} className="mailbox-dropdown-item">
                  <input type="checkbox" checked={checked} onChange={() => toggleMailbox(mailbox)} />
                  <span className="mailbox-dropdown-name">{mailbox}</span>
                  <span className="mailbox-dropdown-count">{counts.get(mailbox) || 0}</span>
                </label>
              );
            })}
          </div>
        </>
      ) : null}
    </div>
  );
}

const REASONS = [
  { key: "automated", label: "Automated / no-reply emails" },
  { key: "promotional", label: "Promotional or marketing content" },
  { key: "newsletter", label: "Newsletter or digest" },
  { key: "calendar", label: "Calendar / system notification" },
  { key: "not_my_area", label: "Not my area of responsibility" },
  { key: "cc_only", label: "I'm CC'd, not the primary recipient" },
];

function SnoozeReasonsDialog({ cardKey, onConfirm, onClose }: { cardKey: string; onConfirm: (reasons: string[]) => void; onClose: () => void }) {
  const [selected, setSelected] = useState<string[]>(["automated"]);

  const toggle = (key: string) => {
    setSelected((prev) => prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]);
  };

  return (
    <div className="snooze-reasons-overlay" onClick={onClose}>
      <div className="snooze-reasons-dialog" onClick={(e) => e.stopPropagation()}>
        <p className="snooze-reasons-title">Don't prioritize because…</p>
        <div className="snooze-reasons-list">
          {REASONS.map((r) => (
            <label key={r.key} className="snooze-reasons-item">
              <input type="checkbox" checked={selected.includes(r.key)} onChange={() => toggle(r.key)} />
              <span>{r.label}</span>
            </label>
          ))}
        </div>
        <div className="snooze-reasons-actions">
          <button className="soft-btn" onClick={onClose}>Cancel</button>
          <button className="primary-btn" onClick={() => onConfirm(selected)}>Confirm</button>
        </div>
      </div>
    </div>
  );
}

export function BriefView() {
  const { state, actions } = useApp();
  const [confirmClear, setConfirmClear] = useState<string | null>(null);
  if (state.gmailAuthStatus.checked && !state.gmailAuthStatus.authorized) {
    return (
      <div className="first-run-layout">
        <section className="first-run-center" aria-label="Gmail authorization required">
          <div className="auth-orb" aria-hidden="true">A</div>
          <h1 className="first-run-title">Connect your Gmail account</h1>
          <p className="first-run-copy">Authorize Anna to read and manage your inbox.</p>
          <div className="auth-guide-card">
            <div className="auth-guide-steps">
              <div className="auth-step"><span className="auth-step-num">1</span><span>Open <strong>More → Authorizations</strong></span></div>
              <div className="auth-step"><span className="auth-step-num">2</span><span>Select <strong>Google</strong> → <strong>Connect with OAuth</strong></span></div>
              <div className="auth-step"><span className="auth-step-num">3</span><span>Tick Gmail and Calendar permissions</span></div>
              <div className="auth-step"><span className="auth-step-num">4</span><span>Click <strong>Authorize</strong> and return here</span></div>
            </div>
          </div>
          <div className="first-run-actions">
            <button className="primary-btn" onClick={() => void actions.checkAnyGmailAuth()}>Check again</button>
            <button className="soft-btn" onClick={() => actions.openSourcesWithConfig?.()}>Scan setting</button>
          </div>
        </section>
      </div>
    );
  }

  const enabledMailboxes = (state.selectedMailboxes.length ? state.selectedMailboxes : [state.mailbox]).map(normalizeMailbox).filter(Boolean);
  const enabledSet = new Set(enabledMailboxes);
  const cardsInEnabledMailboxes = state.allCards.filter((card) => enabledSet.has(cardMailbox(card)));
  const cards = mainCards(state.cards);
  const lower = lowerCards(state.cards);
  const hasCards = visibleCards(cardsInEnabledMailboxes).length > 0;
  const totalScans = Number(state.scanState?.total_scans || 0);
  const isFresh = !state.loading && !state.isScanning && totalScans === 0 && !hasCards;

  if (isFresh) {
    return (
      <div className="first-run-layout">
        <section className="first-run-center" aria-label="First inbox scan">
          <button className="scan-launch-btn" aria-label="Start first scan" disabled={!state.runtime.connected} onClick={() => void actions.startScan("first")}>
            <span className="scan-launch-core"><span className="scan-orb">A</span></span>
          </button>
          <div>
            <h1 className="first-run-title">Let Anna take a first look.</h1>
            <p className="first-run-copy">She'll find what needs attention, and leave the noise behind.</p>
          </div>
          <div className="first-run-actions">
            <button className="primary-btn" disabled={!state.runtime.connected} onClick={() => void actions.startScan("first")}>Start scan</button>
            <button className="soft-btn" onClick={() => actions.openSourcesWithConfig?.()}>Scan setting</button>
          </div>
          {state.scanError ? <p className="assistant-copy is-error">{state.scanError}</p> : null}
        </section>
      </div>
    );
  }

  const n = state.actionCount || 0;
  const title = state.loading
    ? "Anna is waking up..."
    : n > 0
      ? `I found ${n} thing${n === 1 ? "" : "s"} worth your attention.`
      : totalScans > 0
        ? "No attention cards right now."
        : "Welcome to Anna Inbox.";
  const activeFilter = state.resultFilter;
  const isAll = activeFilter === "all";
  const showCleanup = isAll || activeFilter === "cleanup";
  const totalVisible = visibleCards(state.cards).length;
  const mainDisplayCards = isAll
    ? mainCards(state.cards)
    : activeFilter === "cleanup"
      ? filteredCards(state.cards, activeFilter).filter((card) => card.cardType !== "cleanup_bundle")
      : activeFilter === "review"
        ? filteredCards(state.cards, activeFilter)
        : filteredCards(state.cards, activeFilter).filter(isMainCard);
  const cleanupCards = lowerCards(state.cards).filter((c) => c.cardType === "cleanup_bundle");
  const regularLower = lowerCards(state.cards).filter((c) => c.cardType !== "cleanup_bundle");
  const cleanupMessages = cleanupCards.flatMap((c) =>
    (Array.isArray(c.bundledMessages) ? c.bundledMessages : []).map((msg, i) => ({ msg, index: i, cardId: c.uiKey || c.id }))
  );
  const cleanupCount = cleanupMessages.length;

  return (
    <div className="start-grid">
      <section className="assistant-card">
        <div>
          <h1 className="assistant-says">{state.isScanning && totalVisible > 0 ? "Anna is scanning your inbox." : title}</h1>
          {state.isScanning && totalVisible > 0 ? (
            <div className="scan-progress-bar">
              <div className="scan-progress-track">
                <div className="scan-progress-fill" style={{ width: `${Math.round((state.scanStepIndex / (SCAN_STEPS.length - 1)) * 100)}%` }} />
              </div>
              <p className="assistant-copy" style={{ flexShrink: 0 }}>{scanProgressLabel(state.scanStage, state.scanProgress) || SCAN_STEPS[state.scanStepIndex]?.title || "Scanning..."}</p>
            </div>
          ) : (
            <>
              {state.scanStatus ? <p className="assistant-copy">{state.scanStatus}</p> : null}
              {state.scanError ? <p className="assistant-copy is-error">{state.scanError}</p> : null}
            </>
          )}
        </div>
      </section>
      {totalScans > 0 ? (
        <>
          <div className="result-filter-row">
            <div className="result-filter-left">
              <div className="category-segment" role="tablist" aria-label="Card filters">
                {CATEGORY_TABS.map((tab) => {
                  const count = tab.id === "all" ? (filteredCards(state.cards, "all").length - cleanupCards.length + cleanupCount) : tab.id === "cleanup" ? cleanupCount : filteredCards(state.cards, tab.id).length;
                  return (
                    <button key={tab.id} className={`category-tab ${activeFilter === tab.id ? "is-active" : ""}`} role="tab" aria-selected={activeFilter === tab.id} onClick={() => actions.setResultFilter(tab.id)}>
                      {tab.label}&nbsp;{count}
                    </button>
                  );
                })}
              </div>
              <MailboxFilter allCards={cardsInEnabledMailboxes} />
            </div>
            {(() => {
              const activeCount = activeFilter === "all" ? (filteredCards(state.cards, "all").length - cleanupCards.length + cleanupCount) : activeFilter === "cleanup" ? cleanupCount : filteredCards(state.cards, activeFilter).length;
              return activeCount > 0 ? (
                <button className="category-clear-btn" title={`Clear ${activeFilter}`} onClick={() => setConfirmClear(activeFilter)}>🗑 Clear</button>
              ) : null;
            })()}
          </div>
          {!isAll && CATEGORY_NOTE[activeFilter] ? <p className="category-note">{CATEGORY_NOTE[activeFilter]}</p> : null}
        </>
      ) : null}
      {mainDisplayCards.length ? (
        <section className="noticed-section"><div className="attention-queue">{mainDisplayCards.map((card) => <AttentionCard key={card.uiKey || card.id} card={card} />)}</div></section>
      ) : null}
      {state.snoozeReasonsKey ? (
        <SnoozeReasonsDialog
          cardKey={state.snoozeReasonsKey}
          onConfirm={(reasons: string[]) => void actions.snoozeCard(state.snoozeReasonsKey, "dont-prioritize", reasons)}
          onClose={() => actions.closeSnoozeReasons?.()}
        />
      ) : null}
      {isAll ? <LowerPrioritySection cards={regularLower} /> : null}
      {showCleanup && isAll ? cleanupCards.map((card) => <CleanupBundleCard key={card.uiKey || card.id} card={card} />) : null}
      {showCleanup && !isAll ? (
        <>
          {cleanupCards.map((card) => {
            const key = card.uiKey || card.id;
            const readState = state.cleanupReadState[key];
            const isAllRead = readState?.read;
            return (
              <div className="cleanup-mark-all-row" key={`mark-${key}`}>
                <button className="soft-btn" disabled={Boolean(isAllRead || state.markingReadIds[key])} onClick={() => void actions.markCleanupAsRead(key)}>
                  {state.markingReadIds[key] ? "Marking..." : isAllRead ? "All read" : "Mark all read"}
                </button>
              </div>
            );
          })}
          {cleanupMessages.map(({ msg, index, cardId }, i) => <CleanupIndividualCard key={msg.message_id || `${i}`} msg={msg} index={index} cardId={cardId} />)}
        </>
      ) : null}
      {confirmClear ? (
        <div className="confirm-overlay" onClick={() => setConfirmClear(null)}>
          <div className="confirm-dialog" onClick={(e) => e.stopPropagation()}>
            <p>Delete all <strong>{confirmClear === "all" ? "" : confirmClear}</strong> cards? This cannot be undone.</p>
            <div className="confirm-actions">
              <button className="soft-btn" onClick={() => setConfirmClear(null)}>Cancel</button>
              <button className="primary-btn danger-btn" onClick={() => { void actions.clearCards(confirmClear); setConfirmClear(null); }}>Delete</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
