import { Fragment, useLayoutEffect, useRef, useState, type MouseEvent } from "react";
import DOMPurify from "dompurify";
import { useApp } from "../../app/AppContext";
import { formatBeijingTimestamp } from "../../shared/format";
import {
  DEFAULT_DRAFT_PREFERENCES,
  DRAFT_LENGTH_OPTIONS,
  DRAFT_MOOD_OPTIONS,
  DRAFT_REPLY_GOAL_OPTIONS,
  DRAFT_TONE_OPTIONS,
  DRAFT_WRITING_STYLE_OPTIONS,
  type DraftPreferenceField,
  type ThreadContextMessage,
} from "../../types/mail";
import { DRAFT_PREFERENCE_LABELS, resolveDraftPreferences } from "./draftPreferences";
import { cardCategory, cardCategoryLabel, nextCardId } from "../brief/cardHelpers";
import { uniqueLines } from "./summaryContent";

function asString(value: unknown): string {
  return typeof value === "string" ? value : String(value ?? "");
}

function asList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => asString(item).trim()).filter(Boolean);
  const text = asString(value).trim();
  return text ? [text] : [];
}

function formatBytes(value: unknown): string {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function safeExternalUrl(url: string): string {
  try {
    const parsed = new URL(url);
    if (!["http:", "https:", "mailto:"].includes(parsed.protocol)) return "";
    return parsed.toString();
  } catch {
    return "";
  }
}

function openExternalUrl(url: string) {
  const safe = safeExternalUrl(url);
  if (!safe) return;
  const opened = window.open(safe, "_blank", "noopener,noreferrer");
  if (opened) return;
  const link = document.createElement("a");
  link.href = safe;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function emailAddress(value: unknown): string {
  const text = asString(value).trim().toLowerCase();
  const angle = text.match(/<([^>]+)>/);
  if (angle?.[1]) return angle[1].trim();
  const email = text.match(/[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}/i);
  return email?.[0]?.toLowerCase() || text;
}

function displayName(value: unknown): string {
  const text = asString(value).trim();
  const withoutAngle = text.replace(/<[^>]+>/g, "").replace(/"/g, "").trim();
  if (withoutAngle) return withoutAngle;
  return emailAddress(text) || "Unknown";
}

function messageDirection(message: ThreadContextMessage, ownerEmail: string): "inbound" | "outbound" {
  const owner = emailAddress(ownerEmail);
  return owner && emailAddress(message.from).includes(owner) ? "outbound" : "inbound";
}

function threadBodyBlocks(value: unknown): string[] {
  const text = asString(value).replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  if (!text) return [];
  const lines = text.split("\n");
  const blocks: string[] = [];
  let current: string[] = [];

  const flush = () => {
    const block = current.join(" ").replace(/\s+/g, " ").trim();
    if (block) blocks.push(block);
    current = [];
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      flush();
      continue;
    }
    if (/^([-*]|\d+[.)])\s+/.test(trimmed) && current.length) flush();
    current.push(trimmed);
  }
  flush();
  return blocks;
}

function handleOriginalBodyClick(event: MouseEvent<HTMLDivElement>) {
  const target = event.target instanceof Element ? event.target.closest("a[href]") : null;
  if (!target) return;
  event.preventDefault();
  openExternalUrl((target as HTMLAnchorElement).href);
}

const DRAFT_CONTROL_CONFIG: Array<{ field: DraftPreferenceField; options: string[] }> = [
  { field: "length", options: DRAFT_LENGTH_OPTIONS },
  { field: "writingStyle", options: DRAFT_WRITING_STYLE_OPTIONS },
  { field: "tone", options: DRAFT_TONE_OPTIONS },
  { field: "mood", options: DRAFT_MOOD_OPTIONS },
];

function GapForm({ cardKey, gaps, onSubmit, onSkip }: {
  cardKey: string;
  gaps: import("../../types/mail").ReplyGaps;
  onSubmit: (answers: Record<string, string>) => void;
  onSkip: () => void;
}) {
  const { state, actions } = useApp();
  const saved = state.gapAnswersByCard[cardKey] || {};
  const [answers, setAnswers] = useState<Record<string, string>>(saved);
  const isGenerating = state.generatingDraft;

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
            disabled={isGenerating}
            onChange={(e) => {
              const next = { ...answers, [q.id]: e.target.value };
              setAnswers(next);
              actions.setGapAnswers(cardKey, next);
            }}
          />
        </div>
      ))}
      <div className="gap-form-actions">
        <button className="soft-btn" onClick={isGenerating ? actions.stopDraftGeneration : onSkip}>
          {isGenerating ? "Stop generate" : "Skip, generate directly"}
        </button>
        <button className="primary-btn" disabled={isGenerating} onClick={() => onSubmit(answers)}>
          {isGenerating ? "Generating..." : "Generate draft"}
        </button>
      </div>
    </div>
  );
}

export function HandleView() {
  const { state, actions } = useApp();
  const card = state.selectedCard;
  if (!card) return null;
  const draftTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const key = card.uiKey || card.id;
  const original = card.original || {};
  const detail = state.selectedCardDetail || {};
  const context = detail.thread_context || {};
  const latestBody = detail.latest_body || "";
  const latestBodyHtml = detail.latest_body_html || "";
  const attachments = Array.isArray(detail.attachments) ? detail.attachments : (Array.isArray(card.attachments) ? card.attachments : []);
  const bodyLoaded = Boolean(detail.body_loaded);
  const threadMessages = Array.isArray(context.messages) ? context.messages : [];
  const ownerEmail = asString(card.details?.mailbox || state.mailbox);
  const summary = state.threadSummaryById[key];
  const draft = state.draftById[key] || "";
  const draftPreferences = resolveDraftPreferences(state.draftPreferencesById[key] || DEFAULT_DRAFT_PREFERENCES);
  const replyIntent = state.replyIntentById[key] || {};
  const replyGoal = replyIntent.goal || "";
  const replyIntentText = replyIntent.userTake || "";
  const replyGaps = card.replyGaps;
  const hasGaps = replyGaps?.needs_user_input && Array.isArray(replyGaps?.questions) && replyGaps.questions.length > 0;
  const showGapForm = hasGaps && !draft;
  const showDecisionBlock = !draft && !showGapForm;
  const replyMode = state.replyModeById[key] || "reply_to_sender";
  const cc = asString(context.cc || original.cc || "None");
  const contextExpanded = Boolean(state.threadContextExpanded[key]);
  const bodyExpanded = Boolean(state.threadContextExpanded[`${key}_body`]);
  const bodyVisible = bodyLoaded && bodyExpanded;
  const senderEmail = emailAddress(context.from || original.from || "");
  const recipientValue = context.to || original.to || "";
  const toEmail = emailAddress(recipientValue) || asString(recipientValue);
  const threadSubject = asString(context.subject || original.thread || card.title || "").slice(0, 80);
  const latestTime = context.latest_time || original.time || "";
  const messageCount = Math.max(1, Number(context.message_count || threadMessages.length || 1));
  const hasDraft = Boolean(draft);
  const category = cardCategory(card);
  const isReviewCard = category === "review";
  const categoryLabel = hasDraft ? "Reply ready" : cardCategoryLabel(card);
  const draftDisplay = state.generatingDraft && !draft ? "Anna is drafting a reply..." : draft;
  const nid = nextCardId(state);
  const BODY_PREVIEW = 500;
  const loadingBody = state.pendingAction === `body:${key}`;
  const loadingThreadContext = state.pendingAction === `thread:${key}`;
  const bodyTruncated = latestBodyHtml
    ? latestBodyHtml.length > BODY_PREVIEW
    : latestBody.length > BODY_PREVIEW;

  useLayoutEffect(() => {
    const textarea = draftTextareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${textarea.scrollHeight}px`;
  }, [draftDisplay, key]);

  const attachmentsBlock = () => {
    if (!attachments.length || !bodyVisible) return null;
    return (
      <section className="review-block attachment-block">
        <h3 className="review-block-title">Attachments</h3>
        <div className="attachment-list">
          {attachments.map((item) => {
            const downloadState = state.attachmentDownloads[`${key}::${item.id}`];
            const meta = [item.mime_type, formatBytes(item.size)].filter(Boolean).join(" · ");
            return (
              <div className="attachment-row" key={item.id}>
                <div className="attachment-main">
                  <strong>{item.filename || "Attachment"}</strong>
                  {meta ? <span>{meta}</span> : null}
                </div>
                <button className="soft-btn compact" disabled={!item.downloadable || downloadState === "preparing"} onClick={() => void actions.downloadAttachment(item.id)}>
                  {downloadState === "preparing" ? "Preparing..." : "Download"}
                </button>
              </div>
            );
          })}
        </div>
      </section>
    );
  };

  const threadSummaryBlock = () => {
    if (state.summarizingThread) {
      return (
        <section className="review-block is-summary is-loading">
          <p>Anna is reviewing the previous messages so you can quickly remember the context.</p>
        </section>
      );
    }
    if (!summary) {
      return (
        <section className="review-block is-summary">
          <p>Anna can read the thread and pull forward the context that matters before you draft.</p>
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
    const bullets = uniqueLines([asString(summary.headline), ...asList(summary.what_happened)].filter(Boolean));
    const openQuestions = uniqueLines(asList(summary.open_questions));
    const replyFocus = asString(summary.reply_focus).trim();
    const hasSummaryContent = bullets.length || openQuestions.length || replyFocus;
    return (
      <section className="review-block is-summary">
        <div className="review-block-kicker">Anna noticed</div>
        {bullets.length ? <ul>{bullets.map((line, i) => <li key={i}>{line}</li>)}</ul> : null}
        {openQuestions.length ? <p><strong>Open questions:</strong> {openQuestions.join(" ")}</p> : null}
        {replyFocus ? <p><strong>Reply focus:</strong> {replyFocus}</p> : null}
        {!hasSummaryContent ? <p>Anna did not find additional thread details to summarize.</p> : null}
      </section>
    );
  };

  const threadContextMessagesBlock = () => {
    if (!contextExpanded) return null;
    if (!threadMessages.length) {
      return <p className="thread-context-empty">No messages available in this thread.</p>;
    }
    const totalMessages = Number(context.message_count || threadMessages.length);
    const hiddenMessageCount = Math.max(0, totalMessages - threadMessages.length);
    const showHiddenLoader = loadingThreadContext && hiddenMessageCount > 0;
    const messagesToRender = hiddenMessageCount > 0 && threadMessages.length > 1
      ? [threadMessages[0], threadMessages[threadMessages.length - 1]]
      : threadMessages;

    const messageCard = (message: ThreadContextMessage, index: number) => {
      const direction = messageDirection(message, ownerEmail);
      const bodyBlocks = threadBodyBlocks(message.body);
      const messageKey = `${key}_message_${message.message_id || message.date || index}`;
      const expanded = Boolean(state.threadContextExpanded[messageKey]);
      const preview = bodyBlocks[0] || "No message body available.";
      const toggle = () => actions.toggleThreadContext(messageKey);
      return (
        <article
          className={`thread-context-message is-${direction} ${expanded ? "is-open" : "is-collapsed"}`}
          key={message.message_id || `${message.date || ""}-${index}`}
          role="button"
          tabIndex={0}
          aria-expanded={expanded}
          onClick={toggle}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              toggle();
            }
          }}
        >
          <span className="thread-context-message-meta">
            {direction} at {formatBeijingTimestamp(message.date || "")}
          </span>
          <div className="thread-context-message-head">
            <strong>{asString(message.from || "Unknown sender")}</strong>
            {message.to ? <span>to {asString(message.to)}</span> : null}
          </div>
          {message.subject && message.subject !== threadSubject ? <div className="thread-context-message-subject">{asString(message.subject)}</div> : null}
          {expanded ? (
            bodyBlocks.length ? (
              <div className="thread-context-message-body">
                {bodyBlocks.map((block, blockIndex) => <p key={blockIndex}>{block}</p>)}
              </div>
            ) : (
              <p className="thread-context-message-empty">No message body available.</p>
            )
          ) : (
            <p className="thread-context-message-preview">{preview}</p>
          )}
        </article>
      );
    };

    return (
      <div className="thread-context-message-list">
        {messagesToRender.map((message, index) => (
          <Fragment key={message.message_id || `${message.date || ""}-${index}`}>
            {index === 1 && hiddenMessageCount > 0 ? (
              <button
                className={`thread-context-gap ${showHiddenLoader ? "is-loading" : ""}`}
                disabled={loadingThreadContext}
                aria-label={`Load ${hiddenMessageCount} hidden messages`}
                onClick={() => void actions.loadMoreThreadContext()}
              >
                <span>{showHiddenLoader ? "" : hiddenMessageCount}</span>
                {showHiddenLoader ? <em>Loading hidden messages...</em> : null}
              </button>
            ) : null}
            {messageCard(message, index === 0 ? 0 : threadMessages.length - 1)}
          </Fragment>
        ))}
        {showHiddenLoader ? (
          <div className="thread-context-loading" aria-live="polite">
            <span />
            <span />
            <span />
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <section className="detail-shell">
      <button className="detail-back" onClick={actions.closeCardDetail}>← Back to brief</button>
      <article className="detail-card">
        <div className="reply-review-head">
          <div className="detail-head-main">
            <h2 className="reply-review-title">{threadSubject || card.title || "Email needs review"}</h2>
            {card.title && threadSubject && card.title !== threadSubject ? (
              <p className="detail-subtitle">{card.title}</p>
            ) : null}
            <div className="detail-mail-header" aria-label="Email metadata">
              <div className="detail-mail-route">
                <span className="detail-mail-from">{displayName(context.from || original.from || "")}</span>
                {senderEmail ? <span className="detail-mail-address">&lt;{senderEmail}&gt;</span> : null}
                <span className="detail-mail-sep">to</span>
                <span className="detail-mail-to">{toEmail || ownerEmail || "Unknown recipient"}</span>
              </div>
              {latestTime ? <span className="detail-mail-time">{formatBeijingTimestamp(latestTime)}</span> : null}
            </div>
          </div>
          <span className={`category-tag ${hasDraft ? "is-ready-state" : ""}`}>{categoryLabel}</span>
        </div>
        <div className="detail-workbench">
          <div className="detail-context-column">
            {threadSummaryBlock()}
            <section className="review-block is-quiet is-thread-context">
              <button className="thread-context-toggle" aria-expanded={contextExpanded} onClick={() => actions.toggleThreadContext(key)}>
                <strong>Thread context · {messageCount} message{messageCount === 1 ? "" : "s"}</strong>
                <span>{contextExpanded ? "Collapse" : "Expand"}</span>
              </button>
              {contextExpanded ? (
                <>
                  <div className="thread-context-grid">
                    <div className="thread-context-row"><span>From</span><strong>{asString(context.from || original.from || "")}</strong></div>
                    <div className="thread-context-row"><span>To</span><strong>{asString(context.to || original.to || "")}</strong></div>
                    <div className="thread-context-row"><span>CC</span><strong>{cc || "None"}</strong></div>
                    <div className="thread-context-row"><span>Thread</span><strong>{threadSubject}</strong></div>
                    <div className="thread-context-row"><span>Latest</span><strong>{formatBeijingTimestamp(latestTime)}</strong></div>
                  </div>
                  {threadContextMessagesBlock()}
                </>
              ) : null}
            </section>
            {!bodyVisible ? (
              <section className={`review-block is-quiet is-disclosure${loadingBody ? " is-loading" : ""}`}>
                <div>
                  <p>Original email body is hidden until you choose to view it.</p>
                  {attachments.length ? (
                    <p className="attachment-download-hint">
                      This email has {attachments.length} attachment{attachments.length === 1 ? "" : "s"}. Show full email to download {attachments.length === 1 ? "it" : "them"}.
                    </p>
                  ) : null}
                </div>
                <button className="soft-btn compact" disabled={loadingBody} onClick={() => void actions.loadSelectedEmailBody()}>
                  {loadingBody ? "Loading email..." : "Show full email"}
                </button>
              </section>
            ) : latestBodyHtml ? (
              <section className="review-block is-original-body">
                <div
                  className={`original-body original-body-html${bodyTruncated && !bodyExpanded ? " is-clamped" : ""}`}
                  style={{ wordBreak: "break-all", overflowWrap: "break-word" }}
                  onClick={handleOriginalBodyClick}
                  dangerouslySetInnerHTML={{
                    __html: DOMPurify.sanitize(latestBodyHtml, {
                      ALLOWED_TAGS: [
                        "a", "abbr", "b", "blockquote", "br", "caption", "center",
                        "cite", "code", "col", "colgroup", "dd", "del", "details",
                        "dfn", "div", "dl", "dt", "em", "figcaption", "figure",
                        "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i",
                        "img", "ins", "kbd", "li", "map", "area", "mark", "ol",
                        "p", "pre", "q", "s", "samp", "small", "span", "strike",
                        "strong", "sub", "summary", "sup", "table", "tbody", "td",
                        "tfoot", "th", "thead", "time", "tr", "u", "ul", "var",
                      ],
                      ALLOWED_ATTR: [
                        "alt", "align", "bgcolor", "border", "cellpadding",
                        "cellspacing", "class", "color", "colspan", "face",
                        "height", "href", "hspace", "id", "loading", "name",
                        "nowrap", "referrerpolicy", "rel", "rowspan", "size",
                        "src", "style", "target", "title", "valign", "vspace",
                        "width",
                      ],
                      ALLOW_DATA_ATTR: false,
                      ALLOWED_URI_REGEXP: /^(?:(?:https?|ftp|mailto|data|cid):|[^/]+\/[^/]+)/i,
                    }),
                  }}
                />
                <button className="soft-btn compact" style={{ marginTop: 6 }} onClick={() => actions.toggleThreadContext(`${key}_body`)}>
                  Show less
                </button>
              </section>
            ) : latestBody ? (
              <section className="review-block is-original-body">
                <div className="original-body" style={{ whiteSpace: "pre-wrap", wordBreak: "break-all", overflowWrap: "break-word" }}>
                  {bodyTruncated && !bodyExpanded ? latestBody.slice(0, BODY_PREVIEW) + "…" : latestBody}
                </div>
                <button className="soft-btn compact" style={{ marginTop: 6 }} onClick={() => actions.toggleThreadContext(`${key}_body`)}>
                  Show less
                </button>
              </section>
            ) : (
              <section className="review-block is-quiet is-disclosure">
                <p>Full email body is not available for this card.</p>
              </section>
            )}
            {attachmentsBlock()}
          </div>
          <aside className="detail-action-column" aria-label="Reply composer">
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
                  <div className="draft-controls-grid">
                    {DRAFT_CONTROL_CONFIG.map(({ field, options }) => (
                      <label className="draft-control" key={field}>
                        <span className="draft-control-label">{DRAFT_PREFERENCE_LABELS[field]}</span>
                        <select
                          className="draft-control-select"
                          value={draftPreferences[field]}
                          disabled={state.generatingDraft}
                          onChange={(e) => actions.setDraftPreference(key, field, e.target.value)}
                        >
                          {options.map((option) => <option key={option} value={option}>{option}</option>)}
                        </select>
                      </label>
                    ))}
                  </div>
                  {showDecisionBlock ? (
                    <div className="reply-intent-block">
                      <div className="reply-intent-head">
                        <span className="reply-intent-kicker">Decision needed</span>
                        <p className="reply-intent-title">Anna needs your decision before drafting.</p>
                      </div>
                      <div className="reply-goal-row" role="group" aria-label="Reply goal">
                        {DRAFT_REPLY_GOAL_OPTIONS.map((option) => {
                          const active = replyGoal === option;
                          return (
                            <button
                              key={option}
                              className={`reply-goal-btn ${active ? "is-active" : ""}`}
                              aria-pressed={active}
                              disabled={state.generatingDraft}
                              onClick={() => actions.setReplyIntentGoal(key, active ? "" : option)}
                            >
                              {option}
                            </button>
                          );
                        })}
                      </div>
                      <label className="draft-control">
                        <span className="draft-control-label">Your take</span>
                        <input
                          type="text"
                          className="reply-intent-input"
                          placeholder="Tell Anna your take before drafting..."
                          value={replyIntentText}
                          disabled={state.generatingDraft}
                          onChange={(e) => actions.setReplyIntentText(key, e.target.value)}
                        />
                      </label>
                      <div className="reply-intent-actions">
                        <button className="soft-btn" onClick={state.generatingDraft ? actions.stopDraftGeneration : () => void actions.generateDraft(undefined, { ignoreReplyIntent: true })}>
                          {state.generatingDraft ? "Stop generate" : "Skip, draft anyway"}
                        </button>
                        <button className="primary-btn" disabled={state.generatingDraft} onClick={() => void actions.generateDraft()}>
                          {state.generatingDraft ? "Generating..." : "Generate draft"}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="revise-row">
                      <input type="text" placeholder="Tell Anna how to revise this draft..." value={state.revisionById[key] || ""} disabled={state.generatingDraft} onChange={(e) => actions.setRevision(key, e.target.value)} />
                      <button className="soft-btn" disabled={state.generatingDraft || !draft.trim()} onClick={() => actions.clearDraft()}>
                        Clear local draft
                      </button>
                      <button className="soft-btn generate-draft-btn is-glow" disabled={state.generatingDraft} onClick={() => void actions.generateDraft()}>{state.generatingDraft ? "Generating..." : "Ask Anna to revise"}</button>
                    </div>
                  )}
                  <textarea ref={draftTextareaRef} className={`draft-textarea${state.generatingDraft && !draft ? " is-draft-loading" : ""}`} placeholder="Anna's editable draft will appear here after it is generated." value={draftDisplay} disabled={state.generatingDraft} onChange={(e) => actions.setDraft(key, e.target.value)} />
                </>
              )}
            </section>
            <div className="decision-row drawer-action-row">
              <button className="primary-btn" disabled={!draft.trim() || !!state.pendingAction} onClick={() => void actions.replyNow()}>Reply now</button>
              {isReviewCard ? <button className="soft-btn" disabled={!!state.pendingAction} onClick={() => void actions.markCardRead()}>Read</button> : null}
              <button className="soft-btn" disabled={!!state.pendingAction} onClick={() => void actions.recordDecision("no_action_needed")}>No action needed</button>
              <button className="soft-btn" disabled={!!state.pendingAction} onClick={() => void actions.recordDecision("handled_manually")}>Handled manually</button>
              {nid ? <button className="detail-back" style={{ marginLeft: "auto" }} onClick={() => void actions.openCard(nid)}>Next card →</button> : null}
            </div>
          </aside>
        </div>
      </article>
    </section>
  );
}
