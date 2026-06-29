import { useState } from "react";
import { useApp } from "../../app/AppContext";
import type { AskHistoryEntry, CustomPlanQuery, CustomRunResult, CustomRunResultItem, ReplyGaps } from "../../types/mail";
import { formatBeijingTimestamp, normalizeSubject } from "../../shared/format";
import { CUSTOM_PROGRESS_STEPS, customStageCopy } from "../brief/runHelpers";

const ASK_STARTERS = [
  "What needs my reply?",
  "Show deadlines this week",
  "Find unanswered follow-ups",
  "Summarize unread email",
];

function AskSparkleIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M10 2.5c.45 3.7 2.3 5.55 6 6-3.7.45-5.55 2.3-6 6-.45-3.7-2.3-5.55-6-6 3.7-.45 5.55-2.3 6-6Z" />
      <path d="M15.5 2.5c.16 1.32.83 1.99 2.15 2.15-1.32.16-1.99.83-2.15 2.15-.16-1.32-.83-1.99-2.15-2.15 1.32-.16 1.99-.83 2.15-2.15Z" />
    </svg>
  );
}

function AskArrowIcon() {
  return <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M11 6l4 4-4 4" /></svg>;
}

function asQueries(value: unknown): CustomPlanQuery[] {
  return Array.isArray(value) ? value as CustomPlanQuery[] : [];
}

function groupSources(sources: unknown) {
  const groups = new Map<string, { subject: string; count: number; people: Set<string> }>();
  for (const source of Array.isArray(sources) ? sources as Array<Record<string, unknown>> : []) {
    const subject = normalizeSubject(source.subject);
    const key = String(source.thread_id || subject);
    const existing = groups.get(key) || { subject, count: 0, people: new Set<string>() };
    existing.count += 1;
    if (source.from) existing.people.add(String(source.from));
    groups.set(key, existing);
  }
  return Array.from(groups.values()).map((group) => ({ subject: group.subject, count: group.count, people: Array.from(group.people).slice(0, 2).join(", ") }));
}

function AskProgress() {
  const { state } = useApp();
  const run = state.customRunProgress;
  if (!run) return null;
  const progress = run.progress || {};
  const partial = run.partial || {};
  const plan = (partial.plan || {}) as Record<string, unknown>;
  const queries = asQueries(plan.gmail_queries);
  const sources = Array.isArray(partial.sources) ? partial.sources as Array<Record<string, unknown>> : [];
  const activeIndex = Math.max(0, CUSTOM_PROGRESS_STEPS.findIndex((step) => step.key === run.stageKey));
  const scanned = Number(progress.scanned || 0);
  const total = Number(progress.total || 0);
  const current = Number(progress.current || 0);
  const queryTotal = Number(progress.query_total || queries.length || 0);
  return (
    <section className="ask-progress-card">
      <div className="ask-progress-head">
        <div className="ask-orb" aria-hidden="true"><span /></div>
        <div>
          <p className="ask-progress-kicker">Custom scan running</p>
          <h2 className="ask-progress-title">{String(plan.title || "Planning your scan")}</h2>
          <p className="ask-progress-copy">{customStageCopy(run.stageKey, progress)}</p>
        </div>
      </div>
      <div className="ask-progress-steps" aria-label="Custom scan progress">
        {CUSTOM_PROGRESS_STEPS.map((step, index) => (
          <div key={step.key} className={`ask-progress-step ${index < activeIndex ? "is-done" : ""} ${index === activeIndex ? "is-active" : ""}`}>
            <span className="ask-step-dot" /><span>{step.label}</span>
          </div>
        ))}
      </div>
      <div className="ask-progress-metrics">
        <span><strong>{queryTotal || "-"}</strong> queries</span>
        <span><strong>{scanned || sources.length || "-"}</strong> found</span>
        <span><strong>{total ? `${current}/${total}` : "-"}</strong> read</span>
      </div>
      {queries.length ? (
        <div className="ask-plan-preview">
          <div className="ask-plan-head"><span>Plan preview</span><strong>{String(plan.read_depth || "message_detail")}</strong></div>
          <div className="ask-query-list">
            {queries.slice(0, 3).map((query, i) => <div className="ask-query-item" key={i}><code>{query.query || ""}</code>{query.purpose ? <span>{query.purpose}</span> : null}</div>)}
          </div>
        </div>
      ) : <div className="ask-skeleton"><span /><span /><span /></div>}
      {sources.length ? (
        <div className="ask-source-strip">
          {sources.slice(0, 4).map((source, i) => <div className="ask-source-chip" key={i}><strong>{String(source.subject || "Untitled email")}</strong><span>{String(source.from || "")}</span></div>)}
        </div>
      ) : null}
      {run.stageKey === "answering" ? <div className="ask-answer-wait"><span /><span /><span /></div> : null}
    </section>
  );
}

function AskGapForm({ actionKey, gaps, item, mailbox }: {
  actionKey: string;
  gaps: ReplyGaps;
  item: CustomRunResultItem;
  mailbox: string;
}) {
  const { state, actions } = useApp();
  const saved = state.askGapAnswers[actionKey] || {};
  const [answers, setAnswers] = useState<Record<string, string>>(saved);
  const acts = state.askItemActions[actionKey] || {};
  const questions = Array.isArray(gaps.questions) ? gaps.questions : [];

  return (
    <div className="ask-item-actions">
      <div className="gap-form ask-gap-form">
        <p className="gap-form-summary">{gaps.summary || "Need a few details before drafting:"}</p>
        <div className="ask-gap-row">
          <div className="ask-gap-fields">
            {questions.map((q) => (
              <div className="gap-form-field" key={q.id}>
                <label className="gap-form-label">{q.question}{q.required ? " *" : ""}</label>
                <input
                  type="text"
                  className="gap-form-input"
                  placeholder={q.hint || ""}
                  value={answers[q.id] || ""}
                  onChange={(e) => {
                    const next = { ...answers, [q.id]: e.target.value };
                    setAnswers(next);
                    actions.setAskGapAnswers(actionKey, next);
                  }}
                />
              </div>
            ))}
          </div>
          <div className="gap-form-actions ask-gap-actions">
            <button className="primary-btn" disabled={acts.sending} onClick={() => void actions.generateAskDraftWithAnswers(actionKey, item, answers, mailbox)}>
              {acts.sending ? "Generating..." : "Generate draft"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function AskItemActions({ item }: { item: CustomRunResultItem }) {
  const { state, actions } = useApp();
  const mid = item.message_id || "";
  const itemMailbox = item.mailbox || state.selectedMailboxes[0] || state.mailbox;
  const actionKey = `${itemMailbox}::${mid || item.thread_id || ""}`;
  const acts = state.askItemActions[actionKey] || state.askItemActions[mid] || {};

  const generatedDraft = state.askDraftsByKey[actionKey] || "";
  const draft = generatedDraft || item.draft || "";
  const hasDraft = Boolean(draft.trim());
  const editableDraft = state.askEditDraft[actionKey] ?? draft;

  const replyGaps = item.reply_gaps;
  const hasGaps = replyGaps?.needs_user_input && Array.isArray(replyGaps?.questions) && replyGaps.questions.length > 0;
  const showGapForm = hasGaps && !hasDraft;

  if (showGapForm) {
    return <AskGapForm actionKey={actionKey} gaps={replyGaps!} item={item} mailbox={itemMailbox} />;
  }

  const canReply = hasDraft && item.thread_id && item.from;
  const canMark = Boolean(mid);
  const canTrash = Boolean(mid);
  if (!canReply && !canMark && !canTrash) return null;
  if (acts.trashed && !canReply) return <div className="ask-item-actions"><span className="ask-action-done">Trashed</span></div>;
  if (hasDraft) {
    return (
      <div className="ask-draft-editor">
        <textarea className="ask-edit-textarea" rows={4} value={editableDraft} onChange={(e) => actions.updateAskDraft(actionKey, e.target.value)} />
        <div className="ask-item-actions">
          {canReply ? <button className="ask-action-btn ask-action-reply" disabled={acts.replied || acts.sending} onClick={() => void actions.sendAskDraft(actionKey, item.thread_id || "", item.from || "", itemMailbox, editableDraft)}>{acts.sending ? "Sending..." : acts.replied ? "Replied" : "Send"}</button> : null}
          <button className="ask-action-btn ask-action-cancel" onClick={() => void actions.copyDraft(editableDraft)}>Copy draft</button>
          {canMark ? <button className="ask-action-btn ask-action-read" disabled={acts.read} onClick={() => void actions.handleAskMarkRead(actionKey, mid, itemMailbox)}>{acts.read ? "Read" : "Mark read"}</button> : null}
          {canTrash ? <button className="ask-action-btn ask-action-trash" onClick={() => void actions.handleAskTrash(actionKey, mid, itemMailbox)}>Trash</button> : null}
          {acts.trashed ? <span className="ask-action-done">Trashed</span> : null}
        </div>
      </div>
    );
  }
  return (
    <div className="ask-item-actions">
      {canReply ? <button className="ask-action-btn ask-action-reply" disabled={acts.replied || acts.sending} onClick={() => actions.enterAskDraftEdit(actionKey, draft)}>{acts.sending ? "Sending..." : acts.replied ? "Replied" : "Reply"}</button> : null}
      {canMark ? <button className="ask-action-btn ask-action-read" disabled={acts.read || acts.sending} onClick={() => void actions.handleAskMarkRead(actionKey, mid, itemMailbox)}>{acts.read ? "Read" : "Mark read"}</button> : null}
      {canTrash ? <button className="ask-action-btn ask-action-trash" disabled={acts.sending} onClick={() => void actions.handleAskTrash(actionKey, mid, itemMailbox)}>Trash</button> : null}
      {acts.trashed ? <span className="ask-action-done">Trashed</span> : null}
    </div>
  );
}

function CustomTrace({ result }: { result: CustomRunResult }) {
  const { state, actions } = useApp();
  const trace = result.trace || {};
  const plan = (trace.plan as Record<string, unknown>) || {
    title: result.plan_title || "",
    description: result.plan_description || "",
    gmail_queries: result.plan_gmail_queries || [],
    read_depth: result.plan_read_depth || "",
  };
  const queries = asQueries(plan.gmail_queries);
  const sources = Array.isArray(trace.sources) ? trace.sources : [];
  const groups = groupSources(sources);
  const progress = (trace.progress || {}) as Record<string, unknown>;
  const readTotal = Number(progress.total || progress.emails || sources.length || 0);
  const readCurrent = Number(progress.current || readTotal || 0);
  const found = Number(progress.scanned || sources.length || 0);
  const readDepth = String(plan.read_depth || result.plan_read_depth || "message_detail");
  const open = state.customTraceOpen;
  return (
    <section className={`custom-trace-card ${open ? "is-open" : ""}`}>
      <button className="custom-trace-summary" aria-expanded={open} onClick={actions.toggleCustomTrace}>
        <span><strong>How Anna got here</strong><em>{queries.length || "-"} quer{queries.length === 1 ? "y" : "ies"} · {found || "-"} found · {readTotal ? `${readCurrent}/${readTotal}` : "-"} read · {readDepth}</em></span>
        <span className="trace-toggle">{open ? "Hide" : "Expand"}</span>
      </button>
      {open ? (
        <div className="custom-trace-detail">
          <div className="trace-block"><div className="trace-block-head"><span>Plan</span><strong>{readDepth}</strong></div><p>{String(plan.title || "Custom scan")}</p>{plan.description ? <small>{String(plan.description)}</small> : null}</div>
          {queries.length ? <div className="trace-block"><div className="trace-block-head"><span>Searched</span><strong>{queries.length}</strong></div><div className="trace-query-list">{queries.slice(0, 4).map((query, i) => <div className="trace-query" key={i}><span>{query.purpose || "Mailbox search"}</span><code>{query.query || ""}</code></div>)}</div></div> : null}
          {groups.length ? <div className="trace-block"><div className="trace-block-head"><span>Evidence</span><strong>{groups.length} group{groups.length === 1 ? "" : "s"}</strong></div><div className="trace-source-list">{groups.slice(0, 6).map((group, i) => <div className="trace-source" key={i}><strong>{group.subject}</strong><span>{group.count} message{group.count === 1 ? "" : "s"}{group.people ? ` · ${group.people}` : ""}</span></div>)}</div></div> : null}
        </div>
      ) : null}
    </section>
  );
}

function CustomRunResultCard({ result, timestamp }: { result: CustomRunResult; timestamp?: string }) {
  const { state } = useApp();
  const sections = Array.isArray(result.sections) ? result.sections : [];
  return (
    <section className="custom-result-card">
      <div className="custom-result-head">
        <div className="custom-result-head-main">
          <div className="custom-result-eyebrow"><AskSparkleIcon /><span>Latest answer</span>{timestamp ? <time>{formatBeijingTimestamp(timestamp)}</time> : null}</div>
          <p className="custom-result-title">{result.title || result.plan_title || "Scan result"}</p>
          <p className="assistant-copy">{result.summary || ""}</p>
        </div>
        <span className="custom-result-badge">Custom scan</span>
      </div>
      {result.planner_fallback ? <div className="ask-plan-fallback">Anna could not generate a smart scan plan this time, so a rule-based fallback was used. The answer may be less precise.</div> : null}
      {sections.map((sec, i) => (
        <div className="point-box point-box-sm" key={i}>
          <span className="box-label">{sec.heading || ""}</span>
          {sec.body ? <p>{sec.body}</p> : null}
          {sec.items?.length ? (
            <ul className="simple-list simple-list-sm">
              {sec.items.map((it, j) => {
                  return (
                <li key={j}>
                  {it.mailbox && state.selectedMailboxes.length > 1 ? <span className="ask-mailbox-chip">{it.mailbox}</span> : null}
                  <strong>{it.subject || ""}</strong>
                  {it.context ? <><br /><span className="text-muted-inline">{it.context}</span></> : null}
                  {it.suggestion ? <><br /><span className="text-accent-inline">→ {it.suggestion}</span></> : null}
                  <AskItemActions item={it} />
                </li>
                  );
              })}
            </ul>
          ) : null}
        </div>
      ))}
      <CustomTrace result={result} />
    </section>
  );
}

function runMeta(result: CustomRunResult) {
  const trace = result.trace || {};
  const plan = (trace.plan as Record<string, unknown>) || {
    gmail_queries: result.plan_gmail_queries || [],
    read_depth: result.plan_read_depth || "",
  };
  const queries = asQueries(plan.gmail_queries);
  const sources = Array.isArray(trace.sources) ? trace.sources : [];
  const progress = (trace.progress || {}) as Record<string, unknown>;
  const found = Number(progress.scanned || sources.length || 0);
  const readTotal = Number(progress.total || progress.emails || sources.length || 0);
  const readCurrent = Number(progress.current || readTotal || 0);
  const readDepth = String(plan.read_depth || result.plan_read_depth || "message_detail");
  return { queries, found, readCurrent, readTotal, readDepth };
}

function CustomRunPreview({ result }: { result: CustomRunResult }) {
  const sections = Array.isArray(result.sections) ? result.sections : [];
  const previewSections = sections.slice(0, 2);
  return (
    <div className="ask-history-preview">
      {result.summary ? <p className="ask-history-preview-summary">{result.summary}</p> : null}
      {previewSections.length ? (
        <div className="ask-history-preview-grid">
          {previewSections.map((sec, i) => {
            const items = Array.isArray(sec.items) ? sec.items.slice(0, 2) : [];
            return (
              <div className="ask-history-preview-block" key={i}>
                <strong>{sec.heading || "Result"}</strong>
                {sec.body ? <p>{sec.body}</p> : null}
                {items.length ? (
                  <ul>
                    {items.map((item, j) => (
                      <li key={j}>
                        <span>{item.subject || "Email"}</span>
                        {item.context ? <em>{item.context}</em> : null}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
      <CustomTrace result={result} />
    </div>
  );
}

function AskHistoryEntryRow({ entry, index }: { entry: AskHistoryEntry; index: number }) {
  const { state, actions } = useApp();
  const expanded = state.askHistoryExpanded[index] || false;
  const meta = runMeta(entry.result);
  const title = entry.result.plan_title || entry.result.title || entry.query || "Custom scan";
  const summary = entry.result.summary || entry.result.plan_description || "";
  const readLabel = meta.readTotal ? `${meta.readCurrent}/${meta.readTotal} read` : "read depth";
  return (
    <div className={`ask-history-entry ${expanded ? "is-expanded" : ""}`}>
      <button className="ask-history-row" onClick={() => actions.toggleAskHistory(index)} aria-expanded={expanded}>
        <span className="ask-history-index">{String(index + 1).padStart(2, "0")}</span>
        <span className="ask-history-main">
          <strong>{title}</strong>
          <span className="ask-history-query">{entry.query}</span>
          {summary ? <span className="ask-history-summary">{summary}</span> : null}
          <span className="ask-history-meta">
            {meta.queries.length || "-"} queries · {meta.found || "-"} found · {readLabel} · {meta.readDepth}
          </span>
        </span>
        <span className="ask-history-side">
          <time>{formatBeijingTimestamp(entry.timestamp)}</time>
          <span className="ask-history-toggle">{expanded ? "Hide" : "Open"}</span>
        </span>
      </button>
      {expanded ? <CustomRunPreview result={entry.result} /> : null}
    </div>
  );
}

export function AskView() {
  const { state, actions } = useApp();
  const [libraryTab, setLibraryTab] = useState<"history" | "plans">(state.askHistory.length > 1 ? "history" : "plans");
  const latest = state.askHistory.length > 0 ? state.askHistory[0] : null;
  const older = state.askHistory.length > 1 ? state.askHistory.slice(1) : [];
  const isRunning = state.isCustomScanning || !!(state.customRunProgress && state.customRunProgress.status !== "failed");
  const mailboxLabel = state.selectedMailboxes.length > 1 ? `${state.selectedMailboxes.length} selected mailboxes` : state.selectedMailboxes[0] || state.mailbox;
  const hasLibrary = older.length > 0 || state.customPlans.length > 0;
  const activeLibraryTab = libraryTab === "history" && !older.length && state.customPlans.length
    ? "plans"
    : libraryTab === "plans" && !state.customPlans.length && older.length
      ? "history"
      : libraryTab;

  return (
    <div className="ask-layout">
      <section className="ask-hero">
        <div className="ask-hero-eyebrow"><AskSparkleIcon /><span>Ask your inbox</span>{mailboxLabel ? <span className="ask-scope-chip">{mailboxLabel}</span> : null}</div>
        <h1>What can Anna find for you?</h1>
        <p>Ask about decisions, follow-ups, deadlines, or anything buried in your email.</p>
      </section>
      <section className="ask-composer" aria-label="Custom scan prompt">
        <textarea
          className="ask-composer-input"
          id="customScanInput"
          placeholder="e.g. Find all unread emails and check which need a reply..."
          rows={1}
          value={state.customScanInput}
          onChange={(e) => actions.setInput("customScanInput", e.target.value)}
          onInput={(e) => { const t = e.currentTarget; t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 160) + "px"; }}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !isRunning) {
              e.preventDefault();
              void actions.startCustomScan();
            }
          }}
        />
        <div className="ask-composer-footer">
          <div className="ask-composer-context">
            <span className={`ask-context-dot ${isRunning ? "is-running" : ""}`} />
            {isRunning ? <span>Anna is scanning</span> : mailboxLabel ? <><span>{mailboxLabel}</span><span className="ask-context-separator" /></> : null}
            <span>Daily brief unchanged</span>
          </div>
          <div className="ask-composer-actions">
            <kbd>{navigator.platform?.toLowerCase().includes("mac") ? "⌘" : "Ctrl"} ↵</kbd>
            <button className="ask-run-btn" disabled={isRunning} onClick={() => void actions.startCustomScan()} aria-label="Ask Anna">
              <span>{isRunning ? "Working" : "Ask Anna"}</span><AskArrowIcon />
            </button>
          </div>
        </div>
      </section>
      {!latest && !isRunning ? (
        <div className="ask-starters" aria-label="Suggested questions">
          <span>Try asking</span>
          <div className="ask-starter-list">
            {ASK_STARTERS.map((starter) => (
              <button key={starter} onClick={() => actions.setInput("customScanInput", starter)}>{starter}</button>
            ))}
          </div>
        </div>
      ) : null}
      {isRunning && state.customRunProgress ? <AskProgress /> : null}
      {state.scanError && !isRunning ? <section className="custom-error-card">{state.scanError}</section> : null}

      {latest ? <CustomRunResultCard result={latest.result} timestamp={latest.timestamp} /> : null}

      {hasLibrary ? (
        <section className="ask-library-section">
          <div className="ask-section-head">
            <div>
              <h2>Library</h2>
              <p>Return to an answer or run a saved scan again.</p>
            </div>
            <div className="ask-library-tabs" role="tablist" aria-label="Ask library">
              <button className={activeLibraryTab === "history" ? "is-active" : ""} role="tab" aria-selected={activeLibraryTab === "history"} disabled={!older.length} onClick={() => setLibraryTab("history")}>Past runs <span>{older.length}</span></button>
              <button className={activeLibraryTab === "plans" ? "is-active" : ""} role="tab" aria-selected={activeLibraryTab === "plans"} disabled={!state.customPlans.length} onClick={() => setLibraryTab("plans")}>Saved scans <span>{state.customPlans.length}</span></button>
            </div>
          </div>
          {activeLibraryTab === "history" ? (
            <div className="ask-library-panel" role="tabpanel">
              <div className="ask-library-panel-head"><span>Recent custom scans</span><button className="history-clear-btn" title="Clear history" onClick={() => void actions.clearHistory()}>Clear history</button></div>
              {older.map((entry, idx) => <AskHistoryEntryRow key={idx} entry={entry} index={idx + 1} />)}
            </div>
          ) : (
            <div className="ask-library-panel" role="tabpanel">
              <div className="ask-library-panel-head"><span>Reusable scans for the selected mailboxes</span></div>
              <div className="history-list">
                {state.customPlans.map((plan) => {
                  const timestampLabel = plan.last_used_at ? `Last run ${formatBeijingTimestamp(plan.last_used_at)}` : `Created ${formatBeijingTimestamp(plan.created_at || "")}`;
                  return (
                    <div className="custom-plan-row" key={plan.plan_id}>
                      <div className="history-row">
                        <div className="custom-plan-head">
                          <strong>{plan.title || plan.user_request?.slice(0, 60) || "Custom scan"}</strong>
                          <button className="custom-plan-delete" aria-label="Delete plan" onClick={() => void actions.deleteCustomPlan(plan.plan_id)}>Delete</button>
                        </div>
                        <span className="custom-plan-meta">{plan.user_request?.slice(0, 100) || ""}{plan.use_count ? ` · Used ${plan.use_count} time${plan.use_count === 1 ? "" : "s"}` : ""}{timestampLabel ? ` · ${timestampLabel}` : ""}</span>
                        {plan.last_result_summary ? <span className="custom-plan-result">{plan.last_result_summary}</span> : null}
                        <span className="custom-plan-actions">
                          <button className="custom-plan-run" disabled={state.isCustomScanning} onClick={() => void actions.reRunCustomPlan(plan.plan_id)}>Run scan <AskArrowIcon /></button>
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </section>
      ) : null}
    </div>
  );
}
