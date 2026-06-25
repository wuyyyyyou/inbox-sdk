import { useEffect, useRef, useState } from "react";
import { CATEGORY_NOTE, CATEGORY_TABS } from "../../app/constants";
import { useApp } from "../../app/AppContext";
import { SCAN_STEPS, scanProgressLabel } from "./runHelpers";
import type { CleanupMessage, FrontendCard, GmailErrorPopup } from "../../types/mail";
import { formatBeijingTimestamp } from "../../shared/format";
import {
  cardCategory,
  cardCategoryLabel,
  filteredCards,
  isMainCard,
  lowerCards,
  mainCards,
  normalizeRecommendation,
  primaryAction,
  visibleCards,
} from "./cardHelpers";

function ScanErrorBlock({ error }: { error: string }) {
  const [expanded, setExpanded] = useState(false);
  const lines = error.split("\n").filter((line) => line.trim());
  if (!lines.length) return null;
  const hasMore = lines.length > 1;
  return (
    <p className="assistant-copy is-error">
      {expanded ? lines.map((line, i) => <span key={i}>{line}{i < lines.length - 1 ? <br /> : null}</span>) : lines[0]}
      {hasMore ? (
        <button className="inline-link" onClick={() => setExpanded((v) => !v)}>
          {" | "}{expanded ? "Show less" : `Show all (${lines.length - 1} more)`}
        </button>
      ) : null}
    </p>
  );
}


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

function attachmentIcon(mimeType: string): string {
  const text = String(mimeType || "").toLowerCase();
  if (text.includes("pdf")) return "PDF";
  if (text.startsWith("image/")) return "IMG";
  if (text.includes("spreadsheet") || text.includes("excel") || text.includes("csv")) return "XLS";
  if (text.includes("word") || text.includes("document")) return "DOC";
  return "FILE";
}

function AttachmentChips({ card }: { card: FrontendCard }) {
  const attachments = Array.isArray(card.attachments) ? card.attachments : [];
  if (!attachments.length) return null;
  const visible = attachments.slice(0, 3);
  const remaining = attachments.length - visible.length;
  return (
    <div className="attention-attachments" aria-label={`${attachments.length} attachment${attachments.length === 1 ? "" : "s"}`}>
      {visible.map((item) => (
        <span className="attachment-chip" key={item.id || item.filename}>
          <span className="attachment-chip-kind">{attachmentIcon(item.mime_type)}</span>
          <span className="attachment-chip-name">{item.filename || "Attachment"}</span>
        </span>
      ))}
      {remaining > 0 ? <span className="attachment-chip attachment-chip-more">+{remaining}</span> : null}
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
  const category = cardCategory(card);
  const categoryLabel = cardCategoryLabel(card);
  const resolutionLabel: Record<string, string> = { replied: "Replied", read: "Read", no_action_needed: "No action", handled_manually: "Handled", dismissed: "Dismissed" };
  const resolvedLabel = card.status === "snoozed" ? "Snoozed" : (resolutionLabel[card.resolution || ""] || "Resolved");
  return (
    <article
      className={`attention-item-card ${expanded ? "is-expanded" : ""} ${isResolved ? "is-resolved" : ""}`}
      data-card-key={key}
      tabIndex={-1}
      aria-label={card.title || "Attention card"}
    >
      <div className="attention-card-head">
        <h2 className="attention-title">
          <span className={`priority-badge priority-${card.priority || "low"}`}>{card.priority || "low"}</span>
          <span className={`attention-category-badge attention-category-${category}`}>{categoryLabel}</span>
          <span>{card.title || "Email thread needs review"}</span>
        </h2>
        {state.selectedMailboxes.length > 1 && mailbox ? (
          <span className="attention-card-mailbox"><span className="mailbox-dot" />{mailbox}</span>
        ) : null}
        <button className="card-details-btn" onClick={() => actions.toggleDetails(key)}>{expanded ? "Hide" : "Details"}</button>
      </div>
      <p className="attention-field">{card.summary || ""}</p>
      <AttachmentChips card={card} />
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
  const { state, actions } = useApp();
  const readState = state.cleanupReadState[cardId];
  const isRead = Boolean(readState && readState.readMsgIndices.includes(index));
  const messageId = msg.message_id || msg.id || "";
  const itemKey = messageId ? `${cardId}:${messageId}` : `${cardId}:${index}`;
  const isMarking = Boolean(state.markingReadIds[itemKey]);
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
        {messageId ? (
          <button className="soft-btn compact cleanup-read-btn" disabled={Boolean(isRead || isMarking)} onClick={() => void actions.markCleanupAsRead(cardId, messageId, index)}>
            {isMarking ? "Marking..." : isRead ? "Read" : "Mark read"}
          </button>
        ) : null}
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
  const sourcesMailboxes = (state.selectedMailboxes.length ? state.selectedMailboxes : [state.mailbox]).map(normalizeMailbox).filter(Boolean);
  const sourcesSet = new Set(sourcesMailboxes);
  const briefSet: Set<string> = (() => {
    if (!state.briefMailboxFilter.length) return sourcesSet;
    const filtered = state.briefMailboxFilter.map(normalizeMailbox).filter((m) => sourcesSet.has(m));
    return filtered.length ? new Set(filtered) : sourcesSet;
  })();
  const fullBundle = Array.isArray(state.cleanupBundle) && state.cleanupBundle.length > 0 ? state.cleanupBundle : null;
  const rawMessages = fullBundle ?? (Array.isArray(card.bundledMessages) ? card.bundledMessages : []);
  const messages = rawMessages.filter((m: CleanupMessage) => {
    const mbox = normalizeMailbox(m.mailbox ?? "");
    return mbox ? briefSet.has(mbox) : true;
  });
  const count = fullBundle ? messages.length : card.bundledCount || messages.length;
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
  const { state, actions } = useApp();
  const readState = state.cleanupReadState[cardId];
  const isRead = Boolean(readState && readState.readMsgIndices.includes(index));
  const messageId = msg.message_id || msg.id || "";
  const itemKey = messageId ? `${cardId}:${messageId}` : `${cardId}:${index}`;
  const isMarking = Boolean(state.markingReadIds[itemKey]);
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
        {messageId ? (
          <button className="soft-btn compact cleanup-read-btn" disabled={Boolean(isRead || isMarking)} onClick={() => void actions.markCleanupAsRead(cardId, messageId, index)}>
            {isMarking ? "Marking..." : isRead ? "Read" : "Mark read"}
          </button>
        ) : null}
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
    if (card.cardType === "cleanup_bundle") continue;
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
  const [authChecking, setAuthChecking] = useState(false);
  const [authError, setAuthError] = useState("");
  const autoRetryRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Auto-retry Gmail auth check every 10s while on the auth guide page
  const isAuthPage = state.gmailAuthStatus.checked && !state.gmailAuthStatus.authorized;
  const checkingRef = useRef(false);
  useEffect(() => {
    if (!isAuthPage) return;
    const doCheck = async () => {
      if (checkingRef.current) return;
      checkingRef.current = true;
      setAuthChecking(true);
      try {
        const r = await actions.checkAnyGmailAuth();
        if (!r.authorized) setAuthError(r.source === "error" ? "No Gmail token found — open More → Authorizations in the Anna app menu to connect a Google account." : `No authorized mailbox detected (source: ${r.source}).`);
        else setAuthError("");
      } catch {
        setAuthError("Auth check failed. Make sure the Anna app has internet access and Gmail permissions.");
      } finally {
        checkingRef.current = false;
        setAuthChecking(false);
      }
    };
    doCheck();
    autoRetryRef.current = setInterval(doCheck, 10000);
    return () => { if (autoRetryRef.current) clearInterval(autoRetryRef.current); };
  }, [isAuthPage]);

  const handleManualCheck = async () => {
    if (checkingRef.current) return;
    checkingRef.current = true;
    setAuthChecking(true);
    setAuthError("");
    try {
      const r = await actions.checkAnyGmailAuth();
      if (!r.authorized) setAuthError("No Gmail token found — open More → Authorizations in the Anna app menu to connect a Google account.");
    } catch {
      setAuthError("Auth check failed. Make sure the Anna app has internet access and Gmail permissions.");
    } finally {
      checkingRef.current = false;
      setAuthChecking(false);
    }
  };

  if (isAuthPage) {
    return (
      <div className="first-run-layout">
        <section className="first-run-center" aria-label="Gmail authorization required">
          <div className={`auth-orb ${authChecking ? "is-pulse" : ""}`} aria-hidden="true">A</div>
          <h1 className="first-run-title">Connect your Gmail account</h1>
          <p className="first-run-copy">Authorize Anna to read and manage your inbox.</p>
          <div className="auth-guide-card">
            <div className="auth-guide-steps">
              <div className="auth-step"><span className="auth-step-num">1</span><span>Open <strong>More → Authorizations</strong></span></div>
              <div className="auth-step"><span className="auth-step-num">2</span><span>Select <strong>Google</strong> → <strong>Connect with OAuth</strong></span></div>
              <div className="auth-step"><span className="auth-step-num">3</span><span>Tick Gmail Read/Modify/Compose/Send</span></div>
              <div className="auth-step"><span className="auth-step-num">4</span><span>Click <strong>Authorize</strong> and return here</span></div>
            </div>
          </div>
          <div className="first-run-actions">
            <button className="primary-btn" disabled={authChecking} onClick={handleManualCheck}>{authChecking ? "Checking..." : "Check again"}</button>
            <button className="soft-btn" onClick={() => actions.openSourcesWithConfig?.()}>Scan setting</button>
          </div>
          {authError ? <p className="assistant-copy is-error" style={{ marginTop: 12, textAlign: "center" }}>{authError}</p> : null}
        </section>
      </div>
    );
  }

  const enabledMailboxes = (state.selectedMailboxes.length ? state.selectedMailboxes : [state.mailbox]).map(normalizeMailbox).filter(Boolean);
  const enabledSet = new Set(enabledMailboxes);
  const briefFilterSet: Set<string> = (() => {
    if (!state.briefMailboxFilter.length) return enabledSet;
    const filtered = state.briefMailboxFilter.map(normalizeMailbox).filter((m) => enabledSet.has(m));
    return filtered.length ? new Set(filtered) : enabledSet;
  })();
  const cardsInEnabledMailboxes = state.allCards.filter((card) => briefFilterSet.has(cardMailbox(card)));
  const cards = mainCards(state.cards);
  const lower = lowerCards(state.cards);
  const hasCards = visibleCards(cardsInEnabledMailboxes).length > 0;
  const totalScans = Number(state.scanState?.total_scans || 0);
  const isFresh = !state.loading && totalScans === 0 && !hasCards;

  if (isFresh) {
    return (
      <div className="first-run-layout">
        <section className="first-run-center" aria-label="First inbox scan">
          <button className="scan-launch-btn" aria-label="Start first scan" disabled={!state.runtime.connected || state.isScanning} onClick={() => void actions.startScan("first")}>
            <span className="scan-launch-core">{state.isScanning ? <span className="scan-orb is-scanning">A</span> : <span className="scan-orb">A</span>}</span>
          </button>
          <div>
            <h1 className="first-run-title">{state.isScanning ? "Anna is scanning your inbox." : "Let Anna take a first look."}</h1>
            <p className="first-run-copy">{state.isScanning ? (state.scanStatus || "Scanning…") : "She'll find what needs attention, and leave the noise behind."}</p>
          </div>
          {state.isScanning ? (
            <div className="scan-progress-bar">
              <div className="scan-progress-track">
                <div className="scan-progress-fill" style={{ width: `${Math.round((state.scanStepIndex / (SCAN_STEPS.length - 1)) * 100)}%` }} />
              </div>
              <p className="assistant-copy">{scanProgressLabel(state.scanStage, state.scanProgress) || SCAN_STEPS[state.scanStepIndex]?.title || "Scanning..."}</p>
            </div>
          ) : (
            <div className="first-run-actions">
              <button className="primary-btn" disabled={!state.runtime.connected} onClick={() => void actions.startScan("first")}>Start scan</button>
              <button className="soft-btn" onClick={() => actions.openSourcesWithConfig?.()}>Scan setting</button>
            </div>
          )}
          <ScanErrorBlock error={state.scanError} />
        </section>
      </div>
    );
  }

  const n = state.actionCount || 0;
  const title = state.loading
    ? "Anna is waking up..."
    : n > 0
      ? `${n} need${n === 1 ? "s" : ""} reply or review.`
      : totalScans > 0
        ? "Nothing needs attention right now."
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
  const fullCleanupBundle = Array.isArray(state.cleanupBundle) && state.cleanupBundle.length > 0 ? state.cleanupBundle : null;
  const cleanupMessages = (fullCleanupBundle
    ? fullCleanupBundle.map((msg, index) => ({ msg, index, cardId: cleanupCards[0]?.uiKey || cleanupCards[0]?.id || "cleanup" }))
    : cleanupCards.flatMap((c) => (Array.isArray(c.bundledMessages) ? c.bundledMessages : []).map((msg, i) => ({ msg, index: i, cardId: c.uiKey || c.id })))
  ).filter(({ msg }) => {
    const mbox = normalizeMailbox(msg.mailbox ?? "");
    return mbox ? briefFilterSet.has(mbox) : true;
  }).map((entry, index) => ({ ...entry, index }));
  const cleanupPreviewCount = cleanupCards.reduce((sum, card) => sum + (card.bundledCount || (Array.isArray(card.bundledMessages) ? card.bundledMessages.length : 0)), 0);
  const cleanupCount = fullCleanupBundle ? cleanupMessages.length : cleanupPreviewCount;

  return (
    <>
    <div className="start-grid">
      <section className="assistant-card">
        <div>
          <h1 className="assistant-says">{state.isScanning ? "Anna is scanning your inbox." : title}</h1>
          {state.isScanning && totalVisible === 0 ? (
            <p className="assistant-copy">{state.scanStatus || "Scanning..."}</p>
          ) : null}
          {!state.isScanning ? (
            <>
              {state.scanStatus ? <p className="assistant-copy">{state.scanStatus}</p> : null}
              <ScanErrorBlock error={state.scanError} />
            </>
          ) : null}
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
              return (
                <button
                  className={`category-clear-btn${activeCount > 0 ? "" : " is-placeholder"}`}
                  title={activeCount > 0 ? `Clear ${activeFilter}` : ""}
                  disabled={activeCount <= 0}
                  aria-hidden={activeCount <= 0}
                  onClick={() => {
                    if (activeCount > 0) setConfirmClear(activeFilter);
                  }}
                >
                  🗑 Clear
                </button>
              );
            })()}
          </div>
          <p className={`category-note${!isAll && CATEGORY_NOTE[activeFilter] ? "" : " is-placeholder"}`}>
            {!isAll && CATEGORY_NOTE[activeFilter] ? CATEGORY_NOTE[activeFilter] : " "}
          </p>
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
      {state.isScanning && totalVisible > 0 ? (
        <div className="scan-bottom-bar">
          <div className="scan-bottom-track">
            <div className="scan-bottom-fill" style={{ width: `${Math.round((state.scanStepIndex / (SCAN_STEPS.length - 1)) * 100)}%` }} />
          </div>
          <span className="scan-bottom-label">{scanProgressLabel(state.scanStage, state.scanProgress) || SCAN_STEPS[state.scanStepIndex]?.title || "Scanning..."}</span>
          {state.scanStatus ? <span className="scan-bottom-meta">{state.scanStatus}</span> : null}
        </div>
      ) : null}
    </div>
    {state.gmailErrorPopup ? (
      <div className="gmail-error-overlay">
        <div className="gmail-error-dialog">
          <h2 className="gmail-error-title">Gmail connection failed</h2>
          <p className="gmail-error-body">{state.gmailErrorPopup.mailbox || "your mailbox"} could not be reached.</p>
          <div className="gmail-error-steps">
            <p>To fix this:</p>
            <ol>
              <li>Open Anna Settings → Authorizations</li>
              <li>Check that Gmail access is granted for <strong>{state.gmailErrorPopup.mailbox}</strong></li>
              <li>If already authorized, tap <strong>"Refresh token"</strong> and re-scan</li>
            </ol>
          </div>
          <button className="primary-btn gmail-error-close" onClick={actions.closeGmailErrorPopup}>Close</button>
        </div>
      </div>
    ) : null}
    </>
  );
}
