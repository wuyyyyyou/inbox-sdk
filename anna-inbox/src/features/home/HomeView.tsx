import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { useApp } from "../../app/AppContext";
import type { AiChatMessage, DraftReplyArtifact, CustomRunResult, InboxMessage, InboxThreadStateOperation } from "../../types/mail";
import { SnoozePicker } from "./SnoozePicker";
import { MailDetailDrawer } from "../mail-detail/MailDetailDrawer";

type FeedFilter = "important" | "other";
type MailboxView = "inbox" | "todos" | "starred" | "snoozed" | "done" | "drafts" | "sent" | "trash" | "spam" | "all";
type MailUiFlags = { todos: string[]; snoozed: string[]; done: string[]; drafts: string[]; saved: Record<string, InboxMessage> };
type CategoryFlag = "todos" | "snoozed" | "done" | "drafts";

const AI_SIDEBAR_WIDTH_KEY = "anna-inbox:ai-sidebar-width";
const AI_SIDEBAR_MIN_WIDTH = 260;
const AI_SIDEBAR_MAX_WIDTH = 680;
const CACHED_INBOX_BANNER_SKIP_KEY = "anna-inbox:cached-inbox-banner-skip";

const MAILBOX_VIEWS: Array<{ id: MailboxView; label: string }> = [
  { id: "inbox", label: "Inbox" }, { id: "todos", label: "Todos" }, { id: "starred", label: "Starred" },
  { id: "snoozed", label: "Snoozed" }, { id: "done", label: "Done" }, { id: "drafts", label: "Drafts" },
  { id: "sent", label: "Sent" }, { id: "trash", label: "Trash" }, { id: "spam", label: "Spam" }, { id: "all", label: "All mail" },
];

function scheduleDeferredWork(task: () => void, delayMs = 180) {
  const win = window as Window & {
    requestIdleCallback?: (callback: IdleRequestCallback, options?: IdleRequestOptions) => number;
    cancelIdleCallback?: (handle: number) => void;
  };
  if (typeof window !== "undefined" && typeof win.requestIdleCallback === "function" && typeof win.cancelIdleCallback === "function") {
    const idleId = win.requestIdleCallback(() => task(), { timeout: delayMs + 400 });
    const timer = window.setTimeout(() => {
      win.cancelIdleCallback?.(idleId);
      task();
    }, delayMs);
    return () => {
      window.clearTimeout(timer);
      win.cancelIdleCallback?.(idleId);
    };
  }
  const timer = window.setTimeout(task, delayMs);
  return () => window.clearTimeout(timer);
}

function sidebarWidthBounds() {
  if (typeof window === "undefined") return { min: AI_SIDEBAR_MIN_WIDTH, max: AI_SIDEBAR_MAX_WIDTH };
  const compactMin = window.innerWidth < 520 ? 180 : AI_SIDEBAR_MIN_WIDTH;
  const max = Math.max(compactMin, Math.min(AI_SIDEBAR_MAX_WIDTH, window.innerWidth - 180));
  return { min: compactMin, max };
}

function clampSidebarWidth(value: number) {
  const { min, max } = sidebarWidthBounds();
  return Math.min(max, Math.max(min, Math.round(value)));
}

function initialSidebarWidth() {
  if (typeof window === "undefined") return 420;
  try {
    const saved = Number(window.localStorage.getItem(AI_SIDEBAR_WIDTH_KEY));
    if (Number.isFinite(saved) && saved > 0) return clampSidebarWidth(saved);
  } catch {
    // Ignore unavailable storage; the width can still be adjusted for this session.
  }
  return clampSidebarWidth(Math.max(290, window.innerWidth * 0.31));
}

function persistSidebarWidth(value: number) {
  try {
    window.localStorage.setItem(AI_SIDEBAR_WIDTH_KEY, String(value));
  } catch {
    // Best-effort preference only.
  }
}

function cachedInboxBannerSkipStorageKey(mailbox: string) {
  return `${CACHED_INBOX_BANNER_SKIP_KEY}:${mailbox.trim().toLowerCase() || "global"}`;
}

function Icon({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>;
}

const SearchIcon = () => <Icon><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></Icon>;
const InboxIcon = () => <Icon><path d="M4 5h16l-1.5 14h-13L4 5Z" /><path d="M5 14h4l1.5 2h3l1.5-2h4" /></Icon>;
const SparkleIcon = () => <Icon><path d="M12 3c.6 4.5 2.8 6.7 7.2 7.2-4.4.5-6.6 2.7-7.2 7.2-.6-4.5-2.8-6.7-7.2-7.2C9.2 9.7 11.4 7.5 12 3Z" /><path d="M19 3v4M17 5h4" /></Icon>;
const SendIcon = () => <Icon><path d="m4 4 16 8-16 8 3-8-3-8Z" /><path d="M7 12h13" /></Icon>;
const StopIcon = () => <Icon><rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" stroke="none" /></Icon>;
const RefreshIcon = () => <Icon><path d="M20 6v5h-5" /><path d="M19 11a7.5 7.5 0 1 0 .2 5" /></Icon>;
const SettingsIcon = () => <Icon><circle cx="12.6" cy="12" r="3" /><path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.4 1A8 8 0 0 0 15 6l-.3-2.6h-4L10.5 6A8 8 0 0 0 9 7l-2.4-1-2 3.4 2 1.6a7 7 0 0 0 0 2l-2 1.6 2 3.4L9 17a8 8 0 0 0 1.5 1l.3 2.6h4L15 18a8 8 0 0 0 1.5-1l2.4 1 2-3.4-2-1.5c.1-.4.1-.7.1-1.1Z" /></Icon>;
const HistoryIcon = () => <Icon><path d="M4 5v5h5" /><path d="M5 10a8 8 0 1 1 1.4 6.4" /><path d="M12 8v5l3 2" /></Icon>;
const ChevronLeftIcon = () => <Icon><path d="m15 18-6-6 6-6" /></Icon>;
const ChevronDownIcon = () => <Icon><path d="m7 9 5 5 5-5" /></Icon>;
const CheckIcon = () => <Icon><path d="m5 12 4 4L19 6" /></Icon>;
const PaperclipIcon = () => <Icon><path d="m9 12 5.7-5.7a3 3 0 0 1 4.2 4.2l-7.8 7.8a5 5 0 0 1-7.1-7.1l7.4-7.4" /></Icon>;
const MailOpenIcon = () => <Icon><path d="M3 8.5 12 14l9-5.5" /><path d="M5 6h14a2 2 0 0 1 2 2v10H3V8a2 2 0 0 1 2-2Z" /></Icon>;
const StarIcon = () => <Icon><path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3Z" /></Icon>;
const ClockIcon = () => <Icon><circle cx="12" cy="12" r="8" /><path d="M12 7v5l3 2" /></Icon>;
const TrashIcon = () => <Icon><path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13" /></Icon>;
const TodoIcon = () => <Icon><rect x="4" y="4" width="16" height="16" rx="4" /><path d="m8 12 2.5 2.5L16 9" /></Icon>;
const DraftIcon = () => <Icon><path d="M6 3h9l4 4v14H6z" /><path d="M14 3v5h5M9 12h6M9 16h6" /></Icon>;
const SentIcon = () => <Icon><path d="m3 4 18 8-18 8 4-8-4-8Z" /><path d="M7 12h14" /></Icon>;
const SpamIcon = () => <Icon><circle cx="12" cy="12" r="9" /><path d="M12 7v6M12 17h.01" /></Icon>;
const AllMailIcon = () => <Icon><rect x="3" y="5" width="18" height="14" rx="3" /><path d="m4 8 8 6 8-6" /></Icon>;
const AllDoneIcon = () => <Icon><path d="m4 12 3 3 5-6M11 14l2 2 7-8" /></Icon>;
const ImportantIcon = () => <Icon><path d="M5 6h10l4 6-4 6H5l4-6-4-6Z" /></Icon>;
const PersonIcon = () => <Icon><path d="M15 19v-1a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v1" /><circle cx="10" cy="7" r="3" /></Icon>;

function FolderIcon({ view }: { view: MailboxView }) {
  if (view === "todos") return <TodoIcon />;
  if (view === "starred") return <StarIcon />;
  if (view === "snoozed") return <ClockIcon />;
  if (view === "done") return <CheckIcon />;
  if (view === "drafts") return <DraftIcon />;
  if (view === "sent") return <SentIcon />;
  if (view === "trash") return <TrashIcon />;
  if (view === "spam") return <SpamIcon />;
  if (view === "all") return <AllMailIcon />;
  return <InboxIcon />;
}

export function senderParts(value: unknown) {
  const text = String(value ?? "");
  const match = text.match(/^\s*"?([^"<]+?)"?\s*<([^>]+)>/);
  if (match) return { name: match[1].trim(), email: match[2].trim() };
  if (text.includes("@")) return { name: text.split("@")[0], email: text.trim() };
  return { name: text.trim() || "Unknown sender", email: text.trim() };
}

function splitAddresses(value: unknown): string[] {
  const text = String(value ?? "").trim();
  if (!text) return [];
  const parts: string[] = [];
  let start = 0;
  let quoted = false;
  let angleDepth = 0;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (character === '"') quoted = !quoted;
    else if (!quoted && character === "<") angleDepth += 1;
    else if (!quoted && character === ">") angleDepth = Math.max(0, angleDepth - 1);
    else if (!quoted && angleDepth === 0 && character === ",") {
      parts.push(text.slice(start, index).trim());
      start = index + 1;
    }
  }
  parts.push(text.slice(start).trim());
  return parts.filter(Boolean);
}

export function messageParticipant(message: InboxMessage, mailboxView: MailboxView, mailbox: string) {
  const labels = new Set((message.label_ids || []).map((label) => label.toUpperCase()));
  const sender = senderParts(message.from);
  const normalizedMailbox = mailbox.trim().toLowerCase();
  const outgoing = mailboxView === "sent" || labels.has("SENT") || labels.has("DRAFT")
    || Boolean(normalizedMailbox && sender.email.toLowerCase() === normalizedMailbox);
  const recipients = splitAddresses(message.to).map(senderParts);
  const outgoingParticipant = () => {
    const names = ["me", ...recipients
      .filter((item) => !normalizedMailbox || item.email.toLowerCase() !== normalizedMailbox)
      .map((item) => item.name)]
      .filter((name, index, all) => Boolean(name) && all.indexOf(name) === index);
    const name = names.join(", ") || "me";
    return {
      name,
      title: String(message.to || message.from || "Draft without recipients"),
      initial: recipients[0]?.name || "me",
      email: recipients[0]?.email || normalizedMailbox,
      outgoing,
    };
  };

  if (labels.has("DRAFT")) {
    return outgoingParticipant();
  }

  if (outgoing) {
    return outgoingParticipant();
  }

  const fallbackName = labels.has("DRAFT") ? "Draft" : "No sender";
  return {
    name: sender.name === "Unknown sender" ? fallbackName : sender.name,
    title: String(message.from || fallbackName),
    initial: sender.name === "Unknown sender" ? fallbackName : sender.name,
    email: sender.email,
    outgoing,
  };
}

function senderInitial(message: InboxMessage) {
  const name = senderParts(message.from).name;
  return (name.match(/[A-Za-z0-9]/)?.[0] || name.slice(0, 1) || "?").toUpperCase();
}

function messageDate(message: InboxMessage) {
  const milliseconds = Number(message.internal_date || 0);
  const date = milliseconds ? new Date(milliseconds) : new Date(message.date || "");
  return Number.isNaN(date.getTime()) ? null : date;
}

function dateLabel(message: InboxMessage) {
  const date = messageDate(message);
  if (!date) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return date.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function groupLabel(message: InboxMessage) {
  const date = messageDate(message);
  if (!date) return "LAST 7 DAYS";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return "TODAY";
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return "YESTERDAY";
  return "EARLIER THIS WEEK";
}

export function hasMessageLabel(message: InboxMessage, label: string) {
  return (message.label_ids || []).includes(label);
}

export function isImportantMessage(message: InboxMessage) {
  return message.important ?? hasMessageLabel(message, "IMPORTANT");
}

export function isStarredMessage(message: InboxMessage) {
  return message.starred ?? hasMessageLabel(message, "STARRED");
}

export function isDraftMessage(message: InboxMessage) {
  return (message.label_ids || []).some((label) => label.toUpperCase() === "DRAFT");
}

function InboxRow({ message, selected, flags, mailboxView, mailbox, onSelect, onFlag, onSnooze, onPrefetch, avatarUrl }: {
  message: InboxMessage; selected: boolean; flags: MailUiFlags; mailboxView: MailboxView; mailbox: string; onSelect: () => void;
  onFlag: (kind: CategoryFlag, message: InboxMessage) => void;
  onSnooze: (message: InboxMessage) => void;
  onPrefetch: () => void;
  avatarUrl?: string;
}) {
  const { actions } = useApp();
  const [avatarFailed, setAvatarFailed] = useState(false);
  const sender = senderParts(message.from);
  const participant = messageParticipant(message, mailboxView, mailbox);
  const sentView = participant.outgoing;
  const starred = isStarredMessage(message);
  const draft = isDraftMessage(message);
  const preview = message.snippet || message.body_preview || "No preview available";
  return (
    <article className={`mail-row ${mailboxView === "all" ? "is-all-mail" : ""} ${message.unread ? "is-unread" : ""} ${selected ? "is-selected" : ""}`}>
      <button className="mail-row-main" data-mail-row-id={message.id} onMouseEnter={onPrefetch} onFocus={onPrefetch} onClick={onSelect} aria-expanded={selected}>
        {avatarUrl && !avatarFailed
          ? <img className="sender-avatar is-photo" src={avatarUrl} alt="" referrerPolicy="no-referrer" onError={() => setAvatarFailed(true)} />
          : <span className={`sender-avatar tone-${(participant.initial.charCodeAt(0) || 65) % 5}`}>{(participant.initial.match(/[A-Za-z0-9]/)?.[0] || participant.initial.slice(0, 1) || "?").toUpperCase()}</span>}
        <span className="mail-sender" title={participant.title}>{participant.name}</span>
        <span className="mail-content">
          <strong>{message.subject || "(no subject)"}</strong>
          <span>— {draft ? <><b className="mail-draft-label">Draft:</b> {preview}</> : preview}</span>
        </span>
        <span className="mail-flags">
          {isImportantMessage(message) && !sentView ? <span className="mail-important-icon" title="Important"><ImportantIcon /></span> : null}
          {draft
            ? <span className="mail-draft-icon" title="Draft"><DraftIcon /></span>
            : sentView ? <span className="mail-sent-check" title="Sent"><CheckIcon /></span> : null}
          {starred ? <span className="mail-starred" title="Starred"><StarIcon /></span> : null}
          {message.has_attachment ? <span className="mail-attachment" title={`${message.attachment_count || 1} attachment(s)`}><PaperclipIcon /></span> : null}
        </span>
        <time>{dateLabel(message)}</time>
      </button>
      <span className="mail-row-actions">
        <button className={starred ? "is-active" : ""} aria-label={starred ? "Unstar" : "Star"} data-tooltip={starred ? "Unstar" : "Star"} onClick={() => void actions.setInboxStarred(message.id, !starred)}><StarIcon /></button>
        <button className={flags.todos.includes(message.id) ? "is-active" : ""} aria-label="Add to Todos" data-tooltip="Add to Todos" onClick={() => onFlag("todos", message)}><TodoIcon /></button>
        <button className={flags.snoozed.includes(message.id) ? "is-active" : ""} aria-label="Snooze" data-tooltip="Snooze" onClick={() => onSnooze(message)}><ClockIcon /></button>
        {message.unread ? <button aria-label="Mark as read" data-tooltip="Mark as read" onClick={() => void actions.markInboxRead(message.id)}><MailOpenIcon /></button> : null}
        <button aria-label="Move to trash" data-tooltip="Move to trash" onClick={() => void actions.trashInboxMessage(message.id)}><TrashIcon /></button>
        <button className={flags.done.includes(message.id) ? "is-active" : ""} aria-label="Done" data-tooltip="Done" onClick={() => onFlag("done", message)}><CheckIcon /></button>
      </span>
    </article>
  );
}

function aiResultCount(result: CustomRunResult) {
  return (result.sections || []).reduce((total, section) => total + (section.items?.length || 0), 0);
}

function aiSearchStatus(result: CustomRunResult) {
  const itemCount = aiResultCount(result);
  if (itemCount > 0) return `Found ${itemCount} relevant thread${itemCount === 1 ? "" : "s"}.`;
  const queryCount = result.plan_gmail_queries?.length || 0;
  if (queryCount > 0) return `Searched ${queryCount} focused inbox quer${queryCount === 1 ? "y" : "ies"}.`;
  return "Finished searching your inbox.";
}

function aiTimeLabel(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function relativeTimeLabel(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const diffMs = Date.now() - date.getTime();
  if (diffMs < 0) return "just now";
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diffMs < minute) return "just now";
  if (diffMs < hour) return `${Math.max(1, Math.floor(diffMs / minute))}m ago`;
  if (diffMs < day) return `${Math.floor(diffMs / hour)}h ago`;
  if (diffMs < day * 2) return "yesterday";
  if (diffMs < day * 7) return `${Math.floor(diffMs / day)}d ago`;
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function displayAssistantText(message: AiChatMessage) {
  const text = message.content || "";
  if (message.kind === "error" && (text.includes("[tool_failed]") || text.includes("executa process exited"))) {
    return "The inbox scan was interrupted. You can retry this question in a moment.";
  }
  return text;
}

function shouldAnimateAssistantText(timestamp?: string) {
  if (!timestamp) return false;
  const createdAt = new Date(timestamp).getTime();
  if (!Number.isFinite(createdAt)) return false;
  return Date.now() - createdAt < 15_000;
}

function AnimatedAssistantText({ text, animate }: { text: string; animate: boolean }) {
  const [visibleText, setVisibleText] = useState(animate ? "" : text);

  useEffect(() => {
    if (!animate) {
      setVisibleText(text);
      return;
    }
    setVisibleText("");
    let frame = 0;
    let timer = window.setInterval(() => {
      frame += Math.max(1, Math.ceil(text.length / 36));
      if (frame >= text.length) {
        window.clearInterval(timer);
        setVisibleText(text);
        return;
      }
      setVisibleText(text.slice(0, frame));
    }, 24);
    return () => window.clearInterval(timer);
  }, [animate, text]);

  return <p>{visibleText}</p>;
}

function AiAssistantMessage({ message, onInsertArtifact }: { message: AiChatMessage; onInsertArtifact: (artifact: DraftReplyArtifact) => void }) {
  const { actions } = useApp();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submittingGap, setSubmittingGap] = useState(false);

  if (message.pending) {
    return (
      <div className="ai-message is-assistant is-thinking-inline" aria-live="polite" aria-busy="true">
        <p>Thinking</p>
      </div>
    );
  }

  const result = message.result;
  if (!result) {
    const text = displayAssistantText(message);
    const animate = shouldAnimateAssistantText(message.timestamp);
    const submitReplyGap = async () => {
      if (!message.mailContext || !message.sourcePrompt || !message.replyGaps?.needs_user_input) return;
      const missingRequired = message.replyGaps.questions.filter((question) => question.required !== false && !String(answers[question.id] || "").trim());
      if (missingRequired.length) {
        actions.showToast("Answer the required questions first.");
        return;
      }
      setSubmittingGap(true);
      try {
        await actions.submitMailContextPrompt({
          visiblePrompt: message.sourcePrompt,
          context: message.mailContext,
          expectedArtifact: "draft_reply",
          userAnswers: answers,
        });
      } finally {
        setSubmittingGap(false);
      }
    };
    return (
      <div className={`ai-message is-assistant ${message.kind === "error" ? "is-error" : ""} ${message.kind === "stopped" ? "is-stopped" : ""}`}>
        <AnimatedAssistantText text={text} animate={animate} />
        {message.artifact ? (
          <div className="ai-draft-artifact">
            <pre>{message.artifact.body}</pre>
            <div className="ai-draft-artifact-actions">
              <button onClick={() => onInsertArtifact(message.artifact!)}>Insert draft reply</button>
              <button disabled title="Coming later">Insert into new email</button>
              <button onClick={() => void actions.copyDraft(message.artifact!.body)}>Copy draft</button>
            </div>
          </div>
        ) : null}
        {message.replyGaps?.needs_user_input ? (
          <div className="ai-reply-gaps">
            {message.replyGaps.summary ? <p>{message.replyGaps.summary}</p> : null}
            {message.replyGaps.questions.map((question) => (
              <label key={question.id}>
                <span>{question.question}</span>
                <input
                  value={answers[question.id] || ""}
                  placeholder={question.hint || "Your answer"}
                  onChange={(event) => setAnswers((current) => ({ ...current, [question.id]: event.target.value }))}
                />
              </label>
            ))}
            <button onClick={() => void submitReplyGap()} disabled={submittingGap}>{submittingGap ? "Generating…" : "Generate draft"}</button>
          </div>
        ) : null}
        <time>{aiTimeLabel(message.timestamp)}</time>
      </div>
    );
  }
  const sections = (result.sections || []).slice(0, 4);
  const summaryText = message.content || result.summary || result.plan_description || "Anna finished scanning your inbox.";
  const animate = shouldAnimateAssistantText(message.timestamp);
  return (
    <div className="ai-message is-assistant">
      <div className="ai-answer-meta">{aiSearchStatus(result)} {result.plan_gmail_queries?.[0]?.query ? <code>{result.plan_gmail_queries[0].query}</code> : null}</div>
      <AnimatedAssistantText text={summaryText} animate={animate} />
      {result.title || result.plan_title ? <h2>{result.title || result.plan_title}</h2> : null}
      {sections.length ? (
        <ul className="ai-answer-list">
          {sections.map((section, index) => (
            <li key={`${section.heading || "section"}-${index}`}>
              {section.heading ? <strong>{section.heading}</strong> : null}
              {section.body ? <span>{section.body}</span> : null}
              {section.items?.length ? (
                <div className="ai-answer-items">
                  {section.items.slice(0, 4).map((item, itemIndex) => (
                    <span key={`${item.subject || "item"}-${itemIndex}`}>
                      {item.subject ? <b>{item.subject}</b> : null}
                      {item.context || item.suggestion ? ` ${item.context || item.suggestion}` : null}
                    </span>
                  ))}
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      <time>{aiTimeLabel(message.timestamp)}</time>
    </div>
  );
}

function AiMessageBubble({ message, onInsertArtifact }: { message: AiChatMessage; onInsertArtifact: (artifact: DraftReplyArtifact) => void }) {
  if (message.role === "user") {
    return <div className="ai-message is-user">{message.content}</div>;
  }
  return <AiAssistantMessage message={message} onInsertArtifact={onInsertArtifact} />;
}

function AiSidebar({ collapsed, onToggle, onInsertArtifact }: { collapsed: boolean; onToggle: () => void; onInsertArtifact: (artifact: DraftReplyArtifact) => void }) {
  const { state, actions } = useApp();
  const [historyOpen, setHistoryOpen] = useState(false);
  const running = state.aiChatLoading || state.isCustomScanning || Boolean(state.customRunProgress && state.customRunProgress.status !== "failed");
  const starters = ["What needs my reply?", "Find urgent emails", "Plan my day"];
  const conversation = state.aiChatMessages;

  const submit = () => {
    if (!state.customScanInput.trim() || running) return;
    setHistoryOpen(false);
    void actions.sendAiChatMessage();
  };

  const startNewChat = () => {
    actions.startNewAiConversation();
    setHistoryOpen(false);
  };

  return (
    <aside className={`ai-sidebar ${collapsed ? "is-collapsed" : ""}`}>
      <div className="ai-sidebar-head">
        <div className="anna-wordmark"><button className="anna-logo-button" title={collapsed ? "Expand AI sidebar" : "Collapse AI sidebar"} onClick={onToggle}><SparkleIcon /></button><label>Anna Inbox</label></div>
        <button className="new-chat-btn" onClick={startNewChat}><span>＋</span> New chat</button>
      </div>

      <div className="ai-conversation">
        {conversation.length ? (
          <div className="ai-message-stack" aria-live="polite">
            {conversation.map((message) => <AiMessageBubble key={message.id} message={message} onInsertArtifact={onInsertArtifact} />)}
          </div>
        ) : (
          <div className="ai-empty-state">
            <div className="ai-orb"><SparkleIcon /></div>
            <h1>How can I help you today?</h1>
            <p>Ask Anna to find, organize, or summarize anything in your inbox.</p>
          </div>
        )}
      </div>

      <section className={`ask-history-drawer ${historyOpen ? "is-open" : ""}`} aria-hidden={!historyOpen}>
        <div className="ask-history-head">
          <button onClick={() => setHistoryOpen(false)} aria-label="Back to Ask"><ChevronLeftIcon /></button>
          <div><strong>Ask history</strong><span>Your previous conversations with Anna</span></div>
        </div>
        <div className="ask-history-list">
          {state.askHistory.length ? state.askHistory.map((entry, index) => (
            <button key={`${entry.timestamp}-${index}`} className={state.aiChatConversationId && state.aiChatConversationId === entry.conversationId ? "is-active" : ""} onClick={() => {
              actions.openAiConversation(index);
              setHistoryOpen(false);
            }}>
              <div className="ask-history-row-top">
                <strong>{entry.query || entry.result.title || "Inbox question"}</strong>
                <time>{relativeTimeLabel(entry.timestamp)}</time>
              </div>
              <span>{entry.result.summary || entry.result.plan_description || "Open conversation"}</span>
            </button>
          )) : <div className="ask-history-empty"><HistoryIcon /><span>No conversation history</span></div>}
        </div>
      </section>

      <div className="ai-composer-wrap">
        <div className={`ai-composer ${running ? "is-running" : ""}`}>
          <textarea
            value={state.customScanInput}
            placeholder="Find, organize, ask anything…"
            rows={3}
            onChange={(event) => actions.setInput("customScanInput", event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit();
              }
            }}
          />
          <div className="ai-composer-footer">
            <span></span>
            <div className="ai-composer-actions">
              {state.aiChatLoading ? (
                <button className="ai-stop-button" onClick={actions.stopAiGeneration} aria-label="Stop generating" title="Stop generating"><StopIcon /></button>
              ) : null}
              <button disabled={running || !state.customScanInput.trim()} onClick={submit} aria-label="Ask Anna"><SendIcon /></button>
            </div>
          </div>
        </div>
        {!conversation.length ? <div className="ai-starters">{starters.map((starter) => <button key={starter} onClick={() => actions.setInput("customScanInput", starter)}>{starter}</button>)}</div> : null}
      </div>

      <div className="ai-sidebar-foot">
        <span><i className={state.runtime.connected ? "is-live" : ""} />{state.runtime.connected ? "LLM Connected" : "LLMOffline"}</span>
        <div>
          <button className={historyOpen ? "is-active" : ""} title="Ask history" onClick={() => setHistoryOpen((open) => !open)}><HistoryIcon /></button>
        </div>
      </div>
    </aside>
  );
}

export function hasMailboxScanError(lastScanStatus?: string, lastError?: string, scanError?: string) {
  const normalizedStatus = String(lastScanStatus || "").trim().toLowerCase();
  return Boolean(scanError?.trim())
    || Boolean(lastError?.trim())
    || normalizedStatus === "failed"
    || normalizedStatus === "error";
}

function AccountAvatar({ email, url, className }: { email: string; url?: string; className: string }) {
  const [failed, setFailed] = useState(false);
  const normalizedEmail = email.trim();
  if (!normalizedEmail) {
    return <span className={`${className} is-default`} aria-hidden="true"><PersonIcon /></span>;
  }
  const initial = (email.split("@")[0]?.charAt(0) || "A").toUpperCase();
  return url && !failed
    ? <img className={className} src={url} alt="" referrerPolicy="no-referrer" onError={() => setFailed(true)} />
    : <span className={className}>{initial}</span>;
}

function AccountRail() {
  const { state, actions } = useApp();
  const [menuOpen, setMenuOpen] = useState(false);
  const mailbox = state.mailbox || state.selectedMailboxes[0] || "";
  const activeMailbox = state.mailboxes.find((item) => item.email.toLowerCase() === mailbox.toLowerCase());
  const authorizedMailboxes = state.mailboxes.filter((item) => item.authorized !== false);
  const mailboxes = state.mailboxes.length
    ? authorizedMailboxes
    : mailbox ? [{ email: mailbox, provider: "gmail", authorized: state.gmailAuthStatus.authorized }] : [];
  const scanFailed = hasMailboxScanError(activeMailbox?.last_scan_status, activeMailbox?.last_error, state.scanError);

  return (
    <aside className="account-rail" aria-label="Account controls">
      <button className="account-avatar-btn" title="Switch account" aria-expanded={menuOpen} onClick={() => setMenuOpen((open) => !open)}>
        <AccountAvatar email={mailbox} url={activeMailbox?.avatar_url} className="account-avatar-image" /><i className={scanFailed ? "is-inactive" : ""} />
      </button>
      <button className="account-settings-btn" title="Settings" onClick={() => actions.setDrawer("sources", true)}><SettingsIcon /></button>
      {menuOpen ? <button className="account-menu-backdrop" aria-label="Close account menu" onClick={() => setMenuOpen(false)} /> : null}
      <section className={`account-menu ${menuOpen ? "is-open" : ""}`} aria-hidden={!menuOpen}>
        <header><span>Accounts</span><small>Switch inbox</small></header>
        <div>
          {mailboxes.map((item) => {
            const active = item.email.toLowerCase() === mailbox.toLowerCase();
            return (
              <button key={item.email} className={active ? "is-active" : ""} disabled={item.authorized === false} onClick={() => {
                setMenuOpen(false);
                void actions.switchMailbox(item.email);
              }}>
                <AccountAvatar email={item.email} url={item.avatar_url} className="account-menu-avatar" />
                <span className="account-menu-copy"><strong>{item.email.split("@")[0]}</strong><small>{item.email}</small></span>
                {active ? <span className="account-menu-check"><CheckIcon /></span> : null}
              </button>
            );
          })}
        </div>
      </section>
    </aside>
  );
}

export function HomeView() {
  const { state, actions } = useApp();
  const [filter, setFilter] = useState<FeedFilter>("important");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [snoozeTarget, setSnoozeTarget] = useState<InboxMessage | null>(null);
  const [insertRequest, setInsertRequest] = useState<{ nonce: string; artifact: DraftReplyArtifact } | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(initialSidebarWidth);
  const [sidebarResizing, setSidebarResizing] = useState(false);
  const [folderOpen, setFolderOpen] = useState(false);
  const [mailboxView, setMailboxView] = useState<MailboxView>("inbox");
  const [days, setDays] = useState(7);
  const [messageBodies, setMessageBodies] = useState<Record<string, { status: "loading" | "ready" | "error"; body: string }>>({});
  const bodyRequests = useRef(new Set<string>());
  const avatarRequestKey = useRef("");
  const avatarMisses = useRef(new Set<string>());
  const avatarPermissionNoticeShown = useRef(false);
  const mailbox = state.selectedMailboxes[0] || state.mailbox;
  const flagsKey = `anna-inbox:mail-flags:${mailbox}`;
  const [flags, setFlags] = useState<MailUiFlags>({ todos: [], snoozed: [], done: [], drafts: [], saved: {} });
  const [contactAvatars, setContactAvatars] = useState<Record<string, string>>({});
  const [cachedInboxBannerDismissed, setCachedInboxBannerDismissed] = useState(false);
  const sidebarWidthRef = useRef(sidebarWidth);
  const sidebarDragRef = useRef<{ pointerId: number; startX: number; startWidth: number } | null>(null);
  const sidebarBounds = sidebarWidthBounds();
  const layoutStyle = { "--ai-sidebar-width": `${sidebarWidth}px` } as CSSProperties;

  const applySidebarWidth = useCallback((value: number) => {
    const next = clampSidebarWidth(value);
    sidebarWidthRef.current = next;
    setSidebarWidth(next);
    return next;
  }, []);

  useEffect(() => {
    sidebarWidthRef.current = sidebarWidth;
  }, [sidebarWidth]);

  useEffect(() => {
    const handleResize = () => {
      const next = applySidebarWidth(sidebarWidthRef.current);
      persistSidebarWidth(next);
    };
    window.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("resize", handleResize);
      document.body.classList.remove("is-resizing-ai-sidebar");
    };
  }, [applySidebarWidth]);

  const startSidebarResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (sidebarCollapsed) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    sidebarDragRef.current = { pointerId: event.pointerId, startX: event.clientX, startWidth: sidebarWidthRef.current };
    setSidebarResizing(true);
    document.body.classList.add("is-resizing-ai-sidebar");
  };

  const moveSidebarResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = sidebarDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    applySidebarWidth(drag.startWidth + event.clientX - drag.startX);
  };

  const endSidebarResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = sidebarDragRef.current;
    if (drag?.pointerId === event.pointerId && event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (!drag || drag.pointerId !== event.pointerId) return;
    sidebarDragRef.current = null;
    setSidebarResizing(false);
    document.body.classList.remove("is-resizing-ai-sidebar");
    persistSidebarWidth(sidebarWidthRef.current);
  };

  const adjustSidebarWithKeyboard = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (sidebarCollapsed) return;
    const step = event.shiftKey ? 40 : 16;
    let next: number | null = null;
    if (event.key === "ArrowLeft") next = sidebarWidthRef.current - step;
    if (event.key === "ArrowRight") next = sidebarWidthRef.current + step;
    if (event.key === "Home") next = sidebarWidthBounds().min;
    if (event.key === "End") next = sidebarWidthBounds().max;
    if (next === null) return;
    event.preventDefault();
    persistSidebarWidth(applySidebarWidth(next));
  };

  useEffect(() => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(flagsKey) || "{}");
      setFlags({
        todos: Array.isArray(saved.todos) ? saved.todos : [],
        snoozed: Array.isArray(saved.snoozed) ? saved.snoozed : [], done: Array.isArray(saved.done) ? saved.done : [],
        drafts: Array.isArray(saved.drafts) ? saved.drafts : [], saved: saved.saved && typeof saved.saved === "object" ? saved.saved : {},
      });
    } catch { setFlags({ todos: [], snoozed: [], done: [], drafts: [], saved: {} }); }
    try {
      const cached = JSON.parse(window.localStorage.getItem(`anna-inbox:contact-avatars:${mailbox}`) || "{}");
      const avatarsFresh = Number(cached?.avatarsUpdatedAt || cached?.updatedAt || 0) > Date.now() - 7 * 24 * 60 * 60 * 1000;
      const missesFresh = Number(cached?.missingUpdatedAt || 0) > Date.now() - 24 * 60 * 60 * 1000;
      setContactAvatars(avatarsFresh && cached.avatars && typeof cached.avatars === "object" ? cached.avatars : {});
      avatarMisses.current = new Set(missesFresh && Array.isArray(cached.missing) ? cached.missing : []);
    } catch {
      setContactAvatars({});
      avatarMisses.current = new Set();
    }
    avatarRequestKey.current = "";
    avatarPermissionNoticeShown.current = false;
    setMailboxView("inbox"); setFilter("important"); setDays(7);
  }, [flagsKey]);

  useEffect(() => {
    try {
      setCachedInboxBannerDismissed(window.localStorage.getItem(cachedInboxBannerSkipStorageKey(mailbox)) === "1");
    } catch {
      setCachedInboxBannerDismissed(false);
    }
  }, [mailbox]);

  useEffect(() => {
    if (!state.inboxError) {
      setCachedInboxBannerDismissed(false);
      try {
        window.localStorage.removeItem(cachedInboxBannerSkipStorageKey(mailbox));
      } catch {
        // Ignore storage failures; dismissal is a best-effort preference.
      }
    }
  }, [mailbox, state.inboxError]);

  const counts = useMemo(() => ({
    important: state.inboxMessages.filter((message) => isImportantMessage(message) && !flags.todos.includes(message.id) && !flags.done.includes(message.id) && !flags.snoozed.includes(message.id)).length,
    other: state.inboxMessages.filter((message) => !isImportantMessage(message) && !flags.todos.includes(message.id) && !flags.done.includes(message.id) && !flags.snoozed.includes(message.id)).length,
  }), [flags, state.inboxMessages]);
  const localCategory = mailboxView === "todos" || mailboxView === "snoozed" || mailboxView === "done";

  const sourceMessages = useMemo(() => {
    const localKind = localCategory ? mailboxView as "todos" | "snoozed" | "done" : null;
    if (!localKind) {
      const source = state.inboxSnapshotMessages.length ? state.inboxSnapshotMessages : state.inboxMessages;
      if (mailboxView === "inbox") return source.filter((message) => hasMessageLabel(message, "INBOX"));
      if (mailboxView === "starred") return source.filter((message) => hasMessageLabel(message, "STARRED"));
      if (mailboxView === "drafts") return source.filter((message) => hasMessageLabel(message, "DRAFT"));
      if (mailboxView === "sent") return source.filter((message) => hasMessageLabel(message, "SENT"));
      if (mailboxView === "trash") return source.filter((message) => hasMessageLabel(message, "TRASH"));
      if (mailboxView === "spam") return source.filter((message) => hasMessageLabel(message, "SPAM"));
      if (mailboxView === "all") return source.filter((message) => !["TRASH", "SPAM", "CHAT"].some((label) => hasMessageLabel(message, label)));
      return source;
    }
    return flags[localKind].map((id) => flags.saved[id]).filter((message): message is InboxMessage => Boolean(message));
  }, [flags, localCategory, mailboxView, state.inboxMessages, state.inboxSnapshotMessages]);

  const messagesInThread = useCallback((message: InboxMessage, currentFlags: MailUiFlags = flags) => {
    const threadId = message.thread_id || message.id;
    const byId = new Map<string, InboxMessage>();
    for (const item of [message, ...sourceMessages, ...state.inboxSnapshotMessages, ...state.inboxMessages, ...Object.values(currentFlags.saved)]) {
      if ((item.thread_id || item.id) === threadId) byId.set(item.id, item);
    }
    return [...byId.values()];
  }, [flags, sourceMessages, state.inboxMessages, state.inboxSnapshotMessages]);

  const setWorkflowFlag = useCallback((kind: "todos" | "snoozed" | "done", messages: InboxMessage[], enabled: boolean) => {
    const ids = new Set(messages.map((item) => item.id));
    setFlags((current) => {
      const next = { ...current, saved: { ...current.saved } };
      for (const workflowKind of ["todos", "snoozed", "done"] as const) {
        const retained = current[workflowKind].filter((id) => !ids.has(id));
        next[workflowKind] = enabled && workflowKind === kind ? [...retained, ...ids] : retained;
      }
      for (const item of messages) next.saved[item.id] = item;
      window.localStorage.setItem(flagsKey, JSON.stringify(next));
      return next;
    });
  }, [flagsKey]);

  const restoreWorkflowFlags = useCallback((messages: InboxMessage[], previous: MailUiFlags) => {
    const ids = new Set(messages.map((item) => item.id));
    setFlags((current) => {
      const next = { ...current, saved: { ...current.saved } };
      for (const workflowKind of ["todos", "snoozed", "done"] as const) {
        next[workflowKind] = [
          ...current[workflowKind].filter((id) => !ids.has(id)),
          ...previous[workflowKind].filter((id) => ids.has(id)),
        ];
      }
      window.localStorage.setItem(flagsKey, JSON.stringify(next));
      return next;
    });
  }, [flagsKey]);

  const updateFlag = useCallback((kind: CategoryFlag, message: InboxMessage) => {
    if (kind === "drafts") return;
    const messages = messagesInThread(message);
    setWorkflowFlag(kind, messages, !flags[kind].includes(message.id));
  }, [flags, messagesInThread, setWorkflowFlag]);

  const prefetchMessageBody = async (message: InboxMessage) => {
    if (message.id === selectedId) return;
    const key = `${message.mailbox || mailbox}:${message.id}`;
    if (bodyRequests.current.has(key) || messageBodies[key]?.status === "ready") return;
    bodyRequests.current.add(key);
    setMessageBodies((current) => ({ ...current, [key]: { status: "loading", body: current[key]?.body || "" } }));
    try {
      const body = await actions.loadInboxEmailBody(message.id, message.mailbox || mailbox);
      setMessageBodies((current) => ({ ...current, [key]: { status: "ready", body } }));
    } catch {
      setMessageBodies((current) => ({ ...current, [key]: { status: "error", body: "" } }));
    } finally {
      bodyRequests.current.delete(key);
    }
  };

  useEffect(() => {
    const messages = state.inboxSnapshotMessages.length ? state.inboxSnapshotMessages : state.inboxMessages;
    if (!mailbox || !messages.length) return;
    const cleanup = scheduleDeferredWork(() => {
      const emails = [...new Set(messages.flatMap((message) => [message.from, message.to]
        .flatMap(splitAddresses)
        .map((value) => senderParts(value).email.toLowerCase())
        .filter((email) => email.includes("@"))))]
        .filter((email) => !contactAvatars[email] && !avatarMisses.current.has(email))
        .slice(0, 200);
      const requestKey = `${mailbox}:${emails.join("|")}`;
      if (!emails.length || avatarRequestKey.current === requestKey) return;
      avatarRequestKey.current = requestKey;
      void actions.loadContactAvatars(emails, mailbox).then(({ avatars, permissionRequired }) => {
        if (permissionRequired) {
          if (!avatarPermissionNoticeShown.current) {
            avatarPermissionNoticeShown.current = true;
            actions.showToast("Reconnect Google to load saved contact photos.");
          }
          return;
        }
        const missing = emails.filter((email) => !avatars[email]);
        for (const email of missing) avatarMisses.current.add(email);
        setContactAvatars((current) => {
          const next = { ...current, ...avatars };
          window.localStorage.setItem(`anna-inbox:contact-avatars:${mailbox}`, JSON.stringify({
            avatars: next,
            missing: [...avatarMisses.current],
            avatarsUpdatedAt: Date.now(),
            missingUpdatedAt: Date.now(),
          }));
          return next;
        });
      }).catch(() => undefined);
    }, 320);
    return cleanup;
    // Avatar lookup is auxiliary and follows mailbox snapshot changes only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mailbox, state.inboxMessages, state.inboxSnapshotMessages]);

  const visible = useMemo(() => {
    const query = search.trim().toLowerCase();
    return sourceMessages.filter((message) => {
      const matchesView = mailboxView === "inbox" ? !flags.todos.includes(message.id) && !flags.done.includes(message.id) && !flags.snoozed.includes(message.id)
        : mailboxView === "todos" ? flags.todos.includes(message.id)
        : mailboxView === "snoozed" ? flags.snoozed.includes(message.id)
          : mailboxView === "done" ? flags.done.includes(message.id)
            : mailboxView === "starred" ? isStarredMessage(message) : true;
      const matchesFilter = mailboxView !== "inbox" ? true : filter === "important" ? isImportantMessage(message)
        : !isImportantMessage(message) && !flags.todos.includes(message.id);
      if (!matchesView) return false;
      if (!matchesFilter) return false;
      if (!query) return true;
      return `${message.from || ""} ${message.subject || ""} ${message.snippet || ""}`.toLowerCase().includes(query);
    });
  }, [filter, flags, mailboxView, search, sourceMessages]);

  const dismissCachedInboxBanner = () => {
    setCachedInboxBannerDismissed(true);
    try {
      window.localStorage.setItem(cachedInboxBannerSkipStorageKey(mailbox), "1");
    } catch {
      // Ignore storage failures; dismissal is a best-effort preference.
    }
  };

  const grouped = useMemo(() => {
    const groups: Array<{ label: string; messages: InboxMessage[] }> = [];
    for (const message of visible) {
      const label = groupLabel(message);
      const current = groups[groups.length - 1];
      if (!current || current.label !== label) groups.push({ label, messages: [message] });
      else current.messages.push(message);
    }
    return groups;
  }, [visible]);

  const selectedMessage = useMemo(() => {
    if (!selectedId) return null;
    return sourceMessages.find((item) => item.id === selectedId)
      || state.inboxSnapshotMessages.find((item) => item.id === selectedId)
      || state.inboxMessages.find((item) => item.id === selectedId)
      || flags.saved[selectedId]
      || null;
  }, [flags.saved, selectedId, sourceMessages, state.inboxMessages, state.inboxSnapshotMessages]);

  const latestSelectedThreadMessageId = useMemo(() => {
    if (!selectedMessage) return "";
    const threadKey = selectedMessage.thread_id || selectedMessage.id;
    const candidates = [
      ...sourceMessages,
      ...state.inboxSnapshotMessages,
      ...state.inboxMessages,
      flags.saved[selectedId],
    ].filter((item): item is InboxMessage => Boolean(item));
    let latest = selectedMessage;
    for (const item of candidates) {
      if ((item.thread_id || item.id) !== threadKey) continue;
      const itemTime = Number(item.internal_date || 0);
      const latestTime = Number(latest.internal_date || 0);
      if (Number.isFinite(itemTime) && itemTime > latestTime) latest = item;
    }
    return latest.id || "";
  }, [flags.saved, selectedId, selectedMessage, sourceMessages, state.inboxMessages, state.inboxSnapshotMessages]);

  const closeDetailDrawer = useCallback(() => {
    const currentId = selectedId;
    setSelectedId("");
    requestAnimationFrame(() => {
      const row = document.querySelector<HTMLButtonElement>(`[data-mail-row-id="${CSS.escape(currentId)}"]`);
      row?.focus();
    });
  }, [selectedId]);

  const openMessageDetail = useCallback((message: InboxMessage) => {
    setSelectedId(message.id);
    if (!(message.unread || hasMessageLabel(message, "UNREAD")) || !message.thread_id) return;
    void actions.updateInboxThreadState(mailbox, message.thread_id, "mark_read").catch((reason) => {
      actions.showToast(reason instanceof Error ? reason.message : String(reason));
    });
  }, [actions, mailbox]);

  const handleGmailThreadAction = useCallback(async (operation: InboxThreadStateOperation, message: InboxMessage) => {
    if (!message.thread_id) return;
    const reverse: Record<InboxThreadStateOperation, InboxThreadStateOperation> = {
      mark_read: "mark_unread",
      mark_unread: "mark_read",
      star: "unstar",
      unstar: "star",
      mark_important: "mark_not_important",
      mark_not_important: "mark_important",
      trash: "untrash",
      untrash: "trash",
    };
    const notices: Record<InboxThreadStateOperation, string> = {
      mark_read: "Marked as read.",
      mark_unread: "Marked as unread.",
      star: "Thread starred.",
      unstar: "Stars removed.",
      mark_important: "Marked as important.",
      mark_not_important: "Marked as not important.",
      trash: "Moved to trash.",
      untrash: "Moved to inbox.",
    };
    try {
      await actions.updateInboxThreadState(mailbox, message.thread_id, operation);
      closeDetailDrawer();
      actions.showToast(notices[operation], {
        actionLabel: "Undo",
        onAction: () => {
          void actions.updateInboxThreadState(mailbox, message.thread_id || message.id, reverse[operation]).catch((reason) => {
            actions.showToast(reason instanceof Error ? reason.message : String(reason));
          });
        },
      });
    } catch (reason) {
      actions.showToast(reason instanceof Error ? reason.message : String(reason));
    }
  }, [actions, closeDetailDrawer, mailbox]);

  const handleTodoFromDetail = useCallback((message: InboxMessage) => {
    if (flags.todos.includes(message.id)) return;
    const previous = flags;
    const messages = messagesInThread(message);
    setWorkflowFlag("todos", messages, true);
    closeDetailDrawer();
    actions.showToast("Added to Todo.", {
      actionLabel: "Undo",
      onAction: () => restoreWorkflowFlags(messages, previous),
      secondaryActionLabel: "View",
      onSecondaryAction: () => {
        setMailboxView("todos");
        setFolderOpen(false);
        setSelectedId("");
      },
    });
  }, [actions, closeDetailDrawer, flags, messagesInThread, restoreWorkflowFlags, setWorkflowFlag]);

  const handleDoneFromDetail = useCallback((message: InboxMessage) => {
    const wasDone = flags.done.includes(message.id);
    const previous = flags;
    const messages = messagesInThread(message);
    setWorkflowFlag("done", messages, !wasDone);
    closeDetailDrawer();
    actions.showToast(wasDone ? "Moved to inbox." : "Marked as done.", {
      actionLabel: "Undo",
      onAction: () => restoreWorkflowFlags(messages, previous),
      secondaryActionLabel: "View",
      onSecondaryAction: () => {
        setMailboxView(wasDone ? "inbox" : "done");
        setFilter(isImportantMessage(message) ? "important" : "other");
        setFolderOpen(false);
        setSelectedId("");
      },
    });
  }, [actions, closeDetailDrawer, flags, messagesInThread, restoreWorkflowFlags, setWorkflowFlag]);

  const openSnoozePicker = useCallback((message: InboxMessage) => {
    if (mailboxView === "snoozed" || flags.snoozed.includes(message.id)) {
      const previous = flags;
      const messages = messagesInThread(message);
      setWorkflowFlag("snoozed", messages, false);
      actions.showToast("Snooze removed.", {
        actionLabel: "Undo",
        onAction: () => restoreWorkflowFlags(messages, previous),
      });
      return;
    }
    setSnoozeTarget(message);
  }, [actions, flags, mailboxView, messagesInThread, restoreWorkflowFlags, setWorkflowFlag]);

  const submitSnooze = useCallback((isoTime: string) => {
    const target = snoozeTarget;
    if (!target) return;
    const previous = flags;
    const messages = messagesInThread(target);
    setSnoozeTarget(null);
    setWorkflowFlag("snoozed", messages, true);
    if (selectedId === target.id) {
      closeDetailDrawer();
    }
    actions.showToast(`Snoozed until ${new Date(isoTime).toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    })}.`, {
      actionLabel: "Undo",
      onAction: () => restoreWorkflowFlags(messages, previous),
    });
  }, [actions, closeDetailDrawer, flags, messagesInThread, restoreWorkflowFlags, selectedId, setWorkflowFlag, snoozeTarget]);

  const selectMailboxView = (next: MailboxView) => {
    setMailboxView(next); setFolderOpen(false); setDays(7); setSelectedId("");
    setFilter("important");
  };

  const markTimelineDone = (messages: InboxMessage[]) => {
    setFlags((current) => {
      const done = [...new Set([...current.done, ...messages.map((message) => message.id)])];
      const saved = { ...current.saved };
      for (const message of messages) saved[message.id] = message;
      const ids = new Set(messages.map((message) => message.id));
      const next = {
        ...current,
        todos: current.todos.filter((id) => !ids.has(id)),
        snoozed: current.snoozed.filter((id) => !ids.has(id)),
        done,
        saved,
      };
      window.localStorage.setItem(flagsKey, JSON.stringify(next));
      return next;
    });
  };

  return (
    <div className={`inbox-home ${sidebarCollapsed ? "is-ai-collapsed" : ""} ${sidebarResizing ? "is-resizing-ai-sidebar" : ""}`} style={layoutStyle}>
      <AiSidebar collapsed={sidebarCollapsed} onToggle={() => setSidebarCollapsed((value) => !value)} onInsertArtifact={(artifact) => setInsertRequest({ nonce: crypto.randomUUID(), artifact })} />
      <div
        className="ai-sidebar-resizer"
        role="separator"
        aria-label="Resize Anna sidebar"
        aria-orientation="vertical"
        aria-valuemin={sidebarBounds.min}
        aria-valuemax={sidebarBounds.max}
        aria-valuenow={sidebarWidth}
        tabIndex={sidebarCollapsed ? -1 : 0}
        onPointerDown={startSidebarResize}
        onPointerMove={moveSidebarResize}
        onPointerUp={endSidebarResize}
        onPointerCancel={endSidebarResize}
        onKeyDown={adjustSidebarWithKeyboard}
      />
      <main className={`mail-workspace ${mailboxView === "inbox" ? "" : "is-folder-view"}`}>
        <header className="mail-topbar">
          <div className="mailbox-picker">
            <button className="mailbox-title" aria-expanded={folderOpen} onClick={() => setFolderOpen((open) => !open)}><span className={`mailbox-current-icon is-${mailboxView}`}><FolderIcon view={mailboxView} /></span><div><strong>{MAILBOX_VIEWS.find((item) => item.id === mailboxView)?.label}</strong><span>{mailbox || "Gmail"}</span></div><span className="mailbox-picker-chevron"><ChevronDownIcon /></span></button>
            {folderOpen ? <button className="mailbox-picker-backdrop" aria-label="Close folders" onClick={() => setFolderOpen(false)} /> : null}
            <div className={`mailbox-picker-menu ${folderOpen ? "is-open" : ""}`}>
              {MAILBOX_VIEWS.map((item) => <button key={item.id} className={mailboxView === item.id ? "is-active" : ""} onClick={() => selectMailboxView(item.id)}><span className={`folder-glyph is-${item.id}`}><FolderIcon view={item.id} /></span><span>{item.label}</span>{mailboxView === item.id ? <CheckIcon /> : null}</button>)}
            </div>
          </div>
          <label className="mail-search"><SearchIcon /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search in this catagory" /></label>
          <button className={`refresh-mail-btn ${state.inboxSnapshotLoading ? "is-syncing" : ""}`} disabled={state.inboxSnapshotLoading} onClick={() => void actions.refreshInboxEmails(days)}><RefreshIcon /><span>{state.inboxSnapshotLoading ? "Syncing" : "Refresh"}</span></button>
        </header>

        {mailboxView === "inbox" ? <nav className="mail-tabs" aria-label="Inbox filters">
          {([
            ["important", "Important"],
            ["other", "Other"],
          ] as Array<[FeedFilter, string]>).map(([key, label]) => (
            <button key={key} className={filter === key ? "is-active" : ""} onClick={() => setFilter(key)}>{label}<span>{counts[key]}</span></button>
          ))}
          <p>Last {days} days</p>
        </nav> : null}

        <section className="mail-feed" aria-live="polite">
          {state.inboxError && state.inboxMessages.length > 0 && !cachedInboxBannerDismissed ? (
            <div className="mail-sync-banner">
              <span>Showing cached inbox.</span>
              <div className="mail-sync-banner-actions">
                <button className="mail-sync-banner-skip" onClick={dismissCachedInboxBanner}>Skip</button>
                <button onClick={() => void actions.loadInboxEmails(mailboxView, days, true)}>Retry sync</button>
              </div>
            </div>
          ) : null}
          {!localCategory && state.inboxLoading && !state.inboxMessages.length ? (
            <div className="mail-loading">{[1, 2, 3, 4, 5, 6].map((item) => <span key={item} />)}</div>
          ) : !localCategory && state.inboxError && !state.inboxMessages.length ? (
            <div className="mail-empty"><InboxIcon /><h2>We couldn’t load Gmail</h2><p>{state.inboxError}</p><button onClick={() => void actions.loadInboxEmails(mailboxView, days, true)}>Try again</button></div>
          ) : !visible.length ? (
            mailboxView !== "inbox" && mailboxView !== "all"
              ? <div className="mail-empty is-category-empty"><SearchIcon /><h2>No matching results</h2></div>
              : <div className="mail-empty"><InboxIcon /><h2>No messages here</h2><p>{search ? "Try a different search." : `This filter is clear for the last ${days} days.`}</p></div>
          ) : grouped.map((group) => (
            <div className="mail-group" key={group.label}>
              <div className="mail-group-label"><span>{group.label}</span><i /><button title="Mark this timeline as done" onClick={() => markTimelineDone(group.messages)}><AllDoneIcon /></button></div>
              {group.messages.map((message) => {
                const avatarEmail = messageParticipant(message, mailboxView, mailbox).email.toLowerCase();
                return <InboxRow key={message.id} message={message} mailboxView={mailboxView} mailbox={mailbox} flags={flags} selected={selectedId === message.id} avatarUrl={contactAvatars[avatarEmail]} onPrefetch={() => void prefetchMessageBody(message)} onFlag={updateFlag} onSnooze={openSnoozePicker} onSelect={() => openMessageDetail(message)} />;
              })}
            </div>
          ))}
          {days === 7 && !state.inboxLoading && state.inboxMessages.length > 0 && (mailboxView === "inbox" || mailboxView === "all") ? <button className="older-mail-btn" onClick={() => { setDays(30); void actions.loadInboxEmails(mailboxView, 30); }}>Show emails older than 7 days</button> : null}
        </section>
        <MailDetailDrawer
          key={`${mailbox}:${selectedMessage?.id || "closed"}`}
          open={Boolean(selectedMessage)}
          mailbox={mailbox}
          message={selectedMessage}
          flags={flags}
          aiBusy={state.aiChatLoading}
          insertRequest={insertRequest}
          onConsumeInsertRequest={(nonce) => setInsertRequest((current) => current?.nonce === nonce ? null : current)}
          onClose={closeDetailDrawer}
          onTodoMessage={handleTodoFromDetail}
          onDoneMessage={handleDoneFromDetail}
          onSnoozeMessage={openSnoozePicker}
          onThreadAction={handleGmailThreadAction}
          showToast={actions.showToast}
          loadInboxEmailBody={actions.loadInboxEmailBody}
          loadInboxThreadPage={actions.loadInboxThreadPage}
          loadInboxMessageDisplayBody={actions.loadInboxMessageDisplayBody}
          loadInboxThreadAssist={actions.loadInboxThreadAssist}
          getInboxThreadDraft={actions.getInboxThreadDraft}
          saveInboxThreadDraft={actions.saveInboxThreadDraft}
          deleteInboxThreadDraft={actions.deleteInboxThreadDraft}
          prepareInboxAttachmentAccess={actions.prepareInboxAttachmentAccess}
          submitMailContextPrompt={actions.submitMailContextPrompt}
          sendInboxThreadReply={actions.sendInboxThreadReply}
          contactAvatars={contactAvatars}
          loadContactAvatars={actions.loadContactAvatars}
          latestThreadMessageId={latestSelectedThreadMessageId}
        />
        <SnoozePicker
          open={Boolean(snoozeTarget)}
          onClose={() => setSnoozeTarget(null)}
          onSubmit={submitSnooze}
        />
      </main>
      <AccountRail />
    </div>
  );
}
