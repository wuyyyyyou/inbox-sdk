import { useState } from "react";
import { useApp } from "../../app/AppContext";
import { formatBeijingTimestamp } from "../../shared/format";
import { nextCardId } from "../brief/cardHelpers";

function asString(value: unknown): string {
  return typeof value === "string" ? value : String(value ?? "");
}

function asList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => asString(item).trim()).filter(Boolean);
  const text = asString(value).trim();
  return text ? [text] : [];
}

function uniqueLines(lines: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const line of lines) {
    const text = line.trim();
    const key = text.toLowerCase();
    if (text && !seen.has(key)) {
      seen.add(key);
      result.push(text);
    }
  }
  return result;
}

function contactContextLines(contactContext: Record<string, unknown>): string[] {
  const topics = Array.isArray(contactContext.relevant_topics) ? contactContext.relevant_topics : [];
  return uniqueLines(topics.map((topic) => {
    const raw = topic && typeof topic === "object" ? topic as Record<string, unknown> : {};
    return [raw.title, raw.summary, raw.open_loop].map(asString).filter(Boolean).join(" · ");
  }).filter(Boolean));
}

function GapForm({ cardKey, gaps, onSubmit, onSkip }: {
  cardKey: string;
  gaps: import("../../types/mail").ReplyGaps;
  onSubmit: (answers: Record<string, string>) => void;
  onSkip: () => void;
}) {
  const { state, actions } = useApp();
  const saved = state.gapAnswersByCard[cardKey] || {};
  const [answers, setAnswers] = useState<Record<string, string>>(saved);

  const questions = Array.isArray(gaps.questions) ? gaps.questions : [];

  return (
    <div className="gap-form">
      <p className="gap-form-summary">{gaps.summary || "需要你确认几个问题："}</p>
      {questions.map((q) => (
        <div className="gap-form-field" key={q.id}>
          <label className="gap-form-label">{q.question}</label>
          <input
            type="text"
            className="gap-form-input"
            placeholder={q.hint || ""}
            value={answers[q.id] || ""}
            onChange={(e) => {
              const next = { ...answers, [q.id]: e.target.value };
              setAnswers(next);
              actions.setGapAnswers(cardKey, next);
            }}
          />
        </div>
      ))}
      <div className="gap-form-actions">
        <button className="soft-btn" onClick={onSkip}>Skip, generate directly</button>
        <button className="primary-btn" onClick={() => onSubmit(answers)}>Generate draft</button>
      </div>
    </div>
  );
}

export function HandleView() {
  const { state, actions } = useApp();
  const card = state.selectedCard;
  if (!card) return null;
  const key = card.uiKey || card.id;
  const original = card.original || {};
  const detail = state.selectedCardDetail || {};
  const context = detail.thread_context || {};
  const contactContext = detail.contact_context || {};
  const summary = state.threadSummaryById[key];
  const draft = state.draftById[key] || "";
  const replyGaps = card.replyGaps;
  const hasGaps = replyGaps?.needs_user_input && Array.isArray(replyGaps?.questions) && replyGaps.questions.length > 0;
  const showGapForm = hasGaps && !draft;
  const replyMode = state.replyModeById[key] || "reply_to_sender";
  const cc = asString(context.cc || original.cc || "None");
  const contextExpanded = Boolean(state.threadContextExpanded[key]);
  const senderName = asString(context.from || original.from || "").split("<")[0].trim().replace(/"/g, "") || "Unknown";
  const threadSubject = asString(context.subject || original.thread || card.title || "").slice(0, 80);
  const hasDraft = Boolean(draft);
  const draftDisplay = state.generatingDraft && !draft ? "Anna is drafting a reply..." : draft;
  const nid = nextCardId(state);
  const messageCount = Number(context.message_count || 1);
  const isSingleShort = messageCount <= 1 && asString(original.body || "").trim().length <= 280;
  const relatedContext = contactContextLines(contactContext);

  const summaryBlock = () => {
    if (state.summarizingThread) {
      return <section className="review-block is-summary is-loading"><p>Anna is reading the thread and preparing a summary...</p></section>;
    }
    if (!summary) {
      if (isSingleShort) {
        if (!relatedContext.length) return null;
        return (
          <section className="review-block is-summary">
            <div className="review-block-kicker">Related context</div>
            <ul>{relatedContext.map((line, i) => <li key={i}>{line}</li>)}</ul>
          </section>
        );
      }
      return (
        <section className="review-block is-summary">
          <p>Anna can read the full thread and summarize what matters before you draft.</p>
          <div className="proposal-actions"><button className="soft-btn" onClick={() => void actions.summarizeSelectedThread()}>Summarize thread</button></div>
        </section>
      );
    }
    if (summary.core_ask) {
      const legacyBullets = uniqueLines([summary.core_ask, summary.current_progress, ...asList(summary.open_questions)].map(asString));
      return (
        <section className="review-block is-summary">
          <div className="review-block-kicker">Anna noticed</div>
          <ul>{legacyBullets.map((line, i) => <li key={i}>{line}</li>)}</ul>
          {summary.user_action_needed ? <p><strong>Reply focus:</strong> {asString(summary.user_action_needed)}</p> : null}
        </section>
      );
    }
    const shouldShow = summary.should_show !== false;
    const bullets = uniqueLines([asString(summary.headline), ...asList(summary.what_happened)].filter(Boolean));
    const openQuestions = uniqueLines(asList(summary.open_questions));
    const related = uniqueLines([...asList(summary.related_context), ...relatedContext]);
    const replyFocus = asString(summary.reply_focus).trim();
    if (!shouldShow && !related.length) return null;
    return (
      <section className="review-block is-summary">
        <div className="review-block-kicker">{summary.thread_kind === "single_short" ? "Related context" : "Anna noticed"}</div>
        {bullets.length ? <ul>{bullets.map((line, i) => <li key={i}>{line}</li>)}</ul> : null}
        {related.length ? <p><strong>Related context:</strong> {related.join(" ")}</p> : null}
        {openQuestions.length ? <p><strong>Open questions:</strong> {openQuestions.join(" ")}</p> : null}
        {replyFocus ? <p><strong>Reply focus:</strong> {replyFocus}</p> : null}
      </section>
    );
  };

  return (
    <section className="detail-shell">
      <button className="detail-back" onClick={actions.closeDrawers}>← Back to brief</button>
      <article className="detail-card">
        <div className="reply-review-head">
          <div>
            <h2 className="reply-review-title">{card.title || "Email needs review"}</h2>
            <p className="detail-subtitle">{senderName} · {threadSubject}</p>
          </div>
          <span className={`category-tag ${hasDraft ? "is-ready-state" : ""}`}>{hasDraft ? "Reply ready" : "Needs review"}</span>
        </div>
        {summaryBlock()}
        <section className={`review-block is-composer ${state.generatingDraft ? "is-loading" : ""}`}>
          <h3 className="review-block-title">Draft reply</h3>
          {showGapForm ? (
            <GapForm
              cardKey={key}
              gaps={replyGaps}
              onSubmit={(answers) => void actions.generateDraftWithAnswers(answers)}
              onSkip={() => void actions.generateDraft()}
            />
          ) : (
            <>
              <div className="reply-mode-row">
                <div className="reply-mode-control" role="group" aria-label="Reply mode">
                  <button className={`reply-mode-btn ${replyMode === "reply_to_sender" ? "is-active" : ""}`} onClick={() => actions.setReplyMode(key, "reply_to_sender")}>Reply to sender</button>
                  <button className={`reply-mode-btn ${replyMode === "reply_all" ? "is-active" : ""}`} disabled={!cc || cc === "None"} onClick={() => actions.setReplyMode(key, "reply_all")}>Reply all</button>
                </div>
                {cc && cc !== "None" ? null : <span className="reply-mode-note">No CC recipients</span>}
              </div>
              <div className="revise-row">
                <input type="text" placeholder={draft ? "Tell Anna how to revise this draft..." : "Tell Anna how to write the reply (optional)"} value={state.revisionById[key] || ""} disabled={state.generatingDraft} onChange={(e) => actions.setRevision(key, e.target.value)} />
                <button className="soft-btn generate-draft-btn is-glow" disabled={state.generatingDraft} onClick={() => void actions.generateDraft()}>{state.generatingDraft ? "Working..." : draft ? "Ask Anna to revise" : "Generate draft"}</button>
              </div>
              <textarea className={`draft-textarea${state.generatingDraft && !draft ? " is-draft-loading" : ""}`} placeholder="Click 'Generate draft' to have Anna write a reply based on this thread." value={draftDisplay} onChange={(e) => actions.setDraft(key, e.target.value)} />
              {draft ? (
                <div className="preset-row">
                  <button className="preset-chip" disabled={state.generatingDraft} onClick={() => void actions.generateDraft("Make it shorter")}>Shorter</button>
                  <button className="preset-chip" disabled={state.generatingDraft} onClick={() => void actions.generateDraft("Make it warmer")}>Warmer</button>
                  <button className="preset-chip" disabled={state.generatingDraft} onClick={() => void actions.generateDraft("Make it more direct")}>More direct</button>
                </div>
              ) : null}
            </>
          )}
        </section>
        <section className="review-block is-quiet">
          <button className="thread-context-toggle" aria-expanded={contextExpanded} onClick={() => actions.toggleThreadContext(key)}>
            <strong>Thread context · {senderName} · {formatBeijingTimestamp(context.latest_time || original.time)} · {asString(context.message_count || 1)} message{Number(context.message_count || 1) === 1 ? "" : "s"}</strong>
            <span>{contextExpanded ? "Collapse" : "Expand"}</span>
          </button>
          {contextExpanded ? (
            <>
              <div className="thread-context-grid">
                <div className="thread-context-row"><span>From</span><strong>{asString(context.from || original.from || "")}</strong></div>
                <div className="thread-context-row"><span>To</span><strong>{asString(context.to || original.to || "")}</strong></div>
                <div className="thread-context-row"><span>CC</span><strong>{cc || "None"}</strong></div>
                <div className="thread-context-row"><span>Thread</span><strong>{threadSubject}</strong></div>
                <div className="thread-context-row"><span>Latest</span><strong>{formatBeijingTimestamp(context.latest_time || original.time)}</strong></div>
              </div>
              <div className="original-body" style={{ marginTop: 8 }}>{original.body || "Latest email body is not available."}</div>
            </>
          ) : null}
        </section>
        <div className="decision-row drawer-action-row">
          <button className="primary-btn" disabled={!draft.trim() || !!state.pendingAction} onClick={() => void actions.replyNow()}>Reply now</button>
          <button className="soft-btn" disabled={!!state.pendingAction} onClick={() => void actions.recordDecision("no_action_needed")}>No action needed</button>
          <button className="soft-btn" disabled={!!state.pendingAction} onClick={() => void actions.recordDecision("handled_manually")}>Handled manually</button>
          {nid ? <button className="detail-back" style={{ marginLeft: "auto" }} onClick={() => void actions.openCard(nid)}>Next card →</button> : null}
        </div>
      </article>
    </section>
  );
}
