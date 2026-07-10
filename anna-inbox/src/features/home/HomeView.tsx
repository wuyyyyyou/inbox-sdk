import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type UIEvent as ReactUIEvent,
} from "react";
import { useApp } from "../../app/AppContext";
import type {
  AiChatMessage,
  AiMailContextRef,
  AskMailLink,
  DraftReplyArtifact,
  CustomRunResult,
  InboxMessage,
  InboxThreadStateOperation,
} from "../../types/mail";
import { SnoozePicker } from "./SnoozePicker";
import { MailDetailDrawer } from "../mail-detail/MailDetailDrawer";
import { sortInboxMessagesDesc } from "./inboxMessageOrder";
import {
  parseAiMessageInline,
  parseAiMessageMarkdown,
  type AiMessageInline,
} from "./aiMessageFormatting";
import {
  getCachedMessageBody,
  getContactAvatarCache,
  getMailFlags,
  setCachedMessageBody,
  setContactAvatarCache,
  setMailFlags,
} from "../../shared/browserStorage";
import {
  mailAvatarFallback,
  senderParts as parseMailSenderParts,
  splitAddresses as splitMailAddresses,
} from "../../shared/mailIdentity";

type FeedFilter = "important" | "other";
type MailboxView =
  | "inbox"
  | "todos"
  | "starred"
  | "snoozed"
  | "done"
  | "drafts"
  | "sent"
  | "trash"
  | "spam"
  | "all";
type MailUiFlags = {
  todos: string[];
  snoozed: string[];
  snoozedUntil?: Record<string, string>;
  done: string[];
  doneRemoved: string[];
  drafts: string[];
  saved: Record<string, InboxMessage>;
};
type CategoryFlag = "todos" | "snoozed" | "done" | "drafts";
type InboxFeedWindow = {
  days: number;
  nextOffset: number;
  hasMore: boolean;
  localLimit: number;
  source: "cache" | "gmail";
  gmailPageToken: string;
  gmailPageOffset: number;
};
type FeedActionState = "refresh" | "older" | "more" | "category-page" | null;

const AI_SIDEBAR_WIDTH_KEY = "anna-inbox:ai-sidebar-width";
const AI_SIDEBAR_MIN_WIDTH = 300;
const AI_SIDEBAR_MAX_WIDTH = 680;
const CACHED_INBOX_BANNER_SKIP_KEY = "anna-inbox:cached-inbox-banner-skip";
const INBOX_ALL_TIME_DAYS = 0;
const INBOX_LAST_MONTH_DAYS = 30;
const DEFAULT_INBOX_FEED_WINDOW: InboxFeedWindow = {
  days: 7,
  nextOffset: 100,
  hasMore: false,
  localLimit: 100,
  source: "cache",
  gmailPageToken: "",
  gmailPageOffset: 0,
};
const INBOX_FEED_PAGE_SIZE = 100;
const AI_CONVERSATION_BOTTOM_THRESHOLD = 24;
const DETAIL_DRAWER_TRANSITION_MS = 360;

export function gmailAuthorizationError(source?: string) {
  return `No authorized mailbox detected (source: ${source || "unknown"}).`;
}

export function isGmailAuthorizationRequired(status: {
  checked: boolean;
  authorized: boolean;
  source?: string;
}) {
  return status.checked && !status.authorized && status.source !== "error";
}

export function isAiConversationNearBottom(
  scroller: Pick<HTMLElement, "scrollHeight" | "scrollTop" | "clientHeight">,
) {
  return (
    scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <=
    AI_CONVERSATION_BOTTOM_THRESHOLD
  );
}

const MAILBOX_VIEWS: Array<{ id: MailboxView; label: string }> = [
  { id: "inbox", label: "Inbox" },
  { id: "todos", label: "Todos" },
  { id: "starred", label: "Starred" },
  { id: "snoozed", label: "Snoozed" },
  { id: "done", label: "Done" },
  { id: "drafts", label: "Drafts" },
  { id: "sent", label: "Sent" },
  { id: "trash", label: "Trash" },
  { id: "spam", label: "Spam" },
  { id: "all", label: "All mail" },
];

function isLocalMailboxView(
  view: MailboxView,
): view is "todos" | "snoozed" | "done" | "drafts" {
  return view === "todos" || view === "snoozed" || view === "done" || view === "drafts";
}

function scheduleDeferredWork(task: () => void, delayMs = 180) {
  const win = window as Window & {
    requestIdleCallback?: (
      callback: IdleRequestCallback,
      options?: IdleRequestOptions,
    ) => number;
    cancelIdleCallback?: (handle: number) => void;
  };
  if (
    typeof window !== "undefined" &&
    typeof win.requestIdleCallback === "function" &&
    typeof win.cancelIdleCallback === "function"
  ) {
    const idleId = win.requestIdleCallback(() => task(), {
      timeout: delayMs + 400,
    });
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
  if (typeof window === "undefined")
    return { min: AI_SIDEBAR_MIN_WIDTH, max: AI_SIDEBAR_MAX_WIDTH };
  const compactMin = window.innerWidth < 520 ? 180 : AI_SIDEBAR_MIN_WIDTH;
  const max = Math.max(
    compactMin,
    Math.min(AI_SIDEBAR_MAX_WIDTH, window.innerWidth - 180),
  );
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

export function accountDisplayName(account: {
  email: string;
  display_name?: string;
}) {
  const displayName = String(account.display_name || "").trim();
  if (displayName) return displayName;
  return account.email.split("@")[0] || account.email;
}

function Icon({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

const SearchIcon = () => (
  <Icon>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-4-4" />
  </Icon>
);
const InboxIcon = () => (
  <Icon>
    <path d="M4 5h16l-1.5 14h-13L4 5Z" />
    <path d="M5 14h4l1.5 2h3l1.5-2h4" />
  </Icon>
);
const SparkleIcon = () => (
  <Icon>
    <path d="M12 3c.6 4.5 2.8 6.7 7.2 7.2-4.4.5-6.6 2.7-7.2 7.2-.6-4.5-2.8-6.7-7.2-7.2C9.2 9.7 11.4 7.5 12 3Z" />
    <path d="M19 3v4M17 5h4" />
  </Icon>
);
const SendIcon = () => (
  <Icon>
    <path d="m4 4 16 8-16 8 3-8-3-8Z" />
    <path d="M7 12h13" />
  </Icon>
);
const StopIcon = () => (
  <Icon>
    <rect
      x="7"
      y="7"
      width="10"
      height="10"
      rx="1.5"
      fill="currentColor"
      stroke="none"
    />
  </Icon>
);
const RefreshIcon = () => (
  <Icon>
    <path d="M20 6v5h-5" />
    <path d="M19 11a7.5 7.5 0 1 0 .2 5" />
  </Icon>
);
const HistoryIcon = () => (
  <Icon>
    <path d="M4 5v5h5" />
    <path d="M5 10a8 8 0 1 1 1.4 6.4" />
    <path d="M12 8v5l3 2" />
  </Icon>
);
const ChevronLeftIcon = () => (
  <Icon>
    <path d="m15 18-6-6 6-6" />
  </Icon>
);
const ChevronDownIcon = () => (
  <Icon>
    <path d="m7 9 5 5 5-5" />
  </Icon>
);
const CheckIcon = () => (
  <Icon>
    <path d="m5 12 4 4L19 6" />
  </Icon>
);
const PaperclipIcon = () => (
  <Icon>
    <path d="m9 12 5.7-5.7a3 3 0 0 1 4.2 4.2l-7.8 7.8a5 5 0 0 1-7.1-7.1l7.4-7.4" />
  </Icon>
);
const MailOpenIcon = () => (
  <Icon>
    <path d="M3 8.5 12 14l9-5.5" />
    <path d="M5 6h14a2 2 0 0 1 2 2v10H3V8a2 2 0 0 1 2-2Z" />
  </Icon>
);
const StarIcon = () => (
  <Icon>
    <path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3Z" />
  </Icon>
);
const ClockIcon = () => (
  <Icon>
    <circle cx="12" cy="12" r="8" />
    <path d="M12 7v5l3 2" />
  </Icon>
);
const TrashIcon = () => (
  <Icon>
    <path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13" />
  </Icon>
);
const TrashOffIcon = () => (
  <Icon>
    <path d="M9 7V4h6v3M7.5 7H19M7 10l1 10h8l.6-6M4 4l16 16" />
  </Icon>
);
const TodoIcon = () => (
  <Icon>
    <rect x="4" y="4" width="16" height="16" rx="4" />
    <path d="m8 12 2.5 2.5L16 9" />
  </Icon>
);
const DraftIcon = () => (
  <Icon>
    <path d="M6 3h9l4 4v14H6z" />
    <path d="M14 3v5h5M9 12h6M9 16h6" />
  </Icon>
);
const SentIcon = () => (
  <Icon>
    <path d="m3 4 18 8-18 8 4-8-4-8Z" />
    <path d="M7 12h14" />
  </Icon>
);
const SpamIcon = () => (
  <Icon>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v6M12 17h.01" />
  </Icon>
);
const AllMailIcon = () => (
  <Icon>
    <rect x="3" y="5" width="18" height="14" rx="3" />
    <path d="m4 8 8 6 8-6" />
  </Icon>
);
const AllDoneIcon = () => (
  <Icon>
    <path d="m4 12 3 3 5-6M11 14l2 2 7-8" />
  </Icon>
);
const ImportantIcon = () => (
  <Icon>
    <path d="M5 6h10l4 6-4 6H5l4-6-4-6Z" />
  </Icon>
);
const PersonIcon = () => (
  <Icon>
    <path d="M15 19v-1a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v1" />
    <circle cx="10" cy="7" r="3" />
  </Icon>
);

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
  return parseMailSenderParts(value);
}

function splitAddresses(value: unknown): string[] {
  return splitMailAddresses(value);
}

export function messageParticipant(
  message: InboxMessage,
  mailboxView: MailboxView,
  mailbox: string,
) {
  const labels = new Set(
    (message.label_ids || []).map((label) => label.toUpperCase()),
  );
  const sender = senderParts(message.from);
  const normalizedMailbox = mailbox.trim().toLowerCase();
  const draft = isDraftMessage(message);
  const outgoing =
    mailboxView === "sent" ||
    labels.has("SENT") ||
    draft ||
    Boolean(
      normalizedMailbox && sender.email.toLowerCase() === normalizedMailbox,
    );
  const recipientSource = draft
    && normalizedMailbox
    && sender.email.toLowerCase() !== normalizedMailbox
    ? message.from
    : message.to;
  const recipients = splitAddresses(recipientSource).map(senderParts);
  const outgoingParticipant = () => {
    const names = [
      "me",
      ...recipients
        .filter(
          (item) =>
            !normalizedMailbox ||
            item.email.toLowerCase() !== normalizedMailbox,
        )
        .map((item) => item.name),
    ].filter(
      (name, index, all) => Boolean(name) && all.indexOf(name) === index,
    );
    const name = names.join(", ") || "me";
    return {
      name,
      title: String(recipientSource || message.from || "Draft without recipients"),
      initial: recipients[0]?.name || "me",
      email: recipients[0]?.email || normalizedMailbox,
      outgoing,
    };
  };

  if (draft) {
    return outgoingParticipant();
  }

  if (outgoing) {
    return outgoingParticipant();
  }

  const fallbackName = draft ? "Draft" : "No sender";
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
  return (
    name.match(/[A-Za-z0-9]/)?.[0] ||
    name.slice(0, 1) ||
    "?"
  ).toUpperCase();
}

function messageDate(message: InboxMessage) {
  const milliseconds = Number(message.internal_date || 0);
  const date = milliseconds
    ? new Date(milliseconds)
    : new Date(message.date || "");
  return Number.isNaN(date.getTime()) ? null : date;
}

function dateLabel(message: InboxMessage) {
  const date = messageDate(message);
  if (!date) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString())
    return date.toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
    });
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function snoozeUntilLabel(value: string | undefined) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const target = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const days = Math.round((target.getTime() - today.getTime()) / (24 * 60 * 60 * 1000));
  const dayLabel = days === 0
    ? "Today"
    : days === 1
      ? "Tomorrow"
      : date.toLocaleDateString("en-US", days > 1 && days < 7 ? { weekday: "short" } : { month: "short", day: "numeric" });
  const timeLabel = `${date.getHours()}:${String(date.getMinutes()).padStart(2, "0")}`;
  return `${dayLabel} ${timeLabel}`;
}

function groupLabel(message: InboxMessage) {
  const date = messageDate(message);
  if (!date) return "LAST 7 DAYS";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return "TODAY";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return "YESTERDAY";
  const daysAgo = Math.floor(
    (now.getTime() - date.getTime()) / (24 * 60 * 60 * 1000),
  );
  if (daysAgo < 7) return "LAST 7 DAYS";
  const currentMonthLabel = date
    .toLocaleDateString("en-US", { month: "long" })
    .toUpperCase();
  // if (date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth()) {
  //   return `EARLIER IN ${currentMonthLabel}`;
  // }
  return `EARLIER IN ${currentMonthLabel}`;
}

function inboxRangeLabel(days: number) {
  if (days === INBOX_ALL_TIME_DAYS) return "All time";
  if (days === INBOX_LAST_MONTH_DAYS) return "Last 1 month";
  return `Last ${days} days`;
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

export function isTrashMessage(message: InboxMessage) {
  return hasMessageLabel(message, "TRASH");
}

export function gmailTrashUrl(mailbox: string) {
  return `https://mail.google.com/mail/?authuser=${encodeURIComponent(mailbox.trim())}#trash`;
}

async function copyTextToClipboard(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("Clipboard unavailable");
}

export function isDraftMessage(message: InboxMessage) {
  return Boolean(message.draft_local || message.draft_body);
}

export function shouldShowImportantIcon(
  important: boolean,
  outgoing: boolean,
  draft: boolean,
) {
  return important && (!outgoing || draft);
}

export function isSentMessage(message: InboxMessage) {
  if ((message.label_ids || []).some((label) => label.toUpperCase() === "SENT"))
    return true;
  const sender = senderParts(message.from);
  const normalizedMailbox = String(message.mailbox || "")
    .trim()
    .toLowerCase();
  return Boolean(
    normalizedMailbox &&
    sender.email.trim().toLowerCase() === normalizedMailbox,
  );
}

function inboxThreadProjectionKey(message: InboxMessage) {
  const mailboxKey = String(message.mailbox || "").trim().toLowerCase();
  const threadKey = String(message.thread_id || "").trim();
  if (threadKey) return `${mailboxKey}:thread:${threadKey}`;
  return `${mailboxKey}:message:${message.id}`;
}

function uniqueLatestInboxThreads<T extends InboxMessage>(messages: T[]) {
  const seen = new Set<string>();
  const unique: T[] = [];
  for (const message of sortInboxMessagesDesc(messages)) {
    const key = inboxThreadProjectionKey(message);
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(message);
  }
  return unique;
}

export function mergeDraftOverlayMessages(
  messages: InboxMessage[],
  drafts: InboxMessage[],
) {
  const draftsByThread = new Map(
    drafts.map((draft) => [draft.thread_id || draft.id, draft]),
  );
  const merged = messages.map((message) => {
    const threadKey = message.thread_id || message.id;
    const draft = draftsByThread.get(threadKey);
    if (!draft) return message;
    draftsByThread.delete(threadKey);
    return {
      ...message,
      // Draft metadata may describe a different message in the same thread.
      // Preserve the inbox message identity so sender/recipient and sent-state
      // continue to reflect the selected thread message.
      draft_body: draft.draft_body,
      draft_local: true,
      body_preview: draft.draft_body || draft.body_preview || message.body_preview,
      snippet: draft.draft_body || draft.snippet || message.snippet,
      label_ids: [...new Set([...(message.label_ids || []), "DRAFT"])],
    };
  });
  return [...merged, ...draftsByThread.values()];
}

export function isDoneMessage(
  message: InboxMessage,
  flags: Pick<MailUiFlags, "done" | "doneRemoved">,
) {
  if (flags.doneRemoved.includes(message.id)) return false;
  return flags.done.includes(message.id) || isSentMessage(message);
}

export function resolveSourceMessages(
  mailboxView: MailboxView,
  inboxMessages: InboxMessage[],
  inboxSnapshotMessages: InboxMessage[],
  flags: MailUiFlags,
) {
  const source = inboxSnapshotMessages.length
    ? inboxSnapshotMessages
    : inboxMessages;
  const currentMessages = new Map<string, InboxMessage>();
  for (const message of [
    ...Object.values(flags.saved),
    ...inboxSnapshotMessages,
    ...inboxMessages,
  ]) {
    currentMessages.set(message.id, message);
  }
  if (mailboxView === "todos" || mailboxView === "snoozed") {
    return uniqueLatestInboxThreads(
      flags[mailboxView]
        .map((id) => currentMessages.get(id))
        .filter((message): message is InboxMessage => message !== undefined && !isTrashMessage(message)),
    );
  }
  if (mailboxView === "done") {
    const doneIds = new Set(flags.done);
    for (const message of currentMessages.values()) {
      if (isDoneMessage(message, flags)) doneIds.add(message.id);
    }
    return uniqueLatestInboxThreads(
      [...doneIds]
        .map((id) => currentMessages.get(id))
        .filter((message): message is InboxMessage => message !== undefined && !isTrashMessage(message)),
    );
  }
  if (mailboxView === "inbox") {
    return uniqueLatestInboxThreads(
      inboxMessages.filter((message) => hasMessageLabel(message, "INBOX") && !isTrashMessage(message)),
    );
  }
  if (mailboxView === "starred")
    return uniqueLatestInboxThreads(
      source.filter((message) => hasMessageLabel(message, "STARRED") && !isTrashMessage(message)),
    );
  if (mailboxView === "drafts")
    return uniqueLatestInboxThreads(
      source.filter((message) => isDraftMessage(message) && !isTrashMessage(message)),
    );
  if (mailboxView === "sent")
    return uniqueLatestInboxThreads(
      source.filter((message) => hasMessageLabel(message, "SENT") && !isTrashMessage(message)),
    );
  if (mailboxView === "trash")
    return uniqueLatestInboxThreads(
      source.filter(
        (message) =>
          isTrashMessage(message) &&
          !hasMessageLabel(message, "DRAFT") &&
          !isDraftMessage(message),
      ),
    );
  if (mailboxView === "spam")
    return uniqueLatestInboxThreads(
      source.filter((message) => hasMessageLabel(message, "SPAM") && !isTrashMessage(message)),
    );
  if (mailboxView === "all")
    return uniqueLatestInboxThreads(
      source.filter(
        (message) =>
          !["TRASH", "SPAM", "CHAT"].some((label) =>
            hasMessageLabel(message, label),
          ),
      ),
    );
  return uniqueLatestInboxThreads(source);
}

function hasInboxMessageAttachment(message: InboxMessage) {
  const attachments = (message as InboxMessage & { attachments?: unknown[] }).attachments;
  return Boolean(
    message.has_attachment
    || Number(message.attachment_count || 0) > 0
    || (Array.isArray(attachments) && attachments.length > 0)
  );
}

function InboxRow({
  message,
  selected,
  flags,
  mailboxView,
  mailbox,
  onSelect,
  onFlag,
  onThreadAction,
  onSnooze,
  onPrefetch,
  avatarUrl,
}: {
  message: InboxMessage;
  selected: boolean;
  flags: MailUiFlags;
  mailboxView: MailboxView;
  mailbox: string;
  onSelect: () => void;
  onFlag: (kind: CategoryFlag, message: InboxMessage) => void;
  onThreadAction: (operation: InboxThreadStateOperation, message: InboxMessage) => void;
  onSnooze: (message: InboxMessage) => void;
  onPrefetch: () => void;
  avatarUrl?: string;
}) {
  const { actions } = useApp();
  const [avatarFailed, setAvatarFailed] = useState(false);
  const participant = messageParticipant(message, mailboxView, mailbox);
  const fallbackAvatar = mailAvatarFallback(participant.name || participant.email || participant.initial, participant.name || participant.initial);
  const sentView = participant.outgoing;
  const sentMessage = isSentMessage(message);
  const isDone = isDoneMessage(message, flags);
  const isTodo = flags.todos.includes(message.id);
  const isSnoozed = flags.snoozed.includes(message.id);
  const trashed = isTrashMessage(message);
  const snoozeLabel = !trashed && mailboxView === "snoozed" ? snoozeUntilLabel(flags.snoozedUntil?.[message.id]) : "";
  const important = isImportantMessage(message);
  const starred = isStarredMessage(message);
  const draft = isDraftMessage(message);
  const preview =
    (draft ? message.draft_body : "") || message.snippet || message.body_preview || "No preview available";
  return (
    <article
      className={`mail-row ${mailboxView === "all" ? "is-all-mail" : ""} ${message.unread ? "is-unread" : ""} ${selected ? "is-selected" : ""}`}
    >
      <button
        className={`mail-row-main ${snoozeLabel ? "has-snooze-time" : ""}`}
        data-mail-row-id={message.id}
        onMouseEnter={onPrefetch}
        onFocus={onPrefetch}
        onClick={onSelect}
        aria-expanded={selected}
      >
        {avatarUrl && !avatarFailed ? (
          <img
            className="sender-avatar is-photo"
            src={avatarUrl}
            alt=""
            referrerPolicy="no-referrer"
            onError={() => setAvatarFailed(true)}
          />
        ) : (
          <span
            className={`sender-avatar tone-${fallbackAvatar.tone}`}
          >
            {fallbackAvatar.initial}
          </span>
        )}
        <span className="mail-sender" title={participant.title}>
          {participant.name}
        </span>
        <span className="mail-content">
          <strong>{message.subject || "(no subject)"}</strong>
          <span>
            —{" "}
            {draft && !trashed ? (
              <>
                <b className="mail-draft-label">Draft:</b> {preview}
              </>
            ) : (
              preview
            )}
          </span>
        </span>
        <span className="mail-flags">
          {trashed ? (
            <span className="mail-trash-icon" title="Trash">
              <TrashIcon />
            </span>
          ) : null}
          {!trashed && shouldShowImportantIcon(important, sentView, draft) ? (
            <span className="mail-important-icon" title="Important">
              <ImportantIcon />
            </span>
          ) : null}
          {!trashed && draft ? (
            <span className="mail-draft-icon" title="Draft">
              <DraftIcon />
            </span>
          ) : !trashed && sentMessage ? (
            <span className="mail-sent-badge" title="Sent and done">
              <SentIcon />
              <span className="mail-sent-check">
                <CheckIcon />
              </span>
            </span>
          ) : !trashed && isDone ? (
            <span className="mail-sent-check" title="Done">
              <CheckIcon />
            </span>
          ) : null}
          {!trashed && starred ? (
            <span className="mail-starred" title="Starred">
              <StarIcon />
            </span>
          ) : null}
          {hasInboxMessageAttachment(message) ? (
            <span
              className="mail-attachment"
              title={`${message.attachment_count || 1} attachment(s)`}
            >
              <PaperclipIcon />
            </span>
          ) : null}
        </span>
        {snoozeLabel ? (
          <span className="mail-snooze-until" title={`Snoozed until ${snoozeLabel}`}>
            <ClockIcon />
            <span>{snoozeLabel}</span>
          </span>
        ) : (
          <time>{dateLabel(message)}</time>
        )}
      </button>
      <span className="mail-row-actions">
        {trashed ? (
          <button
            className="is-trashed"
            aria-label="Remove from trash"
            data-tooltip="Remove from trash"
            onClick={() => onThreadAction("untrash", message)}
          >
            <TrashOffIcon />
          </button>
        ) : (
          <>
            <button
              className={starred ? "is-active is-starred" : ""}
              aria-label={starred ? "Unstar" : "Star"}
              data-tooltip={starred ? "Unstar" : "Star"}
              onClick={() => void actions.setInboxStarred(message.id, !starred)}
            >
              <StarIcon />
            </button>
            <button
              className={important ? "is-active is-important" : ""}
              aria-label={important ? "Mark not important" : "Mark important"}
              data-tooltip={important ? "Mark not important" : "Mark important"}
              onClick={() => onThreadAction(important ? "mark_not_important" : "mark_important", message)}
            >
              <ImportantIcon />
            </button>
            <button
              className={isTodo ? "is-active is-todo" : ""}
              aria-label={isTodo ? "Click Done to remove" : "Add to Todo"}
              data-tooltip={isTodo ? "Click Done to remove" : "Add to Todo"}
              disabled={isTodo}
              onClick={() => onFlag("todos", message)}
            >
              <TodoIcon />
            </button>
            <button
              className={isSnoozed ? "is-active is-snoozed" : ""}
              aria-label={isSnoozed ? "Remove from snoozed" : "Snooze"}
              data-tooltip={isSnoozed ? "Remove from snoozed" : "Snooze"}
              onClick={() => onSnooze(message)}
            >
              <ClockIcon />
            </button>
            {message.unread ? (
              <button
                aria-label="Mark as read"
                data-tooltip="Mark as read"
                onClick={() => void actions.markInboxRead(message.id)}
              >
                <MailOpenIcon />
              </button>
            ) : null}
            <button
              aria-label="Move to trash"
              data-tooltip="Move to trash"
              onClick={() => void actions.trashInboxMessage(message.id)}
            >
              <TrashIcon />
            </button>
            <button
              className={isDone ? "is-active is-done" : ""}
              aria-label={
                sentMessage ? "Sent and done" : isDone ? "Move to inbox" : "Done"
              }
              data-tooltip={
                sentMessage ? "Sent and done" : isDone ? "Move to inbox" : "Done"
              }
              disabled={sentMessage}
              onClick={() => onFlag("done", message)}
            >
              <CheckIcon />
            </button>
          </>
        )}
      </span>
    </article>
  );
}

function aiResultCount(result: CustomRunResult) {
  return (result.sections || []).reduce(
    (total, section) => total + (section.items?.length || 0),
    0,
  );
}

export function aiSearchStatus(result: CustomRunResult) {
  const itemCount = aiResultCount(result);
  const chinese = /[\u3400-\u9fff]/.test([
    result.title,
    result.summary,
    ...(result.sections || []).flatMap((section) => [section.heading, section.body]),
  ].filter(Boolean).join(" "));
  if (itemCount > 0)
    return chinese
      ? `找到 ${itemCount} 个相关邮件线程。`
      : `Found ${itemCount} relevant thread${itemCount === 1 ? "" : "s"}.`;
  const queryCount = result.plan_gmail_queries?.length || 0;
  if (queryCount > 0)
    return chinese
      ? `已执行 ${queryCount} 个针对性收件箱查询。`
      : `Searched ${queryCount} focused inbox quer${queryCount === 1 ? "y" : "ies"}.`;
  return chinese ? "已完成收件箱搜索。" : "Finished searching your inbox.";
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
  if (
    message.kind === "error" &&
    (text.includes("[tool_failed]") || text.includes("executa process exited"))
  ) {
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

function AiMessageInlineContent({
  content,
  onOpenThread,
}: {
  content: AiMessageInline[];
  onOpenThread?: (threadId: string) => void;
}) {
  return content.map((node, index) => {
    const key = `${node.type}-${index}`;
    if (node.type === "bold") return <strong key={key}>{node.value}</strong>;
    if (node.type === "link") {
      return (
        <a key={key} href={node.href} target="_blank" rel="noreferrer noopener">
          <AiMessageInlineContent content={parseAiMessageInline(node.label)} onOpenThread={onOpenThread} />
        </a>
      );
    }
    if (node.type === "thread_ref") {
      return (
        <button
          key={key}
          type="button"
          className="ai-thread-reference"
          onClick={() => onOpenThread?.(node.threadId)}
        >
          Open email thread
        </button>
      );
    }
    return node.value;
  });
}

function RichAssistantText({
  text,
  onOpenThread,
}: {
  text: string;
  onOpenThread?: (threadId: string) => void;
}) {
  return (
    <div className="ai-message-rich-text">
      {parseAiMessageMarkdown(text).map((block, index) => {
        const key = `${block.type}-${index}`;
        if (block.type === "heading") {
          const Heading = (`h${block.level + 2}` as "h3" | "h4" | "h5");
          return <Heading key={key}><AiMessageInlineContent content={block.content} onOpenThread={onOpenThread} /></Heading>;
        }
        if (block.type === "unordered_list") {
          return <ul key={key}>{block.items.map((item, itemIndex) => <li key={itemIndex}><AiMessageInlineContent content={item} onOpenThread={onOpenThread} /></li>)}</ul>;
        }
        if (block.type === "ordered_list") {
          return <ol key={key}>{block.items.map((item, itemIndex) => <li key={itemIndex}><AiMessageInlineContent content={item} onOpenThread={onOpenThread} /></li>)}</ol>;
        }
        return <p key={key}><AiMessageInlineContent content={block.content} onOpenThread={onOpenThread} /></p>;
      })}
    </div>
  );
}

function AnimatedAssistantText({
  text,
  animate,
  onComplete,
  onOpenThread,
}: {
  text: string;
  animate: boolean;
  onComplete?: () => void;
  onOpenThread?: (threadId: string) => void;
}) {
  const [visibleText, setVisibleText] = useState(animate ? "" : text);
  const onCompleteRef = useRef(onComplete);

  useEffect(() => {
    onCompleteRef.current = onComplete;
  }, [onComplete]);

  useEffect(() => {
    if (!animate) {
      setVisibleText(text);
      onCompleteRef.current?.();
      return;
    }
    if (!text) {
      setVisibleText("");
      onCompleteRef.current?.();
      return;
    }
    setVisibleText("");
    let frame = 0;
    let timer = window.setInterval(() => {
      frame += Math.max(1, Math.ceil(text.length / 36));
      if (frame >= text.length) {
        window.clearInterval(timer);
        setVisibleText(text);
        onCompleteRef.current?.();
        return;
      }
      setVisibleText(text.slice(0, frame));
    }, 24);
    return () => window.clearInterval(timer);
  }, [animate, text]);

  return <RichAssistantText text={visibleText} onOpenThread={onOpenThread} />;
}

function AiAssistantMessage({
  message,
  currentMailContext,
  onUseArtifact,
  onOpenMail,
}: {
  message: AiChatMessage;
  currentMailContext: AiMailContextRef | null;
  onUseArtifact: (artifact: DraftReplyArtifact, mode: "append" | "replace") => void;
  onOpenMail: (target: AskMailLink) => void;
}) {
  const { state, actions } = useApp();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submittingGap, setSubmittingGap] = useState(false);
  const [clarificationInput, setClarificationInput] = useState("");
  const [assistantTextComplete, setAssistantTextComplete] = useState(
    () => !shouldAnimateAssistantText(message.timestamp),
  );
  const openThreadReference = (threadId: string) => {
    const context = message.mailContext || currentMailContext;
    const referenceMailbox = context?.mailbox || state.mailbox;
    if (!referenceMailbox) {
      actions.showToast("This email reference is unavailable.");
      return;
    }
    onOpenMail({
      label: "Referenced thread",
      mailbox: referenceMailbox,
      thread_id: threadId,
      message_id: "",
    });
  };

  if (message.pending) {
    return (
      <div
        className="ai-message is-assistant is-thinking-inline"
        aria-live="polite"
        aria-busy="true"
      >
        <p>{message.content || "Thinking"}</p>
      </div>
    );
  }

  const result = message.result;
  if (!result) {
    const text = displayAssistantText(message);
    const animate = shouldAnimateAssistantText(message.timestamp);
    const draftArtifact =
      message.artifact && (!animate || assistantTextComplete)
        ? message.artifact
        : null;
    const summaryLink = message.mailSummaryLink;
    const clarification = message.clarification;
    const targetThreadOpen = Boolean(
      draftArtifact &&
      currentMailContext &&
      currentMailContext.mailbox.trim().toLowerCase() === draftArtifact.mailbox.trim().toLowerCase() &&
      currentMailContext.thread_id === draftArtifact.thread_id,
    );
    const submitReplyGap = async () => {
      if (
        !message.mailContext ||
        !message.sourcePrompt ||
        !message.replyGaps?.needs_user_input
      )
        return;
      const missingRequired = message.replyGaps.questions.filter(
        (question) =>
          question.required !== false &&
          !String(answers[question.id] || "").trim(),
      );
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
      <div
        className={`ai-message is-assistant ${message.kind === "error" ? "is-error" : ""} ${message.kind === "stopped" ? "is-stopped" : ""}`}
      >
        {summaryLink ? (
          <div className="ai-mail-summary-title">
            <span>Here's a summary of</span>
            <button
              type="button"
              title={summaryLink.label}
              onClick={() => onOpenMail(summaryLink)}
            >
              {summaryLink.label}
            </button>
          </div>
        ) : null}
        <AnimatedAssistantText
          text={text}
          animate={animate}
          onComplete={() => setAssistantTextComplete(true)}
          onOpenThread={openThreadReference}
        />
        {clarification ? (
          <div className={`ai-clarification is-${clarification.status}`}>
            {clarification.status === "pending" ? (
              <>
                {clarification.freeform_enabled ? (
                  <input
                    value={clarificationInput}
                    placeholder="Add details (optional)"
                    onChange={(event) => setClarificationInput(event.target.value)}
                  />
                ) : null}
                <div className="ai-clarification-actions">
                  {clarification.actions.map((action) => (
                    <button
                      key={action.id}
                      onClick={() => void actions.sendAiChatMessage({
                        currentMailContext,
                        forcedKind: action.id,
                        prompt: clarificationInput.trim() || clarification.original_input,
                        clarificationMessageId: message.id,
                      })}
                    >
                      {action.label}
                    </button>
                  ))}
                  <button className="is-dismiss" onClick={() => actions.dismissAiClarification(message.id)}>
                    Cancel
                  </button>
                </div>
              </>
            ) : (
              <span>{clarification.status === "dismissed" ? "Dismissed" : "Resolved"}</span>
            )}
          </div>
        ) : null}
        {draftArtifact ? (
          <div className="ai-draft-artifact">
            <pre>{draftArtifact.body}</pre>
            <div className="ai-draft-artifact-actions">
              {targetThreadOpen ? (
                <>
                  <button className="is-primary" onClick={() => onUseArtifact(draftArtifact, "append")}>
                    Append to draft reply
                  </button>
                  <button className="is-secondary" onClick={() => onUseArtifact(draftArtifact, "replace")}>
                    Replace draft reply
                  </button>
                </>
              ) : (
                <button
                  className="is-primary"
                  onClick={() => onOpenMail({
                    label: "Draft thread",
                    mailbox: draftArtifact.mailbox,
                    thread_id: draftArtifact.thread_id,
                    message_id: message.mailContext?.latest_message_id || message.mailContext?.anchor_message_id || "",
                  })}
                >
                  Go to thread
                </button>
              )}
              <button
                className="is-secondary"
                onClick={() => void actions.copyDraft(draftArtifact.body)}
              >
                Copy draft
              </button>
            </div>
          </div>
        ) : null}
        {draftArtifact && message.assistantFollowupText ? (
          <AnimatedAssistantText text={message.assistantFollowupText} animate={animate} onOpenThread={openThreadReference} />
        ) : null}
        {assistantTextComplete && message.replyGaps?.needs_user_input ? (
          <div className="ai-reply-gaps">
            {message.replyGaps.summary ? (
              <p>{message.replyGaps.summary}</p>
            ) : null}
            {message.replyGaps.questions.map((question) => (
              <label key={question.id}>
                <span>{question.question}</span>
                <input
                  value={answers[question.id] || ""}
                  placeholder={question.hint || "Your answer"}
                  onChange={(event) =>
                    setAnswers((current) => ({
                      ...current,
                      [question.id]: event.target.value,
                    }))
                  }
                />
              </label>
            ))}
            <button
              onClick={() => void submitReplyGap()}
              disabled={submittingGap}
            >
              {submittingGap ? "Generating…" : "Generate draft"}
            </button>
          </div>
        ) : null}
        <time>{aiTimeLabel(message.timestamp)}</time>
      </div>
    );
  }
  const sections = (result.sections || []).slice(0, 4);
  const summaryText =
    message.content ||
    result.summary ||
    result.plan_description ||
    "Anna finished scanning your inbox.";
  const animate = shouldAnimateAssistantText(message.timestamp);
  return (
    <div className="ai-message is-assistant">
      <div className="ai-answer-meta">
        {aiSearchStatus(result)}{" "}
        {result.plan_gmail_queries?.[0]?.query ? (
          <code>{result.plan_gmail_queries[0].query}</code>
        ) : null}
      </div>
      <AnimatedAssistantText text={summaryText} animate={animate} onOpenThread={openThreadReference} />
      {result.title || result.plan_title ? (
        <h2>{result.title || result.plan_title}</h2>
      ) : null}
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
                      {item.mail_links?.length ? (
                        <span className="ai-mail-links">
                          {item.mail_links.slice(0, 5).map((link) => (
                            <button
                              key={`${link.mailbox}:${link.message_id}`}
                              title={[link.label, link.from, link.date, link.snippet].filter(Boolean).join(" · ")}
                              onClick={() => onOpenMail(link)}
                            >
                              {link.label}
                            </button>
                          ))}
                        </span>
                      ) : item.subject && item.mailbox && item.thread_id && item.message_id ? (
                        <button
                          className="ai-single-mail-link"
                          title={item.subject}
                          onClick={() => onOpenMail({
                            label: item.subject || "Email",
                            mailbox: item.mailbox || "",
                            thread_id: item.thread_id || "",
                            message_id: item.message_id || "",
                            from: item.from,
                          })}
                        >
                          {item.subject}
                        </button>
                      ) : item.subject ? <b>{item.subject}</b> : null}
                      {item.context || item.suggestion
                        ? ` ${item.context || item.suggestion}`
                        : null}
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

function AiMessageBubble({
  message,
  currentMailContext,
  onUseArtifact,
  onOpenMail,
}: {
  message: AiChatMessage;
  currentMailContext: AiMailContextRef | null;
  onUseArtifact: (artifact: DraftReplyArtifact, mode: "append" | "replace") => void;
  onOpenMail: (target: AskMailLink) => void;
}) {
  if (message.role === "user") {
    return <div className="ai-message is-user">{message.content}</div>;
  }
  return (
    <AiAssistantMessage
      message={message}
      currentMailContext={currentMailContext}
      onUseArtifact={onUseArtifact}
      onOpenMail={onOpenMail}
    />
  );
}

function AiSidebar({
  collapsed,
  onToggle,
  currentMailContext,
  onUseArtifact,
  onOpenMail,
}: {
  collapsed: boolean;
  onToggle: () => void;
  currentMailContext: AiMailContextRef | null;
  onUseArtifact: (artifact: DraftReplyArtifact, mode: "append" | "replace") => void;
  onOpenMail: (target: AskMailLink) => void;
}) {
  const { state, actions } = useApp();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [showNewMessagePrompt, setShowNewMessagePrompt] = useState(false);
  const conversationRef = useRef<HTMLDivElement | null>(null);
  const pinnedToBottomRef = useRef(true);
  const scrollAfterSubmitRef = useRef(false);
  const running =
    state.aiChatLoading ||
    state.isCustomScanning ||
    Boolean(
      state.customRunProgress && state.customRunProgress.status !== "failed",
    );
  const starters = [
    "What needs my reply?",
    "Find urgent emails",
    "Plan my day",
  ];
  const conversation = state.aiChatMessages;

  const scrollConversationToBottom = useCallback(
    (behavior: ScrollBehavior = "smooth") => {
      const scroller = conversationRef.current;
      if (!scroller) return;
      scroller.scrollTo({ top: scroller.scrollHeight, behavior });
      pinnedToBottomRef.current = true;
      setShowNewMessagePrompt(false);
    },
    [],
  );

  const previousRunningRef = useRef(running);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      if (!pinnedToBottomRef.current && !scrollAfterSubmitRef.current) return;
      const behavior = scrollAfterSubmitRef.current ? "smooth" : "auto";
      scrollAfterSubmitRef.current = false;
      scrollConversationToBottom(behavior);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [conversation, scrollConversationToBottom]);

  useEffect(() => {
    pinnedToBottomRef.current = true;
    setShowNewMessagePrompt(false);
    const frame = window.requestAnimationFrame(() =>
      scrollConversationToBottom("auto"),
    );
    return () => window.cancelAnimationFrame(frame);
  }, [state.aiChatConversationId, scrollConversationToBottom]);

  useEffect(() => {
    const wasRunning = previousRunningRef.current;
    previousRunningRef.current = running;
    if (!wasRunning || running) return;

    const frame = window.requestAnimationFrame(() => {
      if (pinnedToBottomRef.current) {
        scrollConversationToBottom("auto");
      } else if (conversation.length) {
        setShowNewMessagePrompt(true);
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [conversation.length, running, scrollConversationToBottom]);

  const submit = () => {
    if (!state.customScanInput.trim() || running) return;
    setHistoryOpen(false);
    setShowNewMessagePrompt(false);
    pinnedToBottomRef.current = true;
    scrollAfterSubmitRef.current = true;
    window.requestAnimationFrame(() => scrollConversationToBottom("smooth"));
    void actions.sendAiChatMessage({ currentMailContext });
  };

  const startNewChat = () => {
    actions.startNewAiConversation();
    setHistoryOpen(false);
  };

  return (
    <aside className={`ai-sidebar ${collapsed ? "is-collapsed" : ""}`}>
      <div className="ai-sidebar-head">
        <div className="anna-wordmark">
          <button
            className="anna-logo-button"
            title={collapsed ? "Expand AI sidebar" : "Collapse AI sidebar"}
            onClick={onToggle}
          >
            <SparkleIcon />
          </button>
          <label>Anna Inbox</label>
        </div>
        <button className="new-chat-btn" onClick={startNewChat}>
          <span>＋</span> New chat
        </button>
      </div>

      <div
        className="ai-conversation"
        ref={conversationRef}
        onScroll={(event) => {
          const pinned = isAiConversationNearBottom(event.currentTarget);
          pinnedToBottomRef.current = pinned;
          if (pinned) setShowNewMessagePrompt(false);
        }}
      >
        {conversation.length ? (
          <div className="ai-message-stack" aria-live="polite">
            {conversation.map((message) => (
              <AiMessageBubble
                key={message.id}
                message={message}
                currentMailContext={currentMailContext}
                onUseArtifact={onUseArtifact}
                onOpenMail={onOpenMail}
              />
            ))}
          </div>
        ) : (
          <div className="ai-empty-state">
            <div className="ai-orb">
              <SparkleIcon />
            </div>
            <h1>How can I help you today?</h1>
            <p>
              Ask Anna to find, organize, or summarize anything in your inbox.
            </p>
          </div>
        )}
      </div>

      <section
        className={`ask-history-drawer ${historyOpen ? "is-open" : ""}`}
        aria-hidden={!historyOpen}
      >
        <div className="ask-history-head">
          <button
            onClick={() => setHistoryOpen(false)}
            aria-label="Back to Ask"
          >
            <ChevronLeftIcon />
          </button>
          <div>
            <strong>Ask history</strong>
            <span>Your previous conversations with Anna</span>
          </div>
        </div>
        <div className="ask-history-list">
          {state.askHistory.length ? (
            state.askHistory.map((entry, index) => {
              const active = Boolean(
                state.aiChatConversationId &&
                state.aiChatConversationId === entry.conversationId,
              );
              return (
                <div
                  key={`${entry.timestamp}-${index}`}
                  className={`ask-history-item ${active ? "is-active" : ""}`}
                >
                  <button
                    className="ask-history-open"
                    onClick={() => {
                      actions.openAiConversation(index);
                      setHistoryOpen(false);
                    }}
                  >
                    <div className="ask-history-row-top">
                      <strong>
                        {entry.query || entry.result.title || "Inbox question"}
                      </strong>
                      <time>{relativeTimeLabel(entry.timestamp)}</time>
                    </div>
                    <span>
                      {entry.result.summary ||
                        entry.result.plan_description ||
                        "Open conversation"}
                    </span>
                  </button>
                  <button
                    className="ask-history-delete"
                    aria-label="Delete chat"
                    data-tooltip="Delete chat"
                    onClick={() => actions.deleteAiConversation(index)}
                  >
                    <TrashIcon />
                  </button>
                </div>
              );
            })
          ) : (
            <div className="ask-history-empty">
              <HistoryIcon />
              <span>No conversation history</span>
            </div>
          )}
        </div>
      </section>

      <div className="ai-composer-wrap">
        {showNewMessagePrompt ? (
          <button
            className="ai-new-message-prompt"
            onClick={() => scrollConversationToBottom("smooth")}
            aria-label="Scroll to new messages"
          >
            有新消息 <span aria-hidden="true">↓</span>
          </button>
        ) : null}
        <div className={`ai-composer ${running ? "is-running" : ""}`}>
          <textarea
            value={state.customScanInput}
            placeholder="Find, organize, ask anything…"
            rows={3}
            onChange={(event) =>
              actions.setInput("customScanInput", event.target.value)
            }
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
                <button
                  className="ai-stop-button"
                  onClick={actions.stopAiGeneration}
                  aria-label="Stop generating"
                  title="Stop generating"
                >
                  <StopIcon />
                </button>
              ) : null}
              <button
                disabled={running || !state.customScanInput.trim()}
                onClick={submit}
                aria-label="Ask Anna"
              >
                <SendIcon />
              </button>
            </div>
          </div>
        </div>
        {!conversation.length ? (
          <div className="ai-starters">
            {starters.map((starter) => (
              <button
                key={starter}
                onClick={() => actions.setInput("customScanInput", starter)}
              >
                {starter}
              </button>
            ))}
          </div>
        ) : null}
      </div>

      <div className="ai-sidebar-foot">
        <span>
          <i className={state.runtime.connected ? "is-live" : ""} />
          {state.runtime.connected ? "LLM Connected" : "LLM Offline"}
        </span>
        <div>
          <button
            className={historyOpen ? "is-active" : ""}
            title="Ask history"
            onClick={() => setHistoryOpen((open) => !open)}
          >
            <HistoryIcon />
          </button>
        </div>
      </div>
    </aside>
  );
}

export function hasMailboxScanError(
  lastScanStatus?: string,
  lastError?: string,
  scanError?: string,
) {
  const normalizedStatus = String(lastScanStatus || "")
    .trim()
    .toLowerCase();
  return (
    Boolean(scanError?.trim()) ||
    Boolean(lastError?.trim()) ||
    normalizedStatus === "failed" ||
    normalizedStatus === "error"
  );
}

function AccountAvatar({
  email,
  url,
  className,
}: {
  email: string;
  url?: string;
  className: string;
}) {
  const [failed, setFailed] = useState(false);
  const normalizedEmail = email.trim();
  if (!normalizedEmail) {
    return (
      <span className={`${className} is-default`} aria-hidden="true">
        <PersonIcon />
      </span>
    );
  }
  const initial = (email.split("@")[0]?.charAt(0) || "A").toUpperCase();
  return url && !failed ? (
    <img
      className={className}
      src={url}
      alt=""
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
    />
  ) : (
    <span className={className}>{initial}</span>
  );
}

function AccountRail() {
  const { state, actions } = useApp();
  const [menuOpen, setMenuOpen] = useState(false);
  const mailbox = state.mailbox || state.selectedMailboxes[0] || "";
  const activeMailbox = state.mailboxes.find(
    (item) => item.email.toLowerCase() === mailbox.toLowerCase(),
  );
  const authorizedMailboxes = state.mailboxes.filter(
    (item) => item.authorized !== false,
  );
  const gmailAuthorizationRequired = isGmailAuthorizationRequired(
    state.gmailAuthStatus,
  );
  const mailboxes = gmailAuthorizationRequired
    ? []
    : state.mailboxes.length
      ? authorizedMailboxes
      : mailbox
        ? [
            {
              email: mailbox,
              provider: "gmail",
              authorized: state.gmailAuthStatus.authorized,
            },
          ]
        : [];
  const scanFailed = hasMailboxScanError(
    activeMailbox?.last_scan_status,
    activeMailbox?.last_error,
    state.scanError,
  );

  return (
    <aside className="account-rail" aria-label="Account controls">
      <button
        className="account-avatar-btn"
        title="Switch account"
        aria-expanded={menuOpen}
        onClick={() => setMenuOpen((open) => !open)}
      >
        <AccountAvatar
          email={mailbox}
          url={activeMailbox?.avatar_url}
          className="account-avatar-image"
        />
        {mailboxes.length ? (
          <i className={scanFailed ? "is-inactive" : ""} />
        ) : null}
      </button>
      {menuOpen ? (
        <button
          className="account-menu-backdrop"
          aria-label="Close account menu"
          onClick={() => setMenuOpen(false)}
        />
      ) : null}
      <section
        className={`account-menu ${menuOpen ? "is-open" : ""}`}
        aria-hidden={!menuOpen}
      >
        <header>
          <span>Accounts</span>
          <small>Switch inbox</small>
        </header>
        <div>
          {!mailboxes.length ? <p className="account-menu-empty">empty</p> : null}
          {mailboxes.map((item) => {
            const active = item.email.toLowerCase() === mailbox.toLowerCase();
            return (
              <button
                key={item.email}
                className={active ? "is-active" : ""}
                disabled={item.authorized === false}
                onClick={() => {
                  setMenuOpen(false);
                  void actions.switchMailbox(item.email);
                }}
              >
                <AccountAvatar
                  email={item.email}
                  url={item.avatar_url}
                  className="account-menu-avatar"
                />
                <span className="account-menu-copy">
                  <strong>{accountDisplayName(item)}</strong>
                  <small>{item.email}</small>
                </span>
                {active ? (
                  <span className="account-menu-check">
                    <CheckIcon />
                  </span>
                ) : null}
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
  const [insertRequest, setInsertRequest] = useState<{
    nonce: string;
    artifact: DraftReplyArtifact;
    mode: "append" | "replace";
  } | null>(null);
  const [externalDetailMessage, setExternalDetailMessage] = useState<InboxMessage | null>(null);
  const [drawerMessage, setDrawerMessage] = useState<InboxMessage | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [authChecking, setAuthChecking] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(initialSidebarWidth);
  const [sidebarResizing, setSidebarResizing] = useState(false);
  const [folderOpen, setFolderOpen] = useState(false);
  const [refreshChoiceOpen, setRefreshChoiceOpen] = useState(false);
  const [mailboxView, setMailboxView] = useState<MailboxView>("inbox");
  const mailboxViewRef = useRef<MailboxView>("inbox");
  const [feedWindow, setFeedWindow] = useState<InboxFeedWindow>(
    DEFAULT_INBOX_FEED_WINDOW,
  );
  const [feedAction, setFeedAction] = useState<FeedActionState>(null);
  const [messageBodies, setMessageBodies] = useState<
    Record<string, { status: "loading" | "ready" | "error"; body: string }>
  >({});
  const bodyRequests = useRef(new Set<string>());
  const bodyPreheatSession = useRef(0);
  const bodyPreheatSeen = useRef(new Set<string>());
  const loadInboxEmailBodyRef = useRef(actions.loadInboxEmailBody);
  const pageLoadInFlight = useRef(false);
  const requestedGmailCursors = useRef(new Set<string>());
  const mailFeedRef = useRef<HTMLElement | null>(null);
  const avatarRequestKey = useRef("");
  const avatarMisses = useRef(new Set<string>());
  const avatarPermissionNoticeShown = useRef(false);
  const drawerCloseTimer = useRef<number | null>(null);
  const mailbox = state.selectedMailboxes[0] || state.mailbox;
  const flagsKey = `anna-inbox:mail-flags:${mailbox}`;
  const contactAvatarsKey = `anna-inbox:contact-avatars:${mailbox}`;
  const [flags, setFlags] = useState<MailUiFlags>({
    todos: [],
    snoozed: [],
    snoozedUntil: {},
    done: [],
    doneRemoved: [],
    drafts: [],
    saved: {},
  });
  const [contactAvatars, setContactAvatars] = useState<Record<string, string>>(
    {},
  );
  const [cachedInboxBannerDismissed, setCachedInboxBannerDismissed] =
    useState(false);
  const sidebarWidthRef = useRef(sidebarWidth);
  const sidebarDragRef = useRef<{
    pointerId: number;
    startX: number;
    startWidth: number;
  } | null>(null);
  const sidebarBounds = sidebarWidthBounds();
  const layoutStyle = {
    "--ai-sidebar-width": `${sidebarWidth}px`,
  } as CSSProperties;

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
    loadInboxEmailBodyRef.current = actions.loadInboxEmailBody;
  }, [actions.loadInboxEmailBody]);

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
    sidebarDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: sidebarWidthRef.current,
    };
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
    if (
      drag?.pointerId === event.pointerId &&
      event.currentTarget.hasPointerCapture(event.pointerId)
    ) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (!drag || drag.pointerId !== event.pointerId) return;
    sidebarDragRef.current = null;
    setSidebarResizing(false);
    document.body.classList.remove("is-resizing-ai-sidebar");
    persistSidebarWidth(sidebarWidthRef.current);
  };

  const adjustSidebarWithKeyboard = (
    event: ReactKeyboardEvent<HTMLDivElement>,
  ) => {
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
    let cancelled = false;
    setFlags({
      todos: [],
      snoozed: [],
      snoozedUntil: {},
      done: [],
      doneRemoved: [],
      drafts: [],
      saved: {},
    });
    setContactAvatars({});
    avatarMisses.current = new Set();
    void getMailFlags(mailbox, flagsKey).then((saved) => {
      if (cancelled || !saved) return;
      setFlags(saved);
    });
    void getContactAvatarCache(mailbox, contactAvatarsKey).then((cached) => {
      if (cancelled || !cached) return;
      const avatarsFresh =
        Number(cached.avatarsUpdatedAt || 0) >
        Date.now() - 7 * 24 * 60 * 60 * 1000;
      const missesFresh =
        Number(cached.missingUpdatedAt || 0) >
        Date.now() - 24 * 60 * 60 * 1000;
      setContactAvatars(
        avatarsFresh && cached.avatars && typeof cached.avatars === "object"
          ? cached.avatars
          : {},
      );
      avatarMisses.current = new Set(
        missesFresh && Array.isArray(cached.missing) ? cached.missing : [],
      );
    });
    avatarRequestKey.current = "";
    avatarPermissionNoticeShown.current = false;
    bodyPreheatSeen.current.clear();
    bodyPreheatSession.current += 1;
    requestedGmailCursors.current.clear();
    mailboxViewRef.current = "inbox";
    setMailboxView("inbox");
    setFilter("important");
    setFeedWindow(DEFAULT_INBOX_FEED_WINDOW);
    return () => {
      cancelled = true;
    };
  }, [contactAvatarsKey, flagsKey, mailbox]);

  useEffect(() => {
    try {
      setCachedInboxBannerDismissed(
        window.localStorage.getItem(
          cachedInboxBannerSkipStorageKey(mailbox),
        ) === "1",
      );
    } catch {
      setCachedInboxBannerDismissed(false);
    }
  }, [mailbox]);

  useEffect(() => {
    if (!state.inboxError) {
      setCachedInboxBannerDismissed(false);
      try {
        window.localStorage.removeItem(
          cachedInboxBannerSkipStorageKey(mailbox),
        );
      } catch {
        // Ignore storage failures; dismissal is a best-effort preference.
      }
    }
  }, [mailbox, state.inboxError]);

  const counts = useMemo(
    () => ({
      important: state.inboxMessages.filter(
        (message) =>
          isImportantMessage(message) &&
          !flags.todos.includes(message.id) &&
          !isDoneMessage(message, flags) &&
          !flags.snoozed.includes(message.id),
      ).length,
      other: state.inboxMessages.filter(
        (message) =>
          !isImportantMessage(message) &&
          !flags.todos.includes(message.id) &&
          !isDoneMessage(message, flags) &&
          !flags.snoozed.includes(message.id),
      ).length,
    }),
    [flags, state.inboxMessages],
  );
  const localCategory = isLocalMailboxView(mailboxView);

  const inboxMessagesWithDrafts = useMemo(
    () => mergeDraftOverlayMessages(state.inboxMessages, state.inboxDraftMessages),
    [state.inboxDraftMessages, state.inboxMessages],
  );
  const inboxSnapshotMessagesWithDrafts = useMemo(
    () => mergeDraftOverlayMessages(state.inboxSnapshotMessages, state.inboxDraftMessages),
    [state.inboxDraftMessages, state.inboxSnapshotMessages],
  );
  const sourceMessages = useMemo(
    () =>
      resolveSourceMessages(
        mailboxView,
        inboxMessagesWithDrafts,
        inboxSnapshotMessagesWithDrafts,
        flags,
      ),
    [flags, inboxMessagesWithDrafts, inboxSnapshotMessagesWithDrafts, mailboxView],
  );

  const messagesInThread = useCallback(
    (message: InboxMessage, currentFlags: MailUiFlags = flags) => {
      const threadId = message.thread_id || message.id;
      const byId = new Map<string, InboxMessage>();
      for (const item of [
        message,
        ...sourceMessages,
        ...state.inboxSnapshotMessages,
        ...state.inboxMessages,
        ...Object.values(currentFlags.saved),
      ]) {
        if ((item.thread_id || item.id) === threadId) byId.set(item.id, item);
      }
      return [...byId.values()];
    },
    [flags, sourceMessages, state.inboxMessages, state.inboxSnapshotMessages],
  );

  const setWorkflowFlag = useCallback(
    (
      kind: "todos" | "snoozed" | "done",
      messages: InboxMessage[],
      enabled: boolean,
      snoozeUntil?: string,
    ) => {
      const ids = new Set(messages.map((item) => item.id));
      setFlags((current) => {
        const next = { ...current, saved: { ...current.saved }, snoozedUntil: { ...(current.snoozedUntil || {}) } };
        for (const workflowKind of ["todos", "snoozed", "done"] as const) {
          const retained = current[workflowKind].filter((id) => !ids.has(id));
          next[workflowKind] =
            enabled && workflowKind === kind ? [...retained, ...ids] : retained;
        }
        for (const id of ids) delete next.snoozedUntil[id];
        if (enabled && kind === "snoozed" && snoozeUntil) {
          for (const id of ids) next.snoozedUntil[id] = snoozeUntil;
        }
        if (kind === "done") {
          const sentIds = messages.filter(isSentMessage).map((item) => item.id);
          const removed = current.doneRemoved.filter((id) => !ids.has(id));
          next.doneRemoved = enabled ? removed : [...removed, ...sentIds];
        } else {
          next.doneRemoved = current.doneRemoved;
        }
        for (const item of messages) next.saved[item.id] = item;
        void setMailFlags(mailbox, next);
        return next;
      });
    },
    [mailbox],
  );

  const restoreWorkflowFlags = useCallback(
    (messages: InboxMessage[], previous: MailUiFlags) => {
      const ids = new Set(messages.map((item) => item.id));
      setFlags((current) => {
        const next = { ...current, saved: { ...current.saved }, snoozedUntil: { ...(current.snoozedUntil || {}) } };
        for (const workflowKind of ["todos", "snoozed", "done"] as const) {
          next[workflowKind] = [
            ...current[workflowKind].filter((id) => !ids.has(id)),
            ...previous[workflowKind].filter((id) => ids.has(id)),
          ];
        }
        for (const id of ids) {
          if (previous.snoozedUntil?.[id]) next.snoozedUntil[id] = previous.snoozedUntil[id];
          else delete next.snoozedUntil[id];
        }
        next.doneRemoved = [
          ...current.doneRemoved.filter((id) => !ids.has(id)),
          ...previous.doneRemoved.filter((id) => ids.has(id)),
        ];
        void setMailFlags(mailbox, next);
        return next;
      });
    },
    [mailbox],
  );

  const prefetchMessageBody = async (message: InboxMessage) => {
    if (message.id === selectedId) return;
    const bodyMailbox = message.mailbox || mailbox;
    const key = `${bodyMailbox}:${message.id}:${message.internal_date || ""}`;
    if (bodyRequests.current.has(key) || messageBodies[key]?.status === "ready")
      return;
    bodyRequests.current.add(key);
    setMessageBodies((current) => ({
      ...current,
      [key]: { status: "loading", body: current[key]?.body || "" },
    }));
    try {
      const cached = await getCachedMessageBody(bodyMailbox, message.id, message.internal_date);
      if (cached) {
        setMessageBodies((current) => ({
          ...current,
          [key]: { status: "ready", body: cached.body_text },
        }));
        return;
      }
      const body = await loadInboxEmailBodyRef.current(
        message.id,
        bodyMailbox,
      );
      void setCachedMessageBody(bodyMailbox, message, { body_text: body });
      setMessageBodies((current) => ({
        ...current,
        [key]: { status: "ready", body },
      }));
    } catch {
      setMessageBodies((current) => ({
        ...current,
        [key]: { status: "error", body: "" },
      }));
    } finally {
      bodyRequests.current.delete(key);
    }
  };

  useEffect(() => {
    const messages = state.inboxSnapshotMessages.length
      ? state.inboxSnapshotMessages
      : state.inboxMessages;
    if (!mailbox || !messages.length) return;
    const cleanup = scheduleDeferredWork(() => {
      const emails = [
        ...new Set(
          messages.flatMap((message) =>
            [message.from, message.to]
              .flatMap(splitAddresses)
              .map((value) => senderParts(value).email.toLowerCase())
              .filter((email) => email.includes("@")),
          ),
        ),
      ]
        .filter(
          (email) => !contactAvatars[email] && !avatarMisses.current.has(email),
        )
        .slice(0, 200);
      const requestKey = `${mailbox}:${emails.join("|")}`;
      if (!emails.length || avatarRequestKey.current === requestKey) return;
      avatarRequestKey.current = requestKey;
      void actions
        .loadContactAvatars(emails, mailbox)
        .then(({ avatars, permissionRequired, serviceDisabled }) => {
          const missing = permissionRequired || serviceDisabled
            ? []
            : emails.filter((email) => !avatars[email]);
          for (const email of missing) avatarMisses.current.add(email);
          if (Object.keys(avatars).length || missing.length) {
            setContactAvatars((current) => {
              const next = { ...current, ...avatars };
              void setContactAvatarCache(mailbox, {
                avatars: next,
                missing: [...avatarMisses.current],
                avatarsUpdatedAt: Date.now(),
                missingUpdatedAt: Date.now(),
              });
              return next;
            });
          }
          if (serviceDisabled) {
            if (!avatarPermissionNoticeShown.current) {
              avatarPermissionNoticeShown.current = true;
              actions.showToast(
                "Enable Google People API for this OAuth project to load contact photos.",
              );
            }
            return;
          }
          if (permissionRequired) {
            if (!avatarPermissionNoticeShown.current) {
              avatarPermissionNoticeShown.current = true;
            }
            return;
          }
        })
        .catch(() => undefined);
    }, 320);
    return cleanup;
    // Avatar lookup is auxiliary and follows mailbox snapshot changes only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mailbox, state.inboxMessages, state.inboxSnapshotMessages]);

  const visible = useMemo(() => {
    const query = search.trim().toLowerCase();
    return sourceMessages.filter((message) => {
      const matchesView =
        mailboxView === "inbox"
          ? !flags.todos.includes(message.id) &&
            !isDoneMessage(message, flags) &&
            !flags.snoozed.includes(message.id)
          : mailboxView === "todos"
            ? flags.todos.includes(message.id)
            : mailboxView === "snoozed"
              ? flags.snoozed.includes(message.id)
              : mailboxView === "done"
                ? isDoneMessage(message, flags)
                : mailboxView === "starred"
                  ? isStarredMessage(message)
                  : true;
      const matchesFilter =
        mailboxView !== "inbox"
          ? true
          : filter === "important"
            ? isImportantMessage(message)
            : !isImportantMessage(message) && !flags.todos.includes(message.id);
      if (!matchesView) return false;
      if (!matchesFilter) return false;
      if (!query) return true;
      return `${message.from || ""} ${message.subject || ""} ${message.snippet || ""}`
        .toLowerCase()
        .includes(query);
    });
  }, [filter, flags, mailboxView, search, sourceMessages]);

  const displayedVisible = useMemo(
    () => (localCategory ? visible.slice(0, feedWindow.localLimit) : visible),
    [feedWindow.localLimit, localCategory, visible],
  );
  useEffect(() => {
    if (!mailbox || !displayedVisible.length) return;
    const session = ++bodyPreheatSession.current;
    const start = Math.max(0, displayedVisible.length - INBOX_FEED_PAGE_SIZE);
    const pageMessages = displayedVisible.slice(start).filter((message) => message.id);
    if (!pageMessages.length) return;
    const cleanup = scheduleDeferredWork(() => {
      let cursor = 0;
      let active = 0;
      const runNext = () => {
        if (session !== bodyPreheatSession.current) return;
        while (active < 5 && cursor < pageMessages.length) {
          const message = pageMessages[cursor++];
          const bodyMailbox = message.mailbox || mailbox;
          const key = `${bodyMailbox}:${message.id}:${message.internal_date || ""}`;
          if (bodyPreheatSeen.current.has(key) || bodyRequests.current.has(key)) continue;
          bodyPreheatSeen.current.add(key);
          bodyRequests.current.add(key);
          active += 1;
          void (async () => {
            try {
              const cached = await getCachedMessageBody(bodyMailbox, message.id, message.internal_date);
              if (cached || session !== bodyPreheatSession.current) return;
              const body = await loadInboxEmailBodyRef.current(message.id, bodyMailbox);
              if (session !== bodyPreheatSession.current) return;
              await setCachedMessageBody(bodyMailbox, message, { body_text: body });
            } catch {
              bodyPreheatSeen.current.delete(key);
            } finally {
              bodyRequests.current.delete(key);
              active -= 1;
              runNext();
            }
          })();
        }
      };
      runNext();
    }, 520);
    return () => {
      cleanup();
      bodyPreheatSession.current += 1;
    };
  }, [displayedVisible, mailbox, mailboxView]);

  const dismissCachedInboxBanner = () => {
    setCachedInboxBannerDismissed(true);
    try {
      window.localStorage.setItem(
        cachedInboxBannerSkipStorageKey(mailbox),
        "1",
      );
    } catch {
      // Ignore storage failures; dismissal is a best-effort preference.
    }
  };

  const loadGmailPage = useCallback(
    async (
      category: MailboxView,
      targetDays: number,
      pageToken: string,
      pageOffset: number,
      excludeMessageIds: string[],
    ) => {
      let nextToken = pageToken;
      let nextOffset = pageOffset;
      for (let attempts = 0; attempts < 4; attempts += 1) {
        const cursorKey = `${category}:${targetDays}:${nextToken || "first"}:${nextOffset}`;
        if (requestedGmailCursors.current.has(cursorKey)) return null;
        requestedGmailCursors.current.add(cursorKey);
        const result = await actions.loadGmailInboxEmailsPage(
          category,
          targetDays,
          nextToken,
          nextOffset,
          excludeMessageIds,
        );
        if (!result.ok) {
          requestedGmailCursors.current.delete(cursorKey);
          return result;
        }
        if (result.count > 0 || !result.hasMore) {
          return result;
        }
        nextToken = result.pageToken;
        nextOffset = result.pageOffset;
      }
      return {
        ok: true,
        count: 0,
        hasMore: false,
        pageToken: nextToken,
        pageOffset: nextOffset,
      };
    },
    [actions],
  );

  const loadRemoteCategory = useCallback(
    async (category: MailboxView, targetDays = 7) => {
      const result = await actions.loadCachedInboxEmails(
        category,
        targetDays,
        0,
        false,
      );
      if (!result.ok || mailboxViewRef.current !== category) return result.ok;
      if (result.count === 0 && !result.hasMore) {
        const gmail = await loadGmailPage(category, targetDays, "", 0, []);
        if (gmail?.ok && mailboxViewRef.current === category) {
          setFeedWindow({
            days: targetDays,
            nextOffset: result.nextOffset,
            hasMore: gmail.hasMore,
            localLimit: INBOX_FEED_PAGE_SIZE,
            source: "gmail",
            gmailPageToken: gmail.pageToken,
            gmailPageOffset: gmail.pageOffset,
          });
        }
        return Boolean(gmail?.ok);
      }
      setFeedWindow({
        days: targetDays,
        nextOffset: result.nextOffset,
        hasMore: true,
        localLimit: INBOX_FEED_PAGE_SIZE,
        source: result.hasMore ? "cache" : "gmail",
        gmailPageToken: "",
        gmailPageOffset: 0,
      });
      return true;
    },
    [actions, loadGmailPage],
  );

  const loadRemainingAllTimeInbox = useCallback(
    async (
      source: InboxFeedWindow["source"],
      nextOffset: number,
      gmailPageToken: string,
      gmailPageOffset: number,
      excludeMessageIds: string[],
    ) => {
      const excludeIds = new Set(excludeMessageIds.filter(Boolean));
      let currentSource = source;
      let currentNextOffset = nextOffset;
      let currentGmailPageToken = gmailPageToken;
      let currentGmailPageOffset = gmailPageOffset;
      while (mailboxViewRef.current === "inbox") {
        if (currentSource === "gmail") {
          await new Promise((resolve) => window.setTimeout(resolve, 0));
        }
        const result =
          currentSource === "cache"
            ? await actions.loadCachedInboxEmails(
                "inbox",
                INBOX_ALL_TIME_DAYS,
                currentNextOffset,
                true,
              )
            : await loadGmailPage(
                "inbox",
                INBOX_ALL_TIME_DAYS,
                currentGmailPageToken,
                currentGmailPageOffset,
                [...excludeIds],
              );
        if (mailboxViewRef.current !== "inbox") return false;
        if (!result?.ok) return false;
        for (const message of result.messages || []) {
          if (message.id) excludeIds.add(message.id);
        }
        if (currentSource === "cache") {
          currentNextOffset = "nextOffset" in result ? result.nextOffset : currentNextOffset;
          if (result.hasMore) {
            setFeedWindow({
              days: INBOX_ALL_TIME_DAYS,
              nextOffset: currentNextOffset,
              hasMore: true,
              localLimit: INBOX_FEED_PAGE_SIZE,
              source: "cache",
              gmailPageToken: "",
              gmailPageOffset: 0,
            });
            continue;
          }
          currentSource = "gmail";
          setFeedWindow({
            days: INBOX_ALL_TIME_DAYS,
            nextOffset: currentNextOffset,
            hasMore: true,
            localLimit: INBOX_FEED_PAGE_SIZE,
            source: currentSource,
            gmailPageToken: currentGmailPageToken,
            gmailPageOffset: currentGmailPageOffset,
          });
          continue;
        }
        currentGmailPageToken =
          "pageToken" in result ? result.pageToken : currentGmailPageToken;
        currentGmailPageOffset =
          "pageOffset" in result ? result.pageOffset : currentGmailPageOffset;
        setFeedWindow({
          days: INBOX_ALL_TIME_DAYS,
          nextOffset: currentNextOffset,
          hasMore: result.hasMore,
          localLimit: INBOX_FEED_PAGE_SIZE,
          source: "gmail",
          gmailPageToken: currentGmailPageToken,
          gmailPageOffset: currentGmailPageOffset,
        });
        if (!result.hasMore) break;
      }
      return true;
    },
    [actions, loadGmailPage],
  );

  const syncInbox = useCallback(
    async (targetDays = feedWindow.days, clearCache = false) => {
      requestedGmailCursors.current.clear();
      setFeedAction("refresh");
      try {
        if (localCategory) {
          if (mailboxView === "drafts") {
            await actions.listInboxThreadDrafts(mailbox, 100);
            setFeedWindow((current) => ({ ...current, localLimit: INBOX_FEED_PAGE_SIZE }));
            return true;
          }
          const result = await actions.refreshInboxEmails("inbox", 7, clearCache);
          if (result.ok) {
            setFeedWindow((current) => ({
              ...current,
              localLimit: INBOX_FEED_PAGE_SIZE,
            }));
          }
          return result.ok;
        }
        if (targetDays > 7 && !clearCache) {
          return loadRemoteCategory(mailboxView, targetDays);
        }
        if (mailboxView === "inbox" && targetDays === INBOX_ALL_TIME_DAYS) {
          const result = await actions.refreshInboxEmails(
            "inbox",
            INBOX_ALL_TIME_DAYS,
            clearCache,
          );
          if (!result.ok) return false;
          const source: InboxFeedWindow["source"] = result.hasMore
            ? "cache"
            : "gmail";
          const excludeMessageIds = (result.messages || [])
            .map((message) => message.id)
            .filter(Boolean);
          setFeedWindow({
            days: INBOX_ALL_TIME_DAYS,
            nextOffset: result.nextOffset,
            hasMore: result.hasMore,
            localLimit: INBOX_FEED_PAGE_SIZE,
            source,
            gmailPageToken: "",
            gmailPageOffset: 0,
          });
          if (!result.hasMore) return true;
          const loadedAll = await loadRemainingAllTimeInbox(
            source,
            result.nextOffset,
            "",
            0,
            excludeMessageIds,
          );
          if (loadedAll) actions.showToast("Loaded all inbox emails.");
          return loadedAll;
        }
        const result = await actions.refreshInboxEmails(
          mailboxView,
          targetDays,
          clearCache,
        );
        if (result.ok) {
          setFeedWindow({
            days: targetDays,
            nextOffset: result.nextOffset,
            hasMore: true,
            localLimit: INBOX_FEED_PAGE_SIZE,
            source: result.hasMore ? "cache" : "gmail",
            gmailPageToken: "",
            gmailPageOffset: 0,
          });
        }
        return result.ok;
      } finally {
        setFeedAction((current) => (current === "refresh" ? null : current));
      }
    },
    [actions, feedWindow.days, loadRemainingAllTimeInbox, loadRemoteCategory, localCategory, mailbox, mailboxView],
  );

  const reloadInboxFromEmpty = useCallback(async () => {
    setRefreshChoiceOpen(false);
    setSelectedId("");
    setMessageBodies({});
    setContactAvatars({});
    avatarMisses.current.clear();
    bodyRequests.current.clear();
    bodyPreheatSeen.current.clear();
    bodyPreheatSession.current += 1;
    pageLoadInFlight.current = false;
    requestedGmailCursors.current.clear();
    actions.resetInboxFeed();
    setFeedWindow((current) => ({
      ...DEFAULT_INBOX_FEED_WINDOW,
      days: current.days,
    }));
    await syncInbox(feedWindow.days, true);
  }, [actions, feedWindow.days, syncInbox]);

  const continueLoadingInbox = useCallback(async () => {
    setRefreshChoiceOpen(false);
    requestedGmailCursors.current.clear();
    setFeedAction("refresh");
    try {
      if (localCategory) {
        await syncInbox(feedWindow.days);
        return;
      }
      const category = mailboxView;
      const targetDays = feedWindow.days;
      const excludeIds = sourceMessages.map((message) => message.id).filter(Boolean);
      const result = await loadGmailPage(
        category,
        targetDays,
        "",
        0,
        excludeIds,
      );
      if (!result?.ok) {
        actions.showToast("Failed to load new emails.");
        return;
      }
      setFeedWindow((current) => ({
        ...current,
        days: targetDays,
        hasMore: result.hasMore,
        source: "gmail",
        gmailPageToken: result.pageToken,
        gmailPageOffset: result.pageOffset,
      }));
      actions.showToast(result.count ? `${result.count} new email${result.count === 1 ? "" : "s"} loaded.` : "No new emails found.");
    } finally {
      setFeedAction((current) => (current === "refresh" ? null : current));
    }
  }, [actions, feedWindow.days, loadGmailPage, localCategory, mailboxView, sourceMessages, syncInbox]);

  const gmailAuthorizationRequired = isGmailAuthorizationRequired(
    state.gmailAuthStatus,
  );
  const checkGmailAuthorization = useCallback(async () => {
    if (authChecking) return;
    setAuthChecking(true);
    try {
      const result = await actions.checkAnyGmailAuth();
      if (result.authorized) {
        const mailboxState = await actions.loadMailboxes();
        const primary = mailboxState.primary.trim().toLowerCase();
        if (primary && primary !== mailbox.trim().toLowerCase()) {
          await actions.switchMailbox(primary);
        } else if (primary) {
          await actions.loadInboxEmails("inbox", 7, true);
        }
      }
    } finally {
      setAuthChecking(false);
    }
  }, [actions, authChecking, mailbox]);

  const loadOlderInbox = useCallback(async () => {
    setFeedAction("older");
    try {
      const result = await actions.loadCachedInboxEmails(
        "inbox",
        INBOX_LAST_MONTH_DAYS,
        0,
        false,
      );
      if (mailboxViewRef.current !== "inbox") return;
      if (result.ok) {
        setFeedWindow({
          days: INBOX_LAST_MONTH_DAYS,
          nextOffset: result.nextOffset,
          hasMore: true,
          localLimit: INBOX_FEED_PAGE_SIZE,
          source: result.hasMore ? "cache" : "gmail",
          gmailPageToken: "",
          gmailPageOffset: 0,
        });
        actions.showToast("Inbox synced for the last 1 month.");
      } else {
        actions.showToast("Failed to sync the last 1 month.");
      }
    } finally {
      setFeedAction((current) => (current === "older" ? null : current));
    }
  }, [actions]);

  const loadMoreInbox = useCallback(async () => {
    if (
      pageLoadInFlight.current ||
      mailboxView !== "inbox" ||
      feedWindow.days !== INBOX_LAST_MONTH_DAYS
    )
      return;
    pageLoadInFlight.current = true;
    setFeedAction("more");
    try {
      const initial = await actions.loadCachedInboxEmails(
        "inbox",
        INBOX_ALL_TIME_DAYS,
        0,
        false,
      );
      if (mailboxViewRef.current !== "inbox") return;
      if (!initial.ok) {
        actions.showToast("Failed to load emails older than 1 month.");
        return;
      }
      let source: InboxFeedWindow["source"] = initial.hasMore
        ? "cache"
        : "gmail";
      let nextOffset = initial.nextOffset;
      let gmailPageToken = "";
      let gmailPageOffset = 0;
      const excludeMessageIds = (initial.messages || [])
        .map((message) => message.id)
        .filter(Boolean);
      setFeedWindow({
        days: INBOX_ALL_TIME_DAYS,
        nextOffset,
        hasMore: true,
        localLimit: INBOX_FEED_PAGE_SIZE,
        source,
        gmailPageToken,
        gmailPageOffset,
      });
      const loadedAll = await loadRemainingAllTimeInbox(
        source,
        nextOffset,
        gmailPageToken,
        gmailPageOffset,
        excludeMessageIds,
      );
      if (!loadedAll) {
        actions.showToast("Failed to load emails older than 1 month.");
        return;
      }
      actions.showToast("Loaded all inbox emails.");
    } finally {
      pageLoadInFlight.current = false;
      setFeedAction((current) => (current === "more" ? null : current));
    }
  }, [actions, feedWindow.days, loadRemainingAllTimeInbox, mailboxView]);

  const loadNextCategoryPage = useCallback(async () => {
    if (mailboxView === "inbox" || pageLoadInFlight.current) return;
    if (localCategory) {
      if (feedWindow.localLimit < visible.length) {
        setFeedWindow((current) => ({
          ...current,
          localLimit: current.localLimit + INBOX_FEED_PAGE_SIZE,
        }));
      }
      return;
    }
    if (!feedWindow.hasMore) return;
    pageLoadInFlight.current = true;
    setFeedAction("category-page");
    try {
      if (feedWindow.source === "cache") {
        const result = await actions.loadCachedInboxEmails(
          mailboxView,
          feedWindow.days,
          feedWindow.nextOffset,
          true,
        );
        if (result.ok) {
          setFeedWindow((current) => ({
            ...current,
            nextOffset: result.nextOffset,
            hasMore: true,
            source: result.hasMore ? "cache" : "gmail",
            gmailPageToken: "",
            gmailPageOffset: 0,
          }));
        }
      } else {
        const result = await loadGmailPage(
          mailboxView,
          feedWindow.days,
          feedWindow.gmailPageToken,
          feedWindow.gmailPageOffset,
          sourceMessages.map((message) => message.id),
        );
        if (result?.ok) {
          setFeedWindow((current) => ({
            ...current,
            hasMore: result.hasMore,
            source: "gmail",
            gmailPageToken: result.pageToken,
            gmailPageOffset: result.pageOffset,
          }));
        }
      }
    } finally {
      pageLoadInFlight.current = false;
      setFeedAction((current) =>
        current === "category-page" ? null : current,
      );
    }
  }, [
    actions,
    feedWindow.days,
    feedWindow.gmailPageOffset,
    feedWindow.gmailPageToken,
    feedWindow.hasMore,
    feedWindow.localLimit,
    feedWindow.nextOffset,
    feedWindow.source,
    loadGmailPage,
    localCategory,
    mailboxView,
    sourceMessages,
    visible.length,
  ]);

  const handleFeedScroll = useCallback(
    (event: ReactUIEvent<HTMLElement>) => {
      if (mailboxView === "inbox") return;
      const feed = event.currentTarget;
      if (
        feed.scrollTop <= 0 ||
        feed.scrollHeight - feed.scrollTop - feed.clientHeight > 80
      )
        return;
      void loadNextCategoryPage();
    },
    [loadNextCategoryPage, mailboxView],
  );

  useEffect(() => {
    if (
      mailboxView === "inbox" ||
      localCategory ||
      !feedWindow.hasMore ||
      state.inboxSnapshotLoading
    )
      return;
    const frame = window.requestAnimationFrame(() => {
      const feed = mailFeedRef.current;
      if (feed && feed.scrollHeight <= feed.clientHeight + 1) {
        void loadNextCategoryPage();
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [
    displayedVisible.length,
    feedWindow.hasMore,
    loadNextCategoryPage,
    localCategory,
    mailboxView,
    state.inboxSnapshotLoading,
  ]);

  const isInboxSyncing =
    state.inboxSnapshotLoading || feedAction === "refresh";
  const days = feedWindow.days;
  const canLoadMoreInbox =
    mailboxView === "inbox" && days === INBOX_LAST_MONTH_DAYS;

  const grouped = useMemo(() => {
    if (mailboxView !== "inbox") {
      return [{ label: "", messages: displayedVisible }];
    }
    const groups: Array<{ label: string; messages: InboxMessage[] }> = [];
    for (const message of displayedVisible) {
      const label = groupLabel(message);
      const current = groups[groups.length - 1];
      if (!current || current.label !== label)
        groups.push({ label, messages: [message] });
      else current.messages.push(message);
    }
    return groups;
  }, [displayedVisible, mailboxView]);

  const selectedMessage = useMemo(() => {
    if (!selectedId) return null;
    return (
      sourceMessages.find((item) => item.id === selectedId) ||
      state.inboxDraftMessages.find((item) => item.id === selectedId) ||
      state.inboxSnapshotMessages.find((item) => item.id === selectedId) ||
      state.inboxMessages.find((item) => item.id === selectedId) ||
      flags.saved[selectedId] ||
      (externalDetailMessage?.id === selectedId ? externalDetailMessage : null) ||
      null
    );
  }, [
    flags.saved,
    externalDetailMessage,
    selectedId,
    sourceMessages,
    state.inboxDraftMessages,
    state.inboxMessages,
    state.inboxSnapshotMessages,
  ]);

  useEffect(() => {
    if (!selectedMessage) return;
    if (drawerCloseTimer.current) {
      window.clearTimeout(drawerCloseTimer.current);
      drawerCloseTimer.current = null;
    }
    setDrawerMessage(selectedMessage);
    let enterFrame = 0;
    const mountFrame = window.requestAnimationFrame(() => {
      enterFrame = window.requestAnimationFrame(() => setDrawerOpen(true));
    });
    return () => {
      window.cancelAnimationFrame(mountFrame);
      window.cancelAnimationFrame(enterFrame);
    };
  }, [selectedMessage]);

  useEffect(() => {
    if (selectedMessage || !drawerMessage || !drawerOpen) return;
    setDrawerOpen(false);
    if (drawerCloseTimer.current) window.clearTimeout(drawerCloseTimer.current);
    drawerCloseTimer.current = window.setTimeout(() => {
      setDrawerMessage(null);
      drawerCloseTimer.current = null;
    }, DETAIL_DRAWER_TRANSITION_MS);
  }, [drawerMessage, drawerOpen, selectedMessage]);

  useEffect(() => () => {
    if (drawerCloseTimer.current) window.clearTimeout(drawerCloseTimer.current);
  }, []);

  const detailFlags = useMemo(
    () => ({
      todos: flags.todos,
      snoozed: flags.snoozed,
      done: [
        ...new Set([
          ...flags.done,
          ...sourceMessages
            .filter((message) => isDoneMessage(message, flags))
            .map((message) => message.id),
          ...(selectedMessage && isDoneMessage(selectedMessage, flags)
            ? [selectedMessage.id]
            : []),
        ]),
      ],
    }),
    [flags, selectedMessage, sourceMessages],
  );

  const latestSelectedThreadMessage = useMemo(() => {
    if (!selectedMessage) return null;
    const threadKey = selectedMessage.thread_id || selectedMessage.id;
    const candidates = [
      ...sourceMessages,
      ...state.inboxDraftMessages,
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
    return latest;
  }, [
    flags.saved,
    selectedId,
    selectedMessage,
    sourceMessages,
    state.inboxDraftMessages,
    state.inboxMessages,
    state.inboxSnapshotMessages,
  ]);

  const currentMailContext = useMemo<AiMailContextRef | null>(() => {
    if (!selectedMessage) return null;
    const detailMailbox = selectedMessage.mailbox || mailbox;
    const threadId = selectedMessage.thread_id || selectedMessage.id;
    return {
      kind: "gmail_thread",
      mailbox: detailMailbox,
      thread_id: threadId,
      anchor_message_id: selectedMessage.id,
      latest_message_id: latestSelectedThreadMessage?.id || selectedMessage.id,
    };
  }, [latestSelectedThreadMessage?.id, mailbox, selectedMessage]);

  const closeDetailDrawer = useCallback(() => {
    const currentId = selectedId;
    setDrawerOpen(false);
    setSelectedId("");
    setExternalDetailMessage(null);
    if (drawerCloseTimer.current) window.clearTimeout(drawerCloseTimer.current);
    drawerCloseTimer.current = window.setTimeout(() => {
      setDrawerMessage(null);
      drawerCloseTimer.current = null;
      const row = document.querySelector<HTMLButtonElement>(
        `[data-mail-row-id="${CSS.escape(currentId)}"]`,
      );
      row?.focus();
    }, DETAIL_DRAWER_TRANSITION_MS);
  }, [selectedId]);

  const openMessageDetail = useCallback(
    (message: InboxMessage) => {
      setExternalDetailMessage(null);
      setSelectedId(message.id);
      if (
        !(message.unread || hasMessageLabel(message, "UNREAD")) ||
        !message.thread_id
      )
        return;
      void actions
        .updateInboxThreadState(message.mailbox || mailbox, message.thread_id, "mark_read")
        .catch((reason) => {
          actions.showToast(
            reason instanceof Error ? reason.message : String(reason),
          );
        });
    },
    [actions, mailbox],
  );

  const openMailDetailFromAi = useCallback(async (target: AskMailLink) => {
    const targetMailbox = target.mailbox.trim().toLowerCase();
    if (!targetMailbox || !target.thread_id) {
      actions.showToast("This email reference is incomplete.");
      return;
    }
    try {
      if (targetMailbox !== mailbox.trim().toLowerCase()) {
        await actions.switchMailbox(targetMailbox);
      }
      const known = [
        ...state.inboxMessages,
        ...state.inboxSnapshotMessages,
        ...state.inboxDraftMessages,
        ...Object.values(flags.saved),
      ].find((item) =>
        (item.mailbox || targetMailbox).trim().toLowerCase() === targetMailbox &&
        (target.message_id ? item.id === target.message_id : (item.thread_id || item.id) === target.thread_id),
      );
      if (known) {
        setExternalDetailMessage(null);
        setSelectedId(known.id);
        return;
      }
      const page = await actions.loadInboxThreadPage(targetMailbox, target.thread_id, {
        anchorMessageId: target.message_id || undefined,
        limit: 5,
        includeDisplayBody: true,
      });
      const anchor = page.messages.find((item) => item.id === target.message_id)
        || page.messages.find((item) => item.id === page.latest_message_id)
        || page.messages.at(-1);
      if (!anchor) throw new Error("The referenced thread could not be loaded.");
      const placeholder: InboxMessage = {
        id: anchor.id,
        thread_id: page.thread_id || target.thread_id,
        mailbox: targetMailbox,
        internal_date: anchor.internal_date,
        from: anchor.from,
        to: anchor.to,
        subject: anchor.subject || page.subject,
        label_ids: anchor.label_ids || [],
        snippet: anchor.body_text || "",
      };
      setExternalDetailMessage(placeholder);
      setSelectedId(placeholder.id);
      setMailboxView("inbox");
    } catch (reason) {
      actions.showToast(reason instanceof Error ? reason.message : String(reason));
    }
  }, [actions, flags.saved, mailbox, state.inboxDraftMessages, state.inboxMessages, state.inboxSnapshotMessages]);

  const handleGmailThreadAction = useCallback(
    async (operation: InboxThreadStateOperation, message: InboxMessage) => {
      if (!message.thread_id) return;
      const reverse: Record<
        InboxThreadStateOperation,
        InboxThreadStateOperation
      > = {
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
        untrash: "Removed from trash.",
      };
      try {
        await actions.updateInboxThreadState(
          mailbox,
          message.thread_id,
          operation,
        );
        closeDetailDrawer();
        actions.showToast(notices[operation], {
          actionLabel: "Undo",
          onAction: () => {
            void actions
              .updateInboxThreadState(
                mailbox,
                message.thread_id || message.id,
                reverse[operation],
              )
              .catch((reason) => {
                actions.showToast(
                  reason instanceof Error ? reason.message : String(reason),
                );
              });
          },
        });
      } catch (reason) {
        actions.showToast(
          reason instanceof Error ? reason.message : String(reason),
        );
      }
    },
    [actions, closeDetailDrawer, mailbox],
  );

  const syncDoneMessagesRead = useCallback(
    async (messages: InboxMessage[]) => {
      const pendingThreads = [
        ...new Set(
          messages.reduce<string[]>((threads, message) => {
            if (
              (message.unread || hasMessageLabel(message, "UNREAD")) &&
              message.thread_id
            ) {
              threads.push(message.thread_id);
            }
            return threads;
          }, []),
        ),
      ];
      const pendingSingles = messages.filter(
        (message) =>
          (message.unread || hasMessageLabel(message, "UNREAD")) &&
          !message.thread_id,
      );
      const results = await Promise.allSettled([
        ...pendingThreads.map((threadId) =>
          actions.updateInboxThreadState(mailbox, threadId, "mark_read"),
        ),
        ...pendingSingles.map((message) => actions.markInboxRead(message.id)),
      ]);
      const failed = results.find((result) => result.status === "rejected");
      if (failed?.status === "rejected") {
        actions.showToast(
          failed.reason instanceof Error
            ? failed.reason.message
            : String(failed.reason),
        );
      }
    },
    [actions, mailbox],
  );

  const toggleDoneState = useCallback(
    (message: InboxMessage, closeAfter = false) => {
      const wasDone = isDoneMessage(message, flags);
      const previous = flags;
      const messages = messagesInThread(message);
      setWorkflowFlag("done", messages, !wasDone);
      if (!wasDone) {
        void syncDoneMessagesRead(messages);
      }
      if (closeAfter) {
        closeDetailDrawer();
      }
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
    },
    [
      actions,
      closeDetailDrawer,
      flags,
      messagesInThread,
      restoreWorkflowFlags,
      setWorkflowFlag,
      syncDoneMessagesRead,
    ],
  );

  const updateFlag = useCallback(
    (kind: CategoryFlag, message: InboxMessage) => {
      if (kind === "drafts") return;
      if (kind === "done") {
        toggleDoneState(message);
        return;
      }
      const messages = messagesInThread(message);
      const enabled = flags[kind].includes(message.id);
      if (kind === "todos") {
        if (enabled) return;
        const previous = flags;
        setWorkflowFlag("todos", messages, true);
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
        return;
      }
      setWorkflowFlag(kind, messages, !enabled);
    },
    [
      actions,
      flags,
      messagesInThread,
      restoreWorkflowFlags,
      setWorkflowFlag,
      toggleDoneState,
    ],
  );

  const handleTodoFromDetail = useCallback(
    (message: InboxMessage) => {
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
    },
    [
      actions,
      closeDetailDrawer,
      flags,
      messagesInThread,
      restoreWorkflowFlags,
      setWorkflowFlag,
    ],
  );

  const handleDoneFromDetail = useCallback(
    (message: InboxMessage) => {
      toggleDoneState(message, true);
    },
    [toggleDoneState],
  );

  const openSnoozePicker = useCallback(
    (message: InboxMessage) => {
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
    },
    [
      actions,
      flags,
      mailboxView,
      messagesInThread,
      restoreWorkflowFlags,
      setWorkflowFlag,
    ],
  );

  const submitSnooze = useCallback(
    (isoTime: string) => {
      const target = snoozeTarget;
      if (!target) return;
      const previous = flags;
      const messages = messagesInThread(target);
      setSnoozeTarget(null);
      setWorkflowFlag("snoozed", messages, true, isoTime);
      if (selectedId === target.id) {
        closeDetailDrawer();
      }
      actions.showToast(
        `Snoozed until ${new Date(isoTime).toLocaleString("en-US", {
          month: "short",
          day: "numeric",
          year: "numeric",
          hour: "numeric",
          minute: "2-digit",
        })}.`,
        {
          actionLabel: "Undo",
          onAction: () => restoreWorkflowFlags(messages, previous),
        },
      );
    },
    [
      actions,
      closeDetailDrawer,
      flags,
      messagesInThread,
      restoreWorkflowFlags,
      selectedId,
      setWorkflowFlag,
      snoozeTarget,
    ],
  );

  const selectMailboxView = (next: MailboxView) => {
    if (next === mailboxView) {
      setFolderOpen(false);
      return;
    }
    pageLoadInFlight.current = false;
    requestedGmailCursors.current.clear();
    mailboxViewRef.current = next;
    setMailboxView(next);
    setFolderOpen(false);
    setSelectedId("");
    setFilter("important");
    setFeedWindow(DEFAULT_INBOX_FEED_WINDOW);
    if (next === "done") {
      void actions
        .loadCachedInboxEmails("sent", 7, 0, false)
        .catch(() => undefined);
      return;
    }
    if (next === "drafts") {
      void actions
        .listInboxThreadDrafts(mailbox, 100)
        .catch(() => undefined);
      return;
    }
    if (isLocalMailboxView(next)) {
      return;
    }
    actions.resetInboxFeed();
    if (!isLocalMailboxView(next)) {
      void loadRemoteCategory(next, 7);
    }
  };

  const markTimelineDone = (messages: InboxMessage[]) => {
    const previous = flags;
    setWorkflowFlag("done", messages, true);
    void syncDoneMessagesRead(messages);
    actions.showToast("Marked as done.", {
      actionLabel: "Undo",
      onAction: () => restoreWorkflowFlags(messages, previous),
      secondaryActionLabel: "View",
      onSecondaryAction: () => {
        setMailboxView("done");
        setFolderOpen(false);
        setSelectedId("");
      },
    });
  };

  return (
    <div
      className={`inbox-home ${sidebarCollapsed ? "is-ai-collapsed" : ""} ${sidebarResizing ? "is-resizing-ai-sidebar" : ""}`}
      style={layoutStyle}
    >
      <AiSidebar
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed((value) => !value)}
        currentMailContext={currentMailContext}
        onUseArtifact={(artifact, mode) =>
          setInsertRequest({ nonce: crypto.randomUUID(), artifact, mode })
        }
        onOpenMail={(target) => void openMailDetailFromAi(target)}
      />
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
      <main
        className={`mail-workspace ${mailboxView === "inbox" ? "" : "is-folder-view"}`}
      >
        <header className="mail-topbar">
          <div className="mailbox-picker">
            <button
              className="mailbox-title"
              aria-expanded={folderOpen}
              onClick={() => setFolderOpen((open) => !open)}
            >
              <span className={`mailbox-current-icon is-${mailboxView}`}>
                <FolderIcon view={mailboxView} />
              </span>
              <div>
                <strong>
                  {MAILBOX_VIEWS.find((item) => item.id === mailboxView)?.label}
                </strong>
                <span>{mailbox || "Gmail"}</span>
              </div>
              <span className="mailbox-picker-chevron">
                <ChevronDownIcon />
              </span>
            </button>
            {folderOpen ? (
              <button
                className="mailbox-picker-backdrop"
                aria-label="Close folders"
                onClick={() => setFolderOpen(false)}
              />
            ) : null}
            <div
              className={`mailbox-picker-menu ${folderOpen ? "is-open" : ""}`}
            >
              {MAILBOX_VIEWS.map((item) => (
                <button
                  key={item.id}
                  className={mailboxView === item.id ? "is-active" : ""}
                  onClick={() => selectMailboxView(item.id)}
                >
                  <span className={`folder-glyph is-${item.id}`}>
                    <FolderIcon view={item.id} />
                  </span>
                  <span>{item.label}</span>
                  {mailboxView === item.id ? <CheckIcon /> : null}
                </button>
              ))}
            </div>
          </div>
          <label className="mail-search">
            <SearchIcon />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search in this catagory"
            />
          </label>
          <button
            className={`refresh-mail-btn ${isInboxSyncing ? "is-syncing" : ""}`}
            disabled={isInboxSyncing}
            onClick={() => setRefreshChoiceOpen(true)}
          >
            <RefreshIcon />
            <span>{isInboxSyncing ? "Syncing" : "Refresh"}</span>
          </button>
        </header>

        {refreshChoiceOpen ? (
          <div className="confirm-overlay refresh-choice-overlay" role="presentation">
            <section
              className="confirm-dialog refresh-choice-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="refresh-choice-title"
            >
              <h3 id="refresh-choice-title">Refresh inbox</h3>
              <p>Choose how Anna should refresh the current mailbox cache and view.</p>
              <div className="refresh-choice-actions">
                <button
                  className="danger-btn"
                  type="button"
                  disabled={isInboxSyncing}
                  onClick={() => void reloadInboxFromEmpty()}
                >
                  Clear cache and reload
                </button>
                <button
                  type="button"
                  disabled={isInboxSyncing}
                  onClick={() => void continueLoadingInbox()}
                >
                  Continue loading new mail
                </button>
                <button type="button" onClick={() => setRefreshChoiceOpen(false)}>
                  Cancel
                </button>
              </div>
            </section>
          </div>
        ) : null}

        {mailboxView === "inbox" ? (
          <nav className="mail-tabs" aria-label="Inbox filters">
            {(
              [
                ["important", "Important"],
                ["other", "Other"],
              ] as Array<[FeedFilter, string]>
            ).map(([key, label]) => (
              <button
                key={key}
                className={filter === key ? "is-active" : ""}
                onClick={() => setFilter(key)}
              >
                {label}
                <span>{counts[key]}</span>
              </button>
            ))}
            <p>{inboxRangeLabel(days)}</p>
          </nav>
        ) : null}

        <section
          className={`mail-feed ${mailboxView === "trash" ? "is-trash-view" : ""}`}
          aria-live="polite"
          ref={mailFeedRef}
          onScroll={handleFeedScroll}
        >
          {state.inboxError &&
          sourceMessages.length > 0 &&
          !cachedInboxBannerDismissed ? (
            <div className="mail-sync-banner">
              <span>Showing cached inbox.</span>
              <div className="mail-sync-banner-actions">
                <button
                  className="mail-sync-banner-skip"
                  onClick={dismissCachedInboxBanner}
                >
                  Skip
                </button>
                <button
                  onClick={() => void syncInbox(days)}
                  disabled={isInboxSyncing}
                >
                  Retry sync
                </button>
              </div>
            </div>
          ) : null}
          {gmailAuthorizationRequired ? (
            <div
              className="mail-empty mail-auth-guide"
              aria-label="Gmail authorization required"
            >
              <InboxIcon />
              <h2>Connect your Gmail account</h2>
              <p>Authorize Anna to read and manage your inbox.</p>
              <div className="auth-guide-card">
                <div className="auth-guide-steps">
                  <div className="auth-step">
                    <span className="auth-step-num">1</span>
                    <span>Open <strong>More → Authorizations</strong></span>
                  </div>
                  <div className="auth-step">
                    <span className="auth-step-num">2</span>
                    <span>Select <strong>Google</strong> → <strong>Connect with OAuth</strong></span>
                  </div>
                  <div className="auth-step">
                    <span className="auth-step-num">3</span>
                    <span>Tick Gmail Read/Modify/Compose/Send</span>
                  </div>
                  <div className="auth-step">
                    <span className="auth-step-num">4</span>
                    <span>Click <strong>Authorize</strong> and return here</span>
                  </div>
                </div>
              </div>
              <button
                disabled={authChecking}
                onClick={() => void checkGmailAuthorization()}
              >
                {authChecking ? "Checking..." : "Check again"}
              </button>
              <p className="assistant-copy is-error mail-auth-error">
                {gmailAuthorizationError(state.gmailAuthStatus.source)}
              </p>
            </div>
          ) : !localCategory && state.inboxLoading && !sourceMessages.length ? (
            <div className="mail-loading">
              {[1, 2, 3, 4, 5, 6].map((item) => (
                <span key={item} />
              ))}
            </div>
          ) : !localCategory && state.inboxError && !sourceMessages.length ? (
            <div className="mail-empty">
              <InboxIcon />
              <h2>We couldn’t load Gmail</h2>
              <p>{state.inboxError}</p>
              <button
                onClick={() => void syncInbox(days)}
                disabled={isInboxSyncing}
              >
                Try again
              </button>
            </div>
          ) : !visible.length ? (
            mailboxView !== "inbox" && mailboxView !== "all" ? (
              <div className="mail-empty is-category-empty">
                <SearchIcon />
                <h2>No matching results</h2>
              </div>
            ) : (
              <div className="mail-empty">
                <InboxIcon />
                <h2>No messages here</h2>
                <p>
                  {search
                    ? "Try a different search."
                    : `This filter is clear for the last ${days} days.`}
                </p>
              </div>
            )
          ) : (
            grouped.map((group) => (
              <div className="mail-group" key={group.label}>
                {mailboxView === "inbox" ? (
                  <div className="mail-group-label">
                    <span>{group.label}</span>
                    <i />
                    <button
                      title="Mark this timeline as done"
                      onClick={() => markTimelineDone(group.messages)}
                    >
                      <AllDoneIcon />
                    </button>
                  </div>
                ) : null}
                {group.messages.map((message) => {
                  const avatarEmail = messageParticipant(
                    message,
                    mailboxView,
                    mailbox,
                  ).email.toLowerCase();
                  return (
                    <InboxRow
                      key={message.id}
                      message={message}
                      mailboxView={mailboxView}
                      mailbox={mailbox}
                      flags={flags}
                      selected={selectedId === message.id}
                      avatarUrl={contactAvatars[avatarEmail]}
                      onPrefetch={() => void prefetchMessageBody(message)}
                      onFlag={updateFlag}
                      onThreadAction={(operation, message) => void handleGmailThreadAction(operation, message)}
                      onSnooze={openSnoozePicker}
                      onSelect={() => openMessageDetail(message)}
                    />
                  );
                })}
              </div>
            ))
          )}
          {days === 7 &&
          !state.inboxLoading &&
          feedAction !== "older" &&
          mailboxView === "inbox" ? (
            <button
              className="older-mail-btn"
              onClick={() => void loadOlderInbox()}
              disabled={isInboxSyncing || feedAction !== null}
            >
              Show emails older than 7 days
            </button>
          ) : null}
          {canLoadMoreInbox ? (
            <button
              className="older-mail-btn"
              onClick={() => void loadMoreInbox()}
              disabled={isInboxSyncing || feedAction !== null}
            >
              {feedAction === "more"
                ? "Loading older emails..."
                : "Show emails older than 1 month"}
            </button>
          ) : null}
          {mailboxView === "trash" ? (
            <footer className="trash-retention-notice">
              <p>Trash is deleted after 30 days.</p>
              <p>
                To empty your trash now, {" "}
                <button
                  type="button"
                  onClick={() => {
                    void copyTextToClipboard(gmailTrashUrl(mailbox))
                      .then(() => actions.showToast("Gmail link copied. Paste it into your browser."))
                      .catch(() => actions.showToast("Could not copy the Gmail link."));
                  }}
                >
                  copy Gmail link
                </button>
                .
              </p>
            </footer>
          ) : null}
        </section>
        <MailDetailDrawer
          key={`${drawerMessage?.mailbox || mailbox}:${drawerMessage?.id || "closed"}`}
          open={drawerOpen}
          mailbox={drawerMessage?.mailbox || mailbox}
          message={drawerMessage}
          flags={detailFlags}
          aiBusy={state.aiChatLoading || state.isCustomScanning}
          insertRequest={insertRequest}
          onConsumeInsertRequest={(nonce) =>
            setInsertRequest((current) =>
              current?.nonce === nonce ? null : current,
            )
          }
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
          submitMailContextPrompt={(request) => {
            setSidebarCollapsed(false);
            return actions.submitMailContextPrompt(request);
          }}
          sendInboxThreadReply={actions.sendInboxThreadReply}
          contactAvatars={contactAvatars}
          loadContactAvatars={actions.loadContactAvatars}
          latestThreadMessageId={latestSelectedThreadMessage?.id || ""}
          latestThreadInternalDate={latestSelectedThreadMessage?.internal_date || ""}
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
