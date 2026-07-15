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
  AiComposeContextRef,
  AiMailContextRef,
  AskMailLink,
  ComposeDraftArtifact,
  ComposeDraft,
  DraftReplyArtifact,
  SendPlanArtifact,
  CustomRunResult,
  InboxMessage,
  InboxThreadStateOperation,
} from "../../types/mail";
import { SnoozePicker } from "./SnoozePicker";
import { ComposeView } from "./ComposeView";
import { MailDetailDrawer } from "../mail-detail/MailDetailDrawer";
import {
  groupSplitMessages,
  splitInboxMessages,
  splitImportantMessages,
} from "../settings/inboxSettings";
import { SplitsManager } from "../settings/SplitsManager";
import {
  applyInboxQuerySuggestion,
  getInboxQuerySuggestionPlaceholder,
  getInboxQuerySuggestions,
  matchInboxQuery,
  parseInboxQuery,
  splitInboxQueryTokens,
} from "../search/inboxQuery";
import { sortInboxMessagesDesc } from "./inboxMessageOrder";
import {
  parseAiMessageInline,
  parseAiMessageMarkdown,
  type AiMessageInline,
} from "./aiMessageFormatting";
import { customExecutionSteps } from "../brief/runHelpers";
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

type FeedFilter = "important" | "other" | "search" | `category:${string}`;
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
type FeedActionState = "refresh" | "more" | "category-page" | null;
type CachedInboxRetryAction = "sync" | "load-more";

const AI_SIDEBAR_WIDTH_KEY = "anna-inbox:ai-sidebar-width";
const AI_SIDEBAR_MIN_WIDTH = 300;
const AI_SIDEBAR_MAX_WIDTH = 580;
const DESKTOP_MAIL_WORKSPACE_MIN_WIDTH = 420;
const COMPACT_MAIL_WORKSPACE_MIN_WIDTH = 160;
const DESKTOP_ACCOUNT_RAIL_WIDTH = 54;
const COMPACT_ACCOUNT_RAIL_WIDTH = 48;
const CACHED_INBOX_BANNER_SKIP_KEY = "anna-inbox:cached-inbox-banner-skip";
const INBOX_ALL_TIME_DAYS = 0;
const INBOX_LAST_MONTH_DAYS = 30;
const DEFAULT_INBOX_FEED_WINDOW: InboxFeedWindow = {
  days: 30,
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
  return (
    view === "todos" ||
    view === "snoozed" ||
    view === "done" ||
    view === "drafts"
  );
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
  const viewportWidth =
    document.documentElement.clientWidth || window.innerWidth;
  const compact = viewportWidth < 900;
  const compactMin = viewportWidth < 520 ? 180 : AI_SIDEBAR_MIN_WIDTH;
  const workspaceMin = compact
    ? COMPACT_MAIL_WORKSPACE_MIN_WIDTH
    : DESKTOP_MAIL_WORKSPACE_MIN_WIDTH;
  const accountRailWidth = compact
    ? COMPACT_ACCOUNT_RAIL_WIDTH
    : DESKTOP_ACCOUNT_RAIL_WIDTH;
  const max = Math.max(
    compactMin,
    Math.min(
      AI_SIDEBAR_MAX_WIDTH,
      viewportWidth - workspaceMin - accountRailWidth,
    ),
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
export const PlusIcon = () => (
  <Icon>
    <path d="M12 5v14M5 12h14" />
  </Icon>
);
const ComposeIcon = () => (
  <Icon>
    <path d="M4 20h4l10.5-10.5a2.8 2.8 0 0 0-4-4L4 16v4Z" />
    <path d="m13.5 6.5 4 4" />
  </Icon>
);
const HistoryIcon = () => (
  <Icon>
    <path d="M4 5v5h5" />
    <path d="M5 10a8 8 0 1 1 1.4 6.4" />
    <path d="M12 8v5l3 2" />
  </Icon>
);
const SettingsIcon = () => (
  <Icon>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4v-.2a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1-2.8-2.8.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3v-4h.2a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1L7.2 4l.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.6V2.6h4v.2a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L20 6.8l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1.2Z" />
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
  const recipientSource =
    draft &&
    normalizedMailbox &&
    sender.email.toLowerCase() !== normalizedMailbox
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
      title: String(
        recipientSource || message.from || "Draft without recipients",
      ),
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
  const days = Math.round(
    (target.getTime() - today.getTime()) / (24 * 60 * 60 * 1000),
  );
  const dayLabel =
    days === 0
      ? "Today"
      : days === 1
        ? "Tomorrow"
        : date.toLocaleDateString(
            "en-US",
            days > 1 && days < 7
              ? { weekday: "short" }
              : { month: "short", day: "numeric" },
          );
  const timeLabel = `${date.getHours()}:${String(date.getMinutes()).padStart(2, "0")}`;
  return `${dayLabel} ${timeLabel}`;
}

function groupLabel(
  message: InboxMessage,
  mode: "detailed" | "recent_then_months" | "months_only" = "detailed",
) {
  const date = messageDate(message);
  if (!date) return "LAST 30 DAYS";
  const now = new Date();
  if (mode === "detailed" && date.toDateString() === now.toDateString())
    return "TODAY";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (mode === "detailed" && date.toDateString() === yesterday.toDateString())
    return "YESTERDAY";
  const daysAgo = Math.floor(
    (now.getTime() - date.getTime()) / (24 * 60 * 60 * 1000),
  );
  if (daysAgo < 7 && mode !== "months_only")
    return mode === "detailed" ? "LAST 7 DAYS" : "LAST 7 DAYS";
  if (mode !== "months_only" && daysAgo < INBOX_LAST_MONTH_DAYS)
    return "EARLIER THIS MONTH";
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
  if (days === INBOX_LAST_MONTH_DAYS) return "Last 30 days";
  return `Last ${days} days`;
}

function inboxLastSyncedLabel(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const time = date.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
  });
  const datePrefix =
    date.toDateString() === now.toDateString()
      ? ""
      : `${date.toLocaleDateString("en-US", { month: "short", day: "numeric" })}, `;
  return `Last synced: ${datePrefix}${time}`;
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
  const mailboxKey = String(message.mailbox || "")
    .trim()
    .toLowerCase();
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
      body_preview:
        draft.draft_body || draft.body_preview || message.body_preview,
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
        .filter(
          (message): message is InboxMessage =>
            message !== undefined && !isTrashMessage(message),
        ),
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
        .filter(
          (message): message is InboxMessage =>
            message !== undefined && !isTrashMessage(message),
        ),
    );
  }
  if (mailboxView === "inbox") {
    return uniqueLatestInboxThreads(
      inboxMessages.filter(
        (message) =>
          hasMessageLabel(message, "INBOX") && !isTrashMessage(message),
      ),
    );
  }
  if (mailboxView === "starred")
    return uniqueLatestInboxThreads(
      source.filter(
        (message) =>
          hasMessageLabel(message, "STARRED") && !isTrashMessage(message),
      ),
    );
  if (mailboxView === "drafts")
    return uniqueLatestInboxThreads(
      source.filter(
        (message) => isDraftMessage(message) && !isTrashMessage(message),
      ),
    );
  if (mailboxView === "sent")
    return uniqueLatestInboxThreads(
      source.filter(
        (message) =>
          hasMessageLabel(message, "SENT") && !isTrashMessage(message),
      ),
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
      source.filter(
        (message) =>
          hasMessageLabel(message, "SPAM") && !isTrashMessage(message),
      ),
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
  const attachments = (message as InboxMessage & { attachments?: unknown[] })
    .attachments;
  return Boolean(
    message.has_attachment ||
    Number(message.attachment_count || 0) > 0 ||
    (Array.isArray(attachments) && attachments.length > 0),
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
  selectable = false,
  selectedForBatch = false,
  onBatchToggle,
  onComposeDraftDelete,
}: {
  message: InboxMessage;
  selected: boolean;
  flags: MailUiFlags;
  mailboxView: MailboxView;
  mailbox: string;
  onSelect: () => void;
  onFlag: (kind: CategoryFlag, message: InboxMessage) => void;
  onThreadAction: (
    operation: InboxThreadStateOperation,
    message: InboxMessage,
  ) => void;
  onSnooze: (message: InboxMessage) => void;
  onPrefetch: () => void;
  avatarUrl?: string;
  selectable?: boolean;
  selectedForBatch?: boolean;
  onBatchToggle?: () => void;
  onComposeDraftDelete?: () => void;
}) {
  const { actions } = useApp();
  const [avatarFailed, setAvatarFailed] = useState(false);
  const participant = messageParticipant(message, mailboxView, mailbox);
  const fallbackAvatar = mailAvatarFallback(
    participant.name || participant.email || participant.initial,
    participant.name || participant.initial,
  );
  const sentView = participant.outgoing;
  const sentMessage = isSentMessage(message);
  const isDone = isDoneMessage(message, flags);
  const isTodo = flags.todos.includes(message.id);
  const isSnoozed = flags.snoozed.includes(message.id);
  const trashed = isTrashMessage(message);
  const snoozeLabel =
    !trashed && mailboxView === "snoozed"
      ? snoozeUntilLabel(flags.snoozedUntil?.[message.id])
      : "";
  const important = isImportantMessage(message);
  const starred = isStarredMessage(message);
  const draft = isDraftMessage(message);
  const isComposeDraft = draft && message.id.startsWith("compose:");
  const preview =
    (draft ? message.draft_body : "") ||
    message.snippet ||
    message.body_preview ||
    "No preview available";
  return (
    <article
      className={`mail-row ${mailboxView === "all" ? "is-all-mail" : ""} ${message.unread ? "is-unread" : ""} ${selected ? "is-selected" : ""}`}
    >
      {selectable ? (
        <button
          type="button"
          className="draft-select"
          aria-label={`Select ${message.subject || "draft"}`}
          aria-pressed={selectedForBatch}
          onClick={onBatchToggle}
        >
          {selectedForBatch ? "✓" : ""}
        </button>
      ) : null}
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
          <span className={`sender-avatar tone-${fallbackAvatar.tone}`}>
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
          <span
            className="mail-snooze-until"
            title={`Snoozed until ${snoozeLabel}`}
          >
            <ClockIcon />
            <span>{snoozeLabel}</span>
          </span>
        ) : (
          <time>{dateLabel(message)}</time>
        )}
      </button>
      <span className="mail-row-actions">
        {isComposeDraft ? (
          <button
            aria-label="Delete draft"
            data-tooltip="Delete draft"
            onClick={onComposeDraftDelete}
          >
            <TrashIcon />
          </button>
        ) : trashed ? (
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
              onClick={() =>
                onThreadAction(
                  important ? "mark_not_important" : "mark_important",
                  message,
                )
              }
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
                sentMessage
                  ? "Sent and done"
                  : isDone
                    ? "Move to inbox"
                    : "Done"
              }
              data-tooltip={
                sentMessage
                  ? "Sent and done"
                  : isDone
                    ? "Move to inbox"
                    : "Done"
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
  const chinese = /[\u3400-\u9fff]/.test(
    [
      result.title,
      result.summary,
      ...(result.sections || []).flatMap((section) => [
        section.heading,
        section.body,
      ]),
    ]
      .filter(Boolean)
      .join(" "),
  );
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
          <AiMessageInlineContent
            content={parseAiMessageInline(node.label)}
            onOpenThread={onOpenThread}
          />
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
          const Heading = `h${block.level + 2}` as "h3" | "h4" | "h5";
          return (
            <Heading key={key}>
              <AiMessageInlineContent
                content={block.content}
                onOpenThread={onOpenThread}
              />
            </Heading>
          );
        }
        if (block.type === "unordered_list") {
          return (
            <ul key={key}>
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>
                  <AiMessageInlineContent
                    content={item}
                    onOpenThread={onOpenThread}
                  />
                </li>
              ))}
            </ul>
          );
        }
        if (block.type === "ordered_list") {
          return (
            <ol key={key}>
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>
                  <AiMessageInlineContent
                    content={item}
                    onOpenThread={onOpenThread}
                  />
                </li>
              ))}
            </ol>
          );
        }
        return (
          <p key={key}>
            <AiMessageInlineContent
              content={block.content}
              onOpenThread={onOpenThread}
            />
          </p>
        );
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
  onUseComposeArtifact,
  onOpenMail,
  onConfirmSendPlan,
}: {
  message: AiChatMessage;
  currentMailContext: AiMailContextRef | null;
  onUseArtifact: (
    artifact: DraftReplyArtifact,
    mode: "append" | "replace",
  ) => void;
  onUseComposeArtifact: (
    artifact: ComposeDraftArtifact,
    context: AiComposeContextRef | null,
  ) => void;
  onOpenMail: (target: AskMailLink) => void;
  onConfirmSendPlan: (plan: SendPlanArtifact) => void;
}) {
  const { state, actions } = useApp();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submittingGap, setSubmittingGap] = useState(false);
  const [clarificationInput, setClarificationInput] = useState("");
  const [assistantTextComplete, setAssistantTextComplete] = useState(
    () => !shouldAnimateAssistantText(message.timestamp),
  );
  // 整理确认卡片：勾选状态与主动作
  const proposed = message.proposedActions;
  const [selectedProposeKeys, setSelectedProposeKeys] = useState<Set<string>>(() => {
    const next = new Set<string>();
    for (const item of proposed?.items || []) {
      if (item.default_selected !== false) {
        next.add(`${item.mailbox}|${item.message_id || item.thread_id}`);
      }
    }
    return next;
  });
  const [proposeAction, setProposeAction] = useState(proposed?.primary_action || "mark_done");
  const [proposeBusy, setProposeBusy] = useState(false);
  const [proposeResolved, setProposeResolved] = useState<"none" | "applied" | "skipped">("none");
  const [appliedCount, setAppliedCount] = useState(0);
  /** none | choose（展示 Yes/Not now）| dismissed（Not now 后仍展示收尾建议） */
  const [followupPhase, setFollowupPhase] = useState<"hidden" | "choose" | "dismissed">("hidden");
  const proposeZh = Boolean(
    proposed?.language === "zh"
    || /[\u3400-\u9fff]/.test(message.content || "")
    || /[\u3400-\u9fff]/.test(proposed?.followup_after_apply || "")
    || /[\u3400-\u9fff]/.test(proposed?.step_title || ""),
  );
  const proposeLabels = proposeZh
    ? {
        skip: "跳过",
        markDone: "标为已处理",
        archive: "归档",
        trash: "移到垃圾箱",
        markDoneQ: (n: number) => `将 ${n} 封标为已处理？`,
        archiveQ: (n: number) => `归档 ${n} 封邮件？`,
        trashQ: (n: number) => `将 ${n} 封移到垃圾箱？`,
        marked: (n: number) => `已将 ${n} 封标为已处理`,
        archived: (n: number) => `已归档 ${n} 封`,
        trashed: (n: number) => `已将 ${n} 封移到垃圾箱`,
        skipped: "已跳过本批",
        yesContinue: "是，继续",
        notNow: "暂不",
        continueFallback: "继续整理剩余邮件",
        followApply: "本批已处理。需要我继续整理剩余邮件吗？",
        followSkip: "已跳过本批。需要我继续为剩余邮件生成建议吗？",
        followDismiss: "好的。我仍建议你关注下方这些可整理邮件。之后可以说「继续整理」。",
      }
    : {
        skip: "Skip",
        markDone: "Mark done",
        archive: "Archive",
        trash: "Move to trash",
        markDoneQ: (n: number) => `Mark ${n} threads as done?`,
        archiveQ: (n: number) => `Archive ${n} threads?`,
        trashQ: (n: number) => `Move ${n} threads to trash?`,
        marked: (n: number) => `Marked ${n} threads as done`,
        archived: (n: number) => `Archived ${n} threads`,
        trashed: (n: number) => `Moved ${n} threads to trash`,
        skipped: "Skipped this batch",
        yesContinue: "Yes, continue",
        notNow: "Not now",
        continueFallback: "Continue organizing the remaining emails",
        followApply: "Would you like me to continue organizing the remaining emails?",
        followSkip: "Would you like me to continue with suggestions for the remaining emails?",
        followDismiss: "Okay. I still recommend reviewing the emails listed below. You can say “continue organizing” anytime.",
      };
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
    const executionSteps = message.kind === "scan" && state.customRunProgress
      ? customExecutionSteps(state.customRunProgress.stage, state.customRunProgress.progress)
      : [];
    return (
      <div
        className="ai-message is-assistant is-thinking-inline"
        aria-live="polite"
        aria-busy="true"
      >
        {executionSteps.length ? executionSteps.map((step) => (
          <p key={step.label}>{step.status === "complete" ? "Completed: " : step.status === "active" ? "In progress: " : ""}{step.label}</p>
        )) : <p>Thinking...</p>}
      </div>
    );
  }

  const result = message.result;
  if (!result) {
    const text = displayAssistantText(message);
    const animate = shouldAnimateAssistantText(message.timestamp);
    const draftArtifact =
      message.artifact?.type === "draft_reply" &&
      (!animate || assistantTextComplete)
        ? message.artifact
        : null;
    const composeArtifact =
      message.artifact?.type === "compose_draft" &&
      (!animate || assistantTextComplete)
        ? message.artifact
        : null;
    const sendPlan =
      message.artifact?.type === "send_plan" &&
      (!animate || assistantTextComplete)
        ? message.artifact
        : null;
    const summaryLink = message.mailSummaryLink;
    const clarification = message.clarification;
    const targetThreadOpen = Boolean(
      draftArtifact &&
      currentMailContext &&
      currentMailContext.kind === "gmail_thread" &&
      currentMailContext.mailbox.trim().toLowerCase() ===
        draftArtifact.mailbox.trim().toLowerCase() &&
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
        if (message.mailContext.kind === "compose") {
          const details = message.replyGaps.questions
            .map(
              (question) =>
                `- ${question.question}: ${String(answers[question.id] || "").trim()}`,
            )
            .join("\n");
          await actions.submitMailContextPrompt({
            visiblePrompt: `Create a complete revised draft using these details:\n${details}`,
            context: message.mailContext,
            expectedArtifact: "compose_draft",
          });
          return;
        }
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
                    onChange={(event) =>
                      setClarificationInput(event.target.value)
                    }
                  />
                ) : null}
                <div className="ai-clarification-actions">
                  {clarification.actions.map((action) => (
                    <button
                      key={action.id}
                      onClick={() =>
                        void actions.sendAiChatMessage({
                          currentMailContext,
                          forcedKind: action.id,
                          prompt:
                            clarificationInput.trim() ||
                            clarification.original_input,
                          clarificationMessageId: message.id,
                        })
                      }
                    >
                      {action.label}
                    </button>
                  ))}
                  <button
                    className="is-dismiss"
                    onClick={() => actions.dismissAiClarification(message.id)}
                  >
                    Cancel
                  </button>
                </div>
              </>
            ) : (
              <span>
                {clarification.status === "dismissed"
                  ? "Dismissed"
                  : "Resolved"}
              </span>
            )}
          </div>
        ) : null}
        {proposed ? (
          <div className={`ai-propose-card ${proposeResolved !== "none" ? "is-resolved" : ""}`}>
            <strong>
              {proposeResolved === "applied"
                ? (proposeAction === "trash"
                  ? proposeLabels.trashed(appliedCount)
                  : proposeAction === "archive"
                    ? proposeLabels.archived(appliedCount)
                    : proposeLabels.marked(appliedCount))
                : proposeResolved === "skipped"
                  ? proposeLabels.skipped
                  : proposeAction === "trash"
                    ? proposeLabels.trashQ(selectedProposeKeys.size)
                    : proposeAction === "archive"
                      ? proposeLabels.archiveQ(selectedProposeKeys.size)
                      : proposeLabels.markDoneQ(selectedProposeKeys.size)}
            </strong>
            {proposed.step_title ? <p className="ai-propose-step">{proposed.step_title}</p> : null}
            {proposeResolved === "none" && proposed.rationale ? (
              <p className="ai-propose-rationale">{proposed.rationale}</p>
            ) : null}
            <ul className="ai-propose-list">
              {(proposed.items || []).map((item) => {
                const key = `${item.mailbox}|${item.message_id || item.thread_id}`;
                const wasSelected = selectedProposeKeys.has(key);
                return (
                  <li key={key} className={proposeResolved === "applied" && wasSelected ? "is-done" : ""}>
                    <label>
                      {proposeResolved === "none" ? (
                        <input
                          type="checkbox"
                          checked={wasSelected}
                          onChange={() => {
                            setSelectedProposeKeys((prev) => {
                              const next = new Set(prev);
                              if (next.has(key)) next.delete(key);
                              else next.add(key);
                              return next;
                            });
                          }}
                        />
                      ) : (
                        <span className="ai-propose-check" aria-hidden="true">
                          {proposeResolved === "applied" && wasSelected ? "✓" : "·"}
                        </span>
                      )}
                      <button
                        type="button"
                        className="ai-propose-link"
                        onClick={() =>
                          onOpenMail({
                            label: item.subject || "Thread",
                            mailbox: item.mailbox,
                            thread_id: item.thread_id,
                            message_id: item.message_id,
                          })
                        }
                      >
                        {item.subject || item.thread_id || item.message_id || "Thread"}
                      </button>
                    </label>
                  </li>
                );
              })}
            </ul>
            {proposeResolved === "none" ? (
              <div className="ai-draft-artifact-actions">
                <button
                  className="is-secondary"
                  disabled={proposeBusy}
                  onClick={() => {
                    setProposeResolved("skipped");
                    setFollowupPhase("choose");
                  }}
                >
                  {proposeLabels.skip}
                </button>
                <select
                  value={proposeAction}
                  disabled={proposeBusy}
                  onChange={(event) => setProposeAction(event.target.value)}
                  aria-label={proposeZh ? "整理动作" : "Organize action"}
                >
                  {(proposed.allowed_actions || ["mark_done", "archive", "trash"]).map((actionId) => (
                    <option key={actionId} value={actionId}>
                      {actionId === "trash"
                        ? proposeLabels.trash
                        : actionId === "archive"
                          ? proposeLabels.archive
                          : proposeLabels.markDone}
                    </option>
                  ))}
                </select>
                <button
                  className="is-primary"
                  disabled={proposeBusy || selectedProposeKeys.size === 0}
                  onClick={() => {
                    void (async () => {
                      setProposeBusy(true);
                      try {
                        const items = (proposed.items || []).filter((item) =>
                          selectedProposeKeys.has(`${item.mailbox}|${item.message_id || item.thread_id}`),
                        );
                        const result = await actions.applyProposedActions({
                          action: proposeAction,
                          items,
                        });
                        if (result.success !== false) {
                          setAppliedCount(items.length);
                          setProposeResolved("applied");
                          setFollowupPhase("choose");
                        }
                      } finally {
                        setProposeBusy(false);
                      }
                    })();
                  }}
                >
                  {proposeAction === "trash"
                    ? proposeLabels.trash
                    : proposeAction === "archive"
                      ? proposeLabels.archive
                      : proposeLabels.markDone}
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
        {proposed && proposeResolved !== "none" && followupPhase !== "hidden" ? (
          <div className="ai-propose-followup">
            <RichAssistantText
              text={
                followupPhase === "dismissed"
                  ? (proposed.followup_after_dismiss || proposeLabels.followDismiss)
                  : proposeResolved === "applied"
                    ? (proposed.followup_after_apply || proposeLabels.followApply)
                    : (proposed.followup_after_skip || proposeLabels.followSkip)
              }
              onOpenThread={openThreadReference}
            />
            {(proposed.recommendation_groups || []).map((group) => {
              const linkItems = (group.items && group.items.length
                ? group.items
                : (proposed.items || []).map((item) => ({
                    mailbox: item.mailbox,
                    message_id: item.message_id,
                    thread_id: item.thread_id,
                    subject: item.subject || "",
                  }))
              ).filter((item) => item.subject || item.thread_id || item.message_id);
              if (!linkItems.length && !(group.subjects || []).length) return null;
              return (
                <div key={group.title} className="ai-propose-reco-group">
                  <strong>{group.title}</strong>
                  <ul className="ai-propose-reco-list">
                    {linkItems.length
                      ? linkItems.slice(0, 12).map((item) => (
                        <li key={`${item.mailbox}|${item.message_id || item.thread_id}`}>
                          <button
                            type="button"
                            className="ai-propose-link"
                            onClick={() =>
                              onOpenMail({
                                label: item.subject || "Thread",
                                mailbox: item.mailbox,
                                thread_id: item.thread_id,
                                message_id: item.message_id,
                              })
                            }
                          >
                            {item.subject || item.thread_id || item.message_id}
                          </button>
                        </li>
                      ))
                      : (group.subjects || []).slice(0, 12).map((subject) => (
                        <li key={subject}>{subject}</li>
                      ))}
                  </ul>
                </div>
              );
            })}
            {followupPhase === "choose" ? (
              <div className="ai-draft-artifact-actions">
                <button
                  className="is-primary"
                  onClick={() => {
                    setFollowupPhase("dismissed");
                    void actions.sendAiChatMessage({
                      currentMailContext,
                      prompt: proposed.continue_prompt || proposeLabels.continueFallback,
                    });
                  }}
                >
                  {proposeLabels.yesContinue}
                </button>
                <button
                  className="is-secondary"
                  onClick={() => setFollowupPhase("dismissed")}
                >
                  {proposeLabels.notNow}
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
        {draftArtifact ? (
          <div className="ai-draft-artifact">
            <pre>{draftArtifact.body}</pre>
            <div className="ai-draft-artifact-actions">
              {targetThreadOpen ? (
                <>
                  <button
                    className="is-primary"
                    onClick={() => onUseArtifact(draftArtifact, "append")}
                  >
                    Append to draft reply
                  </button>
                  <button
                    className="is-secondary"
                    onClick={() => onUseArtifact(draftArtifact, "replace")}
                  >
                    Replace draft reply
                  </button>
                </>
              ) : (
                <button
                  className="is-primary"
                  onClick={() =>
                    onOpenMail({
                      label: "Draft thread",
                      mailbox: draftArtifact.mailbox,
                      thread_id: draftArtifact.thread_id,
                      message_id:
                        message.mailContext?.kind === "gmail_thread"
                          ? message.mailContext.latest_message_id ||
                            message.mailContext.anchor_message_id
                          : "",
                    })
                  }
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
        {composeArtifact ? (
          <div className="ai-draft-artifact">
            <pre>{composeArtifact.body}</pre>
            <div className="ai-draft-artifact-actions">
              <button
                className="is-primary"
                onClick={() =>
                  onUseComposeArtifact(
                    composeArtifact,
                    message.mailContext?.kind === "compose"
                      ? message.mailContext
                      : null,
                  )
                }
              >
                {composeArtifact.mode === "replace"
                  ? "Apply revised draft"
                  : "Insert draft"}
              </button>
              <button
                className="is-secondary"
                onClick={() => void actions.copyDraft(composeArtifact.body)}
              >
                Copy draft
              </button>
            </div>
          </div>
        ) : null}
        {sendPlan ? (
          <div className="ai-draft-artifact ai-send-plan">
            <strong>Review before sending</strong>
            {sendPlan.messages.map((item, index) => (
              <div key={index}>
                <p>
                  <b>To:</b> {item.recipients.join(", ") || "Missing recipient"}
                </p>
                <p>
                  <b>Subject:</b> {item.subject || "Missing subject"}
                </p>
                <pre>{item.body}</pre>
              </div>
            ))}
            <div className="ai-draft-artifact-actions">
              <button
                className="is-primary"
                onClick={() => onConfirmSendPlan(sendPlan)}
              >
                Confirm and send
              </button>
            </div>
          </div>
        ) : null}
        {draftArtifact && message.assistantFollowupText ? (
          <AnimatedAssistantText
            text={message.assistantFollowupText}
            animate={animate}
            onOpenThread={openThreadReference}
          />
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
        <div className="ai-message-footer">
          <time>{aiTimeLabel(message.timestamp)}</time>
          {message.kind === "error" ? (
            <button
              type="button"
              className="ai-retry-button"
              aria-label="Retry"
              data-tooltip="Retry"
              onClick={() => actions.retryAiMessage(message.id)}
            >
              <RefreshIcon />
            </button>
          ) : null}
        </div>
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
      <AnimatedAssistantText
        text={summaryText}
        animate={animate}
        onOpenThread={openThreadReference}
      />
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
                              title={[
                                link.label,
                                link.from,
                                link.date,
                                link.snippet,
                              ]
                                .filter(Boolean)
                                .join(" · ")}
                              onClick={() => onOpenMail(link)}
                            >
                              {link.label}
                            </button>
                          ))}
                        </span>
                      ) : item.subject &&
                        item.mailbox &&
                        item.thread_id &&
                        item.message_id ? (
                        <button
                          className="ai-single-mail-link"
                          title={item.subject}
                          onClick={() =>
                            onOpenMail({
                              label: item.subject || "Email",
                              mailbox: item.mailbox || "",
                              thread_id: item.thread_id || "",
                              message_id: item.message_id || "",
                              from: item.from,
                            })
                          }
                        >
                          {item.subject}
                        </button>
                      ) : item.subject ? (
                        <b>{item.subject}</b>
                      ) : null}
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
  onUseComposeArtifact,
  onOpenMail,
  onConfirmSendPlan,
}: {
  message: AiChatMessage;
  currentMailContext: AiMailContextRef | null;
  onUseArtifact: (
    artifact: DraftReplyArtifact,
    mode: "append" | "replace",
  ) => void;
  onUseComposeArtifact: (
    artifact: ComposeDraftArtifact,
    context: AiComposeContextRef | null,
  ) => void;
  onOpenMail: (target: AskMailLink) => void;
  onConfirmSendPlan: (plan: SendPlanArtifact) => void;
}) {
  if (message.role === "user") {
    return <div className="ai-message is-user">{message.content}</div>;
  }
  return (
    <AiAssistantMessage
      message={message}
      currentMailContext={currentMailContext}
      onUseArtifact={onUseArtifact}
      onUseComposeArtifact={onUseComposeArtifact}
      onOpenMail={onOpenMail}
      onConfirmSendPlan={onConfirmSendPlan}
    />
  );
}

function AiSidebar({
  collapsed,
  onToggle,
  currentMailContext,
  onUseArtifact,
  onUseComposeArtifact,
  onOpenMail,
  onConfirmSendPlan,
  composerFocusKey,
}: {
  collapsed: boolean;
  onToggle: () => void;
  currentMailContext: AiMailContextRef | null;
  onUseArtifact: (
    artifact: DraftReplyArtifact,
    mode: "append" | "replace",
  ) => void;
  onUseComposeArtifact: (
    artifact: ComposeDraftArtifact,
    context: AiComposeContextRef | null,
  ) => void;
  onOpenMail: (target: AskMailLink) => void;
  onConfirmSendPlan: (plan: SendPlanArtifact) => void;
  composerFocusKey: number;
}) {
  const { state, actions } = useApp();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [showNewMessagePrompt, setShowNewMessagePrompt] = useState(false);
  const [savedPromptsOpen, setSavedPromptsOpen] = useState(false);
  const [composerFocused, setComposerFocused] = useState(false);
  const [savedPrompts, setSavedPrompts] = useState<Array<{ id: string; title: string; body: string }>>([]);
  const conversationRef = useRef<HTMLDivElement | null>(null);
  const composerInputRef = useRef<HTMLTextAreaElement | null>(null);
  const savedPromptsPanelRef = useRef<HTMLDivElement | null>(null);
  const pinnedToBottomRef = useRef(true);
  const scrollAfterSubmitRef = useRef(false);
  const running =
    state.aiChatLoading ||
    state.isCustomScanning ||
    Boolean(
      state.customRunProgress && state.customRunProgress.status !== "failed",
    );
  const llmOffline = !state.runtime.connected;
  const starters = [
    "What needs my reply?",
    "Find urgent emails",
    "Organize my inbox",
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
    if (!composerFocusKey) return;
    const frame = window.requestAnimationFrame(() =>
      composerInputRef.current?.focus(),
    );
    return () => window.cancelAnimationFrame(frame);
  }, [composerFocusKey]);

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

  const openSavedPrompts = () => {
    setSavedPromptsOpen(true);
    void actions.listSavedPrompts().then(setSavedPrompts);
  };

  useEffect(() => {
    if (!savedPromptsOpen) return;
    const closeOnOutsidePointerDown = (event: PointerEvent) => {
      if (!savedPromptsPanelRef.current?.contains(event.target as Node)) {
        setSavedPromptsOpen(false);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePointerDown);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointerDown);
  }, [savedPromptsOpen]);

  const submit = () => {
    if (llmOffline) {
      actions.showToast("LLM is offline. Please try again when it reconnects.");
      return;
    }
    if (!state.customScanInput.trim() || running) return;
    setHistoryOpen(false);
    setSavedPromptsOpen(false);
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
                onUseComposeArtifact={onUseComposeArtifact}
                onOpenMail={onOpenMail}
                onConfirmSendPlan={onConfirmSendPlan}
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
        <div
          className={`ai-composer ${running ? "is-running" : ""} ${llmOffline ? "is-offline" : ""}`}
          data-tooltip={
            llmOffline
              ? "LLM is offline. Please try again when it reconnects."
              : undefined
          }
        >
          {savedPromptsOpen ? (
            <div className="ai-saved-prompts-panel" ref={savedPromptsPanelRef} role="listbox" aria-label="Saved prompts">
              <header>
                <span>Saved prompts</span>
                <button
                  type="button"
                  className="ai-saved-prompts-settings"
                  onClick={() => {
                    setSavedPromptsOpen(false);
                    actions.openSettings(true);
                  }}
                  aria-label="Open Saved prompts settings"
                  title="Saved prompts settings"
                >
                  <SettingsIcon />
                </button>
              </header>
              {savedPrompts.length ? (
                <div className="ai-saved-prompts-list">
                  {savedPrompts.map((item) => (
                    <div key={item.id} className="ai-saved-prompt-item" role="option">
                      <button
                        type="button"
                        className="ai-saved-prompt-select"
                        onClick={() => {
                          actions.setInput("customScanInput", item.body);
                          setSavedPromptsOpen(false);
                        }}
                      >
                        <strong>{item.title || "Untitled"}</strong>
                        <div>{item.body.slice(0, 80)}</div>
                      </button>
                      <button
                        type="button"
                        className="ai-saved-prompt-delete"
                        aria-label={`Delete ${item.title || "saved prompt"}`}
                        data-tooltip="Delete"
                        onClick={() => {
                          void actions.deleteSavedPrompt(item.id).then((deleted) => {
                            if (deleted) {
                              setSavedPrompts((current) =>
                                current.filter((prompt) => prompt.id !== item.id),
                              );
                            }
                          });
                        }}
                      >
                        <TrashIcon />
                      </button>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="ai-saved-prompts-empty">No saved prompts</div>
              )}
            </div>
          ) : null}
          <textarea
            ref={composerInputRef}
            value={state.customScanInput}
            placeholder={composerFocused ? "Press ↑ for saved prompts" : "Find, organize, ask anything…"}
            rows={3}
            disabled={llmOffline}
            onFocus={() => setComposerFocused(true)}
            onBlur={() => setComposerFocused(false)}
            onChange={(event) =>
              actions.setInput("customScanInput", event.target.value)
            }
            onKeyDown={(event) => {
              if (event.key === "ArrowUp" && !state.customScanInput.trim() && !event.shiftKey) {
                event.preventDefault();
                openSavedPrompts();
                return;
              }
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit();
              }
            }}
          />
          <div className="ai-composer-footer">
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
                disabled={
                  llmOffline || running || !state.customScanInput.trim()
                }
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
                disabled={llmOffline}
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
      <button
        className="icon-btn account-settings-btn"
        type="button"
        aria-label="Open settings"
        title="Settings"
        data-tooltip="Settings"
        onClick={() => actions.openSettings()}
      >
        <svg
          width="20"
          height="20"
          viewBox="0 0 24 24"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
        >
          <path
            d="M8.9104 21.6961C9.00388 22.1635 9.4143 22.5 9.89098 22.5H14.1132C14.5899 22.5 15.0003 22.1635 15.0938 21.6961L15.5256 19.5368L16.7521 18.9236L18.416 19.7555C18.91 20.0025 19.5106 19.8023 19.7576 19.3083L21.8687 15.0861C22.0849 14.6538 21.9609 14.1289 21.5743 13.8389L19.8632 12.5556V11.4444L21.5743 10.1611C21.9609 9.87114 22.0849 9.34616 21.8687 8.9139L19.7576 4.69168C19.525 4.22649 18.9747 4.01726 18.4918 4.21041L16.3413 5.07061L15.5403 4.5366L15.0938 2.30388C15.0003 1.83646 14.5899 1.5 14.1132 1.5H9.89098C9.4143 1.5 9.00388 1.83646 8.9104 2.30388L8.46385 4.5366L7.66285 5.0706L5.51237 4.21041C5.02948 4.01726 4.47914 4.22649 4.24655 4.69168L2.13544 8.9139C1.91931 9.34616 2.04324 9.87114 2.42986 10.1611L4.14098 11.4444V12.5556L2.42986 13.8389C2.04324 14.1289 1.91931 14.6538 2.13544 15.0861L4.24655 19.3083C4.49354 19.8023 5.09421 20.0025 5.58819 19.7555L7.25209 18.9236L8.47853 19.5368L8.9104 21.6961ZM10.7108 20.5L10.3438 18.665C10.2833 18.3624 10.0864 18.1047 9.81041 17.9667L7.6993 16.9111C7.41777 16.7704 7.0864 16.7704 6.80487 16.9111L5.58819 17.5195L4.29753 14.9381L5.74098 13.8556C5.99278 13.6667 6.14098 13.3703 6.14098 13.0556L6.14098 10.9444C6.14098 10.6297 5.99278 10.3333 5.74098 10.1444L4.29753 9.06186L5.62391 6.40909L7.40847 7.12292C7.71422 7.24522 8.06057 7.20916 8.33457 7.02649L9.9179 5.97094C10.1386 5.82382 10.2918 5.59507 10.3438 5.335L10.7108 3.5H13.2934L13.6604 5.335C13.7124 5.59507 13.8656 5.82382 14.0863 5.97094L15.6696 7.02649C15.9436 7.20916 16.29 7.24522 16.5957 7.12292L18.3803 6.40909L19.7066 9.06186L18.2632 10.1444C18.0114 10.3333 17.8632 10.6297 17.8632 10.9444V13.0556C17.8632 13.3703 18.0114 13.6667 18.2632 13.8556L19.7066 14.9381L18.416 17.5195L17.1993 16.9111C16.9178 16.7704 16.5864 16.7704 16.3049 16.9111L14.1938 17.9667C13.9178 18.1047 13.7209 18.3624 13.6604 18.665L13.2934 20.5H10.7108Z"
            fill="black"
          ></path>
          <path
            d="M12 9.5C10.6193 9.5 9.5 10.6193 9.5 12C9.5 13.3807 10.6193 14.5 12 14.5C13.3807 14.5 14.5 13.3807 14.5 12C14.5 10.6193 13.3807 9.5 12 9.5ZM7.5 12C7.5 9.51472 9.51472 7.5 12 7.5C14.4853 7.5 16.5 9.51472 16.5 12C16.5 14.4853 14.4853 16.5 12 16.5C9.51472 16.5 7.5 14.4853 7.5 12Z"
            fill="black"
          ></path>
        </svg>
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
          {!mailboxes.length ? (
            <p className="account-menu-empty">empty</p>
          ) : null}
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
  const [searchFocused, setSearchFocused] = useState(false);
  const [searchSuggestionIndex, setSearchSuggestionIndex] = useState(0);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const [activeSearch, setActiveSearch] = useState("");
  const [filterBeforeSearch, setFilterBeforeSearch] =
    useState<FeedFilter>("important");
  const parsedSearch = useMemo(() => parseInboxQuery(search), [search]);
  const searchSuggestions = useMemo(
    () => getInboxQuerySuggestions(search),
    [search],
  );
  const parsedActiveSearch = useMemo(
    () => parseInboxQuery(activeSearch),
    [activeSearch],
  );
  const applySearch = useCallback(
    (value: string) => {
      const parsed = parseInboxQuery(value);
      const directStatus =
        parsed.expression?.kind === "term" && parsed.expression.field === "is"
          ? parsed.expression.value
          : "";
      setSearch(value);
      setSearchFocused(false);
      if (directStatus) {
        setActiveSearch("");
        if (directStatus === "important") {
          setMailboxView("inbox");
          setFilter("important");
          return;
        }
        if (directStatus === "all") {
          setMailboxView("all");
          return;
        }
        if (directStatus === "unread") {
          setMailboxView("inbox");
          setFilterBeforeSearch("important");
          setActiveSearch(value);
          setFilter("search");
          return;
        }
        const view = directStatus === "is" ? "inbox" : directStatus;
        if (
          [
            "inbox",
            "todos",
            "snoozed",
            "done",
            "starred",
            "drafts",
            "sent",
            "trash",
            "spam",
          ].includes(view)
        ) {
          setMailboxView(view as MailboxView);
          if (view === "inbox") setFilter("other");
          return;
        }
      }
      if (value.trim() && !parsed.error) {
        setFilterBeforeSearch(
          filter === "search" ? filterBeforeSearch : filter,
        );
        setActiveSearch(value);
        setFilter("search");
      }
    },
    [filter, filterBeforeSearch],
  );
  const applySuggestion = useCallback(
    (suggestion: string) => {
      const next = applyInboxQuerySuggestion(search, suggestion);
      setSearch(next);
      setSearchFocused(true);
      // 选择补全后 React 会保留旧光标偏移；显式移到新增 token 的末尾。
      window.requestAnimationFrame(() =>
        searchInputRef.current?.setSelectionRange(next.length, next.length),
      );
    },
    [search],
  );
  const [selectedId, setSelectedId] = useState("");
  const [snoozeTarget, setSnoozeTarget] = useState<InboxMessage | null>(null);
  const [insertRequest, setInsertRequest] = useState<{
    nonce: string;
    artifact: DraftReplyArtifact;
    mode: "append" | "replace";
  } | null>(null);
  const [externalDetailMessage, setExternalDetailMessage] =
    useState<InboxMessage | null>(null);
  const [drawerMessage, setDrawerMessage] = useState<InboxMessage | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [authChecking, setAuthChecking] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(initialSidebarWidth);
  const [sidebarResizing, setSidebarResizing] = useState(false);
  const [folderOpen, setFolderOpen] = useState(false);
  const [refreshChoiceOpen, setRefreshChoiceOpen] = useState(false);
  const [composeOpen, setComposeOpen] = useState(false);
  const [composeClosing, setComposeClosing] = useState(false);
  const [composeResumeDraft, setComposeResumeDraft] =
    useState<ComposeDraft | null>(null);
  const [composeAiContext, setComposeAiContext] =
    useState<AiComposeContextRef | null>(null);
  const [composeInsertRequest, setComposeInsertRequest] = useState<{
    nonce: string;
    artifact: ComposeDraftArtifact;
  } | null>(null);
  const [aiComposerFocusKey, setAiComposerFocusKey] = useState(0);
  const [composeDrafts, setComposeDrafts] = useState<ComposeDraft[]>([]);
  const [selectedComposeDraftIds, setSelectedComposeDraftIds] = useState<
    Set<string>
  >(new Set());
  const [batchConfirmDrafts, setBatchConfirmDrafts] = useState<
    ComposeDraft[] | null
  >(null);
  const pendingComposeTimer = useRef<number | null>(null);
  const pendingComposeCountdown = useRef<number | null>(null);
  const composeCloseTimer = useRef<number | null>(null);
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
  const [cachedInboxRetryAction, setCachedInboxRetryAction] =
    useState<CachedInboxRetryAction>("sync");
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
        Number(cached.missingUpdatedAt || 0) > Date.now() - 24 * 60 * 60 * 1000;
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
      setCachedInboxRetryAction("sync");
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
    () =>
      mergeDraftOverlayMessages(state.inboxMessages, state.inboxDraftMessages),
    [state.inboxDraftMessages, state.inboxMessages],
  );
  const inboxSnapshotMessagesWithDrafts = useMemo(
    () =>
      mergeDraftOverlayMessages(
        state.inboxSnapshotMessages,
        state.inboxDraftMessages,
      ),
    [state.inboxDraftMessages, state.inboxSnapshotMessages],
  );
  const sourceMessages = useMemo(() => {
    const resolved = resolveSourceMessages(
      mailboxView,
      inboxMessagesWithDrafts,
      inboxSnapshotMessagesWithDrafts,
      flags,
    );
    if (mailboxView !== "drafts") return resolved;
    const composeMessages: InboxMessage[] = composeDrafts.map((draft) => ({
      id: `compose:${draft.id}`,
      mailbox,
      date: draft.updated_at || draft.created_at || "",
      from: mailbox,
      to: draft.recipients.join(", "),
      subject: draft.subject || "(no subject)",
      snippet: draft.body.slice(0, 120),
      body_preview: draft.body.slice(0, 120),
      draft_body: draft.body,
      draft_local: true,
      label_ids: ["DRAFT"],
    }));
    return [...composeMessages, ...resolved];
  }, [
    composeDrafts,
    flags,
    inboxMessagesWithDrafts,
    inboxSnapshotMessagesWithDrafts,
    mailbox,
    mailboxView,
  ]);
  const inboxSplitMessages = useMemo(
    () =>
      splitInboxMessages(
        sourceMessages.filter(
          (message) =>
            !flags.todos.includes(message.id) &&
            !isDoneMessage(message, flags) &&
            !flags.snoozed.includes(message.id),
        ),
        state.inboxSettings,
      ),
    [flags, sourceMessages, state.inboxSettings],
  );
  const [splitsOpen, setSplitsOpen] = useState(false);

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
        const next = {
          ...current,
          saved: { ...current.saved },
          snoozedUntil: { ...(current.snoozedUntil || {}) },
        };
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

  // AI 整理确认：后端 mark_read 后由事件写入本地 Done（与 Inbox Done 对齐）
  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail as {
        items?: Array<{ mailbox?: string; message_id?: string; thread_id?: string }>;
      } | undefined;
      const items = detail?.items || [];
      if (!items.length) return;
      const messages: InboxMessage[] = [];
      for (const item of items) {
        const itemMailbox = String(item.mailbox || "").trim().toLowerCase();
        if (itemMailbox && itemMailbox !== String(mailbox || "").trim().toLowerCase()) continue;
        const mid = String(item.message_id || "").trim();
        const tid = String(item.thread_id || "").trim();
        const found = state.inboxMessages.find(
          (message) =>
            (mid && message.id === mid) ||
            (tid && (message.thread_id === tid || message.id === tid)),
        );
        if (found) messages.push(found);
        else if (mid) {
          messages.push({
            id: mid,
            thread_id: tid || mid,
            subject: "",
            snippet: "",
            from: "",
            date: "",
            label_ids: [],
          } as InboxMessage);
        }
      }
      if (messages.length) setWorkflowFlag("done", messages, true);
    };
    window.addEventListener("anna-inbox-local-done", handler as EventListener);
    return () => window.removeEventListener("anna-inbox-local-done", handler as EventListener);
  }, [mailbox, setWorkflowFlag, state.inboxMessages]);

  const restoreWorkflowFlags = useCallback(
    (messages: InboxMessage[], previous: MailUiFlags) => {
      const ids = new Set(messages.map((item) => item.id));
      setFlags((current) => {
        const next = {
          ...current,
          saved: { ...current.saved },
          snoozedUntil: { ...(current.snoozedUntil || {}) },
        };
        for (const workflowKind of ["todos", "snoozed", "done"] as const) {
          next[workflowKind] = [
            ...current[workflowKind].filter((id) => !ids.has(id)),
            ...previous[workflowKind].filter((id) => ids.has(id)),
          ];
        }
        for (const id of ids) {
          if (previous.snoozedUntil?.[id])
            next.snoozedUntil[id] = previous.snoozedUntil[id];
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
      const cached = await getCachedMessageBody(
        bodyMailbox,
        message.id,
        message.internal_date,
      );
      if (cached) {
        setMessageBodies((current) => ({
          ...current,
          [key]: { status: "ready", body: cached.body_text },
        }));
        return;
      }
      const body = await loadInboxEmailBodyRef.current(message.id, bodyMailbox);
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
          const missing =
            permissionRequired || serviceDisabled
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
    const categoryId = filter.startsWith("category:")
      ? filter.slice("category:".length)
      : "";
    const splitMessages =
      filter === "important"
        ? inboxSplitMessages.important
        : filter === "other"
          ? inboxSplitMessages.other
          : categoryId
            ? inboxSplitMessages.custom[categoryId] || []
            : null;
    const splitMessageIds = splitMessages
      ? new Set(splitMessages.map((message) => message.id))
      : null;
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
            ? Boolean(splitMessageIds?.has(message.id))
            : filter === "search"
              ? true
              : Boolean(splitMessageIds?.has(message.id));
      if (!matchesView) return false;
      if (!matchesFilter) return false;
      if (!activeSearch.trim() || parsedActiveSearch.error) return true;
      return matchInboxQuery(message, parsedActiveSearch);
    });
  }, [
    activeSearch,
    filter,
    flags,
    inboxSplitMessages,
    mailboxView,
    parsedActiveSearch,
    sourceMessages,
  ]);

  useEffect(() => {
    if (!filter.startsWith("category:")) return;
    const id = filter.slice("category:".length);
    const categories = state.inboxSettings.custom_categories || [];
    if (!categories.some((split) => split.id === id))
      setFilter("important");
  }, [filter, state.inboxSettings.custom_categories]);

  const displayedVisible = useMemo(
    () => (localCategory ? visible.slice(0, feedWindow.localLimit) : visible),
    [feedWindow.localLimit, localCategory, visible],
  );
  const pinnedImportantMessages = useMemo(() => {
    const byId = new Map<string, InboxMessage>();
    for (const message of [
      ...Object.values(flags.saved),
      ...state.inboxSnapshotMessages,
      ...state.inboxMessages,
    ]) {
      if (message.id && !isTrashMessage(message)) byId.set(message.id, message);
    }
    return [...byId.values()].filter(
      (message) =>
        isStarredMessage(message) || flags.todos.includes(message.id),
    );
  }, [
    flags.saved,
    flags.todos,
    state.inboxMessages,
    state.inboxSnapshotMessages,
  ]);
  useEffect(() => {
    const additions = pinnedImportantMessages.filter(
      (message) => !flags.saved[message.id],
    );
    if (!additions.length) return;
    setFlags((current) => {
      const next = { ...current, saved: { ...current.saved } };
      for (const message of additions) next.saved[message.id] = message;
      void setMailFlags(mailbox, next);
      return next;
    });
  }, [flags.saved, mailbox, pinnedImportantMessages]);
  useEffect(() => {
    if (!mailbox || !displayedVisible.length) return;
    const session = ++bodyPreheatSession.current;
    const start = Math.max(0, displayedVisible.length - INBOX_FEED_PAGE_SIZE);
    const pageMessages = displayedVisible
      .slice(start)
      .filter((message) => message.id);
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
          if (bodyPreheatSeen.current.has(key) || bodyRequests.current.has(key))
            continue;
          bodyPreheatSeen.current.add(key);
          bodyRequests.current.add(key);
          active += 1;
          void (async () => {
            try {
              const cached = await getCachedMessageBody(
                bodyMailbox,
                message.id,
                message.internal_date,
              );
              if (cached || session !== bodyPreheatSession.current) return;
              const body = await loadInboxEmailBodyRef.current(
                message.id,
                bodyMailbox,
              );
              if (session !== bodyPreheatSession.current) return;
              await setCachedMessageBody(bodyMailbox, message, {
                body_text: body,
              });
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
          currentNextOffset =
            "nextOffset" in result ? result.nextOffset : currentNextOffset;
          if (result.hasMore) {
            continue;
          }
          currentSource = "gmail";
          continue;
        }
        currentGmailPageToken =
          "pageToken" in result ? result.pageToken : currentGmailPageToken;
        currentGmailPageOffset =
          "pageOffset" in result ? result.pageOffset : currentGmailPageOffset;
        if (!result.hasMore) break;
      }
      return {
        days: INBOX_ALL_TIME_DAYS,
        nextOffset: currentNextOffset,
        hasMore: false,
        localLimit: INBOX_FEED_PAGE_SIZE,
        source: currentSource,
        gmailPageToken: currentGmailPageToken,
        gmailPageOffset: currentGmailPageOffset,
      } satisfies InboxFeedWindow;
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
            setFeedWindow((current) => ({
              ...current,
              localLimit: INBOX_FEED_PAGE_SIZE,
            }));
            return true;
          }
          const result = await actions.refreshInboxEmails(
            "inbox",
            7,
            clearCache,
          );
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
    [
      actions,
      feedWindow.days,
      loadRemainingAllTimeInbox,
      loadRemoteCategory,
      localCategory,
      mailbox,
      mailboxView,
    ],
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
      const excludeIds = sourceMessages
        .map((message) => message.id)
        .filter(Boolean);
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
      actions.showToast(
        result.count
          ? `${result.count} new email${result.count === 1 ? "" : "s"} loaded.`
          : "No new emails found.",
      );
    } finally {
      setFeedAction((current) => (current === "refresh" ? null : current));
    }
  }, [
    actions,
    feedWindow.days,
    loadGmailPage,
    localCategory,
    mailboxView,
    sourceMessages,
    syncInbox,
  ]);

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
          await actions.loadInboxEmails("inbox", 30, true);
        }
      }
    } finally {
      setAuthChecking(false);
    }
  }, [actions, authChecking, mailbox]);

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
        setCachedInboxRetryAction("load-more");
        actions.showToast("Failed to load emails older than 30 days.");
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
      const allTimeWindow = await loadRemainingAllTimeInbox(
        source,
        nextOffset,
        gmailPageToken,
        gmailPageOffset,
        excludeMessageIds,
      );
      if (!allTimeWindow) {
        setCachedInboxRetryAction("load-more");
        actions.showToast("Failed to load emails older than 30 days.");
        return;
      }
      setFeedWindow(allTimeWindow);
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

  const isInboxSyncing = state.inboxSnapshotLoading || feedAction === "refresh";
  const days = feedWindow.days;
  useEffect(() => {
    const configuredDays = state.inboxSettings.display_range_days;
    if (configuredDays === feedWindow.days || !mailbox) return;
    setFeedWindow((current) => ({
      ...DEFAULT_INBOX_FEED_WINDOW,
      days: configuredDays,
    }));
    void syncInbox(configuredDays, true);
  }, [
    feedWindow.days,
    mailbox,
    state.inboxSettings.display_range_days,
    syncInbox,
  ]);
  const lastSyncedLabel = inboxLastSyncedLabel(state.inboxUpdatedAt);
  const canLoadMoreInbox =
    mailboxView === "inbox" &&
    filter !== "search" &&
    days === INBOX_LAST_MONTH_DAYS;
  const canShowOlderInboxActions =
    !state.inboxError &&
    !state.inboxLoading &&
    !isInboxSyncing &&
    feedAction === null;

  const grouped = useMemo(() => {
    if (mailboxView !== "inbox" || filter === "search") {
      return [{ label: "", messages: displayedVisible }];
    }
    const selectedCustomSplit = filter.startsWith("category:")
      ? (state.inboxSettings.custom_categories || []).find(
          (split) => split.id === filter.slice("category:".length),
        )
      : undefined;
    if (selectedCustomSplit) {
      return groupSplitMessages(
        displayedVisible,
        selectedCustomSplit.bundling_behavior,
        state.inboxSettings.time_section_mode,
      );
    }
    const normalImportant = displayedVisible.filter(
      (message) =>
        !isStarredMessage(message) && !flags.todos.includes(message.id),
    );
    const source =
      filter === "important"
        ? splitImportantMessages(
            [...pinnedImportantMessages, ...normalImportant],
            state.inboxSettings,
            new Set(flags.todos),
          ).flatMap((group) =>
            group.kind === "important"
              ? group.messages
              : group.messages.map(
                  (message) =>
                    ({
                      ...message,
                      __groupLabel: group.kind.toUpperCase(),
                    }) as InboxMessage & { __groupLabel?: string },
                ),
          )
        : displayedVisible;
    const groups: Array<{ label: string; messages: InboxMessage[] }> = [];
    for (const message of source) {
      const label =
        (message as InboxMessage & { __groupLabel?: string }).__groupLabel ||
        groupLabel(message, state.inboxSettings.time_section_mode);
      const current = groups[groups.length - 1];
      if (!current || current.label !== label)
        groups.push({ label, messages: [message] });
      else current.messages.push(message);
    }
    return groups;
  }, [
    displayedVisible,
    filter,
    flags.todos,
    mailboxView,
    pinnedImportantMessages,
    state.inboxSettings,
  ]);

  const selectedMessage = useMemo(() => {
    if (!selectedId) return null;
    return (
      sourceMessages.find((item) => item.id === selectedId) ||
      state.inboxDraftMessages.find((item) => item.id === selectedId) ||
      state.inboxSnapshotMessages.find((item) => item.id === selectedId) ||
      state.inboxMessages.find((item) => item.id === selectedId) ||
      flags.saved[selectedId] ||
      (externalDetailMessage?.id === selectedId
        ? externalDetailMessage
        : null) ||
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

  useEffect(
    () => () => {
      if (drawerCloseTimer.current)
        window.clearTimeout(drawerCloseTimer.current);
    },
    [],
  );

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

  const sidebarMailContext = composeAiContext || currentMailContext;

  const openComposeAiDraft = useCallback(
    (draft: Pick<ComposeDraft, "recipients" | "subject" | "body">) => {
      const context: AiComposeContextRef = {
        kind: "compose",
        session_id: crypto.randomUUID(),
        mailbox,
        recipients: [...draft.recipients],
        subject: draft.subject,
        body: draft.body,
      };
      setComposeAiContext(context);
      actions.startNewAiConversation();
      setSidebarCollapsed(false);
      actions.setInput(
        "customScanInput",
        draft.body.trim()
          ? "Suggest changes to improve my draft"
          : `Write a first draft about ${draft.subject}`,
      );
      setAiComposerFocusKey((key) => key + 1);
    },
    [actions, mailbox],
  );

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
      if (message.id.startsWith("compose:")) {
        const draft = composeDrafts.find(
          (item) => item.id === message.id.slice("compose:".length),
        );
        if (draft) {
          if (composeCloseTimer.current)
            window.clearTimeout(composeCloseTimer.current);
          setComposeClosing(false);
          setComposeResumeDraft(draft);
          setComposeOpen(true);
        }
        return;
      }
      setExternalDetailMessage(null);
      setSelectedId(message.id);
      if (
        !(message.unread || hasMessageLabel(message, "UNREAD")) ||
        !message.thread_id
      )
        return;
      void actions
        .updateInboxThreadState(
          message.mailbox || mailbox,
          message.thread_id,
          "mark_read",
        )
        .catch((reason) => {
          actions.showToast(
            reason instanceof Error ? reason.message : String(reason),
          );
        });
    },
    [actions, composeDrafts, mailbox],
  );

  const openMailDetailFromAi = useCallback(
    async (target: AskMailLink) => {
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
        ].find(
          (item) =>
            (item.mailbox || targetMailbox).trim().toLowerCase() ===
              targetMailbox &&
            (target.message_id
              ? item.id === target.message_id
              : (item.thread_id || item.id) === target.thread_id),
        );
        if (known) {
          setExternalDetailMessage(null);
          setSelectedId(known.id);
          return;
        }
        const page = await actions.loadInboxThreadPage(
          targetMailbox,
          target.thread_id,
          {
            anchorMessageId: target.message_id || undefined,
            limit: 5,
            includeDisplayBody: true,
          },
        );
        const anchor =
          page.messages.find((item) => item.id === target.message_id) ||
          page.messages.find((item) => item.id === page.latest_message_id) ||
          page.messages.at(-1);
        if (!anchor)
          throw new Error("The referenced thread could not be loaded.");
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
        actions.showToast(
          reason instanceof Error ? reason.message : String(reason),
        );
      }
    },
    [
      actions,
      flags.saved,
      mailbox,
      state.inboxDraftMessages,
      state.inboxMessages,
      state.inboxSnapshotMessages,
    ],
  );

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
    if (next === mailboxView && filter !== "search") {
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
      void actions.listInboxThreadDrafts(mailbox, 100).catch(() => undefined);
      void actions
        .listComposeDrafts(mailbox)
        .then((payload) => setComposeDrafts(payload.drafts || []))
        .catch(() => setComposeDrafts([]));
      return;
    }
    if (isLocalMailboxView(next)) {
      return;
    }
    actions.resetInboxFeed();
    if (!isLocalMailboxView(next)) {
      void loadRemoteCategory(
        next,
        next === "inbox" ? INBOX_LAST_MONTH_DAYS : 7,
      );
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

  const scheduleComposeSend = useCallback(
    (draft: ComposeDraft) => {
      if (pendingComposeTimer.current)
        window.clearTimeout(pendingComposeTimer.current);
      if (pendingComposeCountdown.current)
        window.clearInterval(pendingComposeCountdown.current);
      setComposeResumeDraft(null);
      setComposeOpen(false);
      setComposeClosing(true);
      const undo = () => {
        if (pendingComposeTimer.current)
          window.clearTimeout(pendingComposeTimer.current);
        if (pendingComposeCountdown.current)
          window.clearInterval(pendingComposeCountdown.current);
        pendingComposeTimer.current = null;
        pendingComposeCountdown.current = null;
        void actions
          .deleteComposeDraft(mailbox, draft.id)
          .catch(() => undefined);
        if (composeCloseTimer.current)
          window.clearTimeout(composeCloseTimer.current);
        setComposeClosing(false);
        setComposeResumeDraft(draft);
        setComposeOpen(true);
      };
      const deadline = Date.now() + 10_000;
      const updateCountdown = () => {
        const seconds = Math.max(1, Math.ceil((deadline - Date.now()) / 1000));
        actions.showToast(
          `Will send in ${seconds} second${seconds === 1 ? "" : "s"}.`,
          { actionLabel: "Undo", onAction: undo, durationMs: 1_100 },
        );
      };
      updateCountdown();
      pendingComposeCountdown.current = window.setInterval(
        updateCountdown,
        1_000,
      );
      pendingComposeTimer.current = window.setTimeout(() => {
        pendingComposeTimer.current = null;
        if (pendingComposeCountdown.current)
          window.clearInterval(pendingComposeCountdown.current);
        pendingComposeCountdown.current = null;
        actions.showToast("Sending…", { durationMs: 30_000 });
        void actions
          .sendComposeEmails(mailbox, [draft])
          .then(async (results) => {
            const result = results[0];
            if (result?.ok) {
              await actions.deleteComposeDraft(mailbox, draft.id);
              actions.showToast("Email sent.");
            } else {
              actions.showToast(
                result?.error || "Email could not be sent. The draft was kept.",
              );
            }
          })
          .catch((reason) =>
            actions.showToast(
              reason instanceof Error ? reason.message : String(reason),
            ),
          );
      }, 10_000);
    },
    [actions, mailbox],
  );

  const closeComposeDrawer = useCallback(() => {
    setComposeOpen(false);
    setComposeAiContext(null);
    setComposeInsertRequest(null);
    setComposeClosing(true);
    if (composeCloseTimer.current)
      window.clearTimeout(composeCloseTimer.current);
    composeCloseTimer.current = window.setTimeout(() => {
      setComposeClosing(false);
      setComposeResumeDraft(null);
      composeCloseTimer.current = null;
    }, 360);
  }, []);

  const scheduleComposeBatch = useCallback(
    (confirmed = false) => {
      const selected = composeDrafts.filter((draft) =>
        selectedComposeDraftIds.has(`compose:${draft.id}`),
      );
      if (!selected.length) {
        actions.showToast("Select a Compose draft to send.");
        return;
      }
      const incomplete = selected.filter(
        (draft) =>
          !draft.recipients.length ||
          !draft.subject.trim() ||
          !draft.body.trim(),
      );
      if (incomplete.length) {
        actions.showToast(
          `Complete ${incomplete.length} selected draft${incomplete.length === 1 ? "" : "s"} before sending.`,
        );
        return;
      }
      if (!confirmed) {
        setBatchConfirmDrafts(selected);
        return;
      }
      setBatchConfirmDrafts(null);
      if (pendingComposeTimer.current)
        window.clearTimeout(pendingComposeTimer.current);
      if (pendingComposeCountdown.current)
        window.clearInterval(pendingComposeCountdown.current);
      const deadline = Date.now() + 10_000;
      const undo = () => {
        if (pendingComposeTimer.current)
          window.clearTimeout(pendingComposeTimer.current);
        if (pendingComposeCountdown.current)
          window.clearInterval(pendingComposeCountdown.current);
        pendingComposeTimer.current = null;
        pendingComposeCountdown.current = null;
      };
      const updateCountdown = () => {
        const seconds = Math.max(1, Math.ceil((deadline - Date.now()) / 1000));
        actions.showToast(
          `${selected.length} drafts will send in ${seconds} second${seconds === 1 ? "" : "s"}.`,
          { actionLabel: "Undo", onAction: undo, durationMs: 1_100 },
        );
      };
      updateCountdown();
      pendingComposeCountdown.current = window.setInterval(
        updateCountdown,
        1_000,
      );
      pendingComposeTimer.current = window.setTimeout(() => {
        pendingComposeTimer.current = null;
        if (pendingComposeCountdown.current)
          window.clearInterval(pendingComposeCountdown.current);
        pendingComposeCountdown.current = null;
        actions.showToast("Sending drafts…", { durationMs: 30_000 });
        void actions
          .sendComposeEmails(mailbox, selected)
          .then(async (results) => {
            const succeeded = results
              .filter((result) => result.ok)
              .map((result) => result.id);
            await Promise.all(
              succeeded.map((id) => actions.deleteComposeDraft(mailbox, id)),
            );
            setComposeDrafts((drafts) =>
              drafts.filter((draft) => !succeeded.includes(draft.id)),
            );
            setSelectedComposeDraftIds(new Set());
            const failures = results.filter((result) => !result.ok);
            actions.showToast(
              failures.length
                ? `${succeeded.length} sent; ${failures.length} draft${failures.length === 1 ? "" : "s"} failed and were kept.`
                : `${succeeded.length} drafts sent.`,
            );
          })
          .catch((reason) =>
            actions.showToast(
              reason instanceof Error ? reason.message : String(reason),
            ),
          );
      }, 10_000);
    },
    [actions, composeDrafts, mailbox, selectedComposeDraftIds],
  );

  const confirmAiSendPlan = useCallback(
    (plan: SendPlanArtifact) => {
      const item = plan.messages[0];
      if (!item) return;
      void actions
        .saveComposeDraft(mailbox, {
          recipients: item.recipients,
          subject: item.subject,
          body: item.body,
        })
        .then((draft) => scheduleComposeSend(draft))
        .catch((reason) =>
          actions.showToast(
            reason instanceof Error ? reason.message : String(reason),
          ),
        );
    },
    [actions, mailbox, scheduleComposeSend],
  );

  useEffect(
    () => () => {
      if (pendingComposeTimer.current)
        window.clearTimeout(pendingComposeTimer.current);
      if (pendingComposeCountdown.current)
        window.clearInterval(pendingComposeCountdown.current);
      if (composeCloseTimer.current)
        window.clearTimeout(composeCloseTimer.current);
    },
    [],
  );

  return (
    <div
      className={`inbox-home ${sidebarCollapsed ? "is-ai-collapsed" : ""} ${sidebarResizing ? "is-resizing-ai-sidebar" : ""}`}
      style={layoutStyle}
    >
      <AiSidebar
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed((value) => !value)}
        currentMailContext={sidebarMailContext}
        onUseArtifact={(artifact, mode) =>
          setInsertRequest({ nonce: crypto.randomUUID(), artifact, mode })
        }
        onUseComposeArtifact={(artifact, sourceContext) => {
          if (
            !composeOpen ||
            !composeAiContext ||
            sourceContext?.session_id !== composeAiContext?.session_id ||
            composeAiContext.mailbox.trim().toLowerCase() !==
              artifact.mailbox.trim().toLowerCase()
          ) {
            actions.showToast(
              "Open the matching Compose draft before applying this suggestion.",
            );
            return;
          }
          setComposeInsertRequest({ nonce: crypto.randomUUID(), artifact });
        }}
        onOpenMail={(target) => void openMailDetailFromAi(target)}
        onConfirmSendPlan={confirmAiSendPlan}
        composerFocusKey={aiComposerFocusKey}
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
        className={`mail-workspace ${mailboxView === "inbox" && filter !== "search" ? "" : "is-folder-view"}`}
      >
        <header className="mail-topbar">
          <div className="mailbox-picker">
            <button
              className="mailbox-title"
              aria-expanded={folderOpen}
              onClick={() => setFolderOpen((open) => !open)}
            >
              <span
                className={`mailbox-current-icon is-${filter === "search" ? "search" : mailboxView}`}
              >
                {filter === "search" ? (
                  <SearchIcon />
                ) : (
                  <FolderIcon view={mailboxView} />
                )}
              </span>
              <div>
                <strong>
                  {filter === "search"
                    ? "Search"
                    : MAILBOX_VIEWS.find((item) => item.id === mailboxView)
                        ?.label}
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
          <div className="mail-search-wrap">
            <label className="mail-search">
              <SearchIcon />
              <span className="mail-search-highlight" aria-hidden="true">
                {splitInboxQueryTokens(search).map((token, index, tokens) => (
                  <span
                    className={`is-${token.kind}${parsedSearch.error && index === tokens.length - 1 && !/\s$/u.test(search) ? " is-editing" : ""}`}
                    key={`${token.text}-${index}`}
                  >
                    {token.text}
                  </span>
                ))}
              </span>
              <input
                ref={searchInputRef}
                value={search}
                onChange={(event) => {
                  setSearch(event.target.value);
                  setActiveSearch("");
                  setSearchFocused(true);
                  setSearchSuggestionIndex(0);
                }}
                onFocus={() => setSearchFocused(true)}
                onBlur={() =>
                  window.setTimeout(() => setSearchFocused(false), 120)
                }
                onKeyDown={(event) => {
                  if (event.key === "ArrowDown" && searchSuggestions.length) {
                    event.preventDefault();
                    setSearchSuggestionIndex(
                      (index) => (index + 1) % searchSuggestions.length,
                    );
                  } else if (
                    event.key === "ArrowUp" &&
                    searchSuggestions.length
                  ) {
                    event.preventDefault();
                    setSearchSuggestionIndex(
                      (index) =>
                        (index - 1 + searchSuggestions.length) %
                        searchSuggestions.length,
                    );
                  } else if (event.key === "Enter") {
                    event.preventDefault();
                    if (searchSuggestions.length)
                      applySuggestion(searchSuggestions[searchSuggestionIndex]);
                    else if (
                      parsedSearch.expression &&
                      !parsedSearch.error &&
                      !/\s$/u.test(search)
                    ) {
                      const next = `${search} `;
                      setSearch(next);
                      window.requestAnimationFrame(() =>
                        searchInputRef.current?.setSelectionRange(
                          next.length,
                          next.length,
                        ),
                      );
                    } else applySearch(search);
                  }
                }}
                placeholder="search emails..."
              />
              {activeSearch || search ? (
                <button
                  type="button"
                  className="mail-search-clear"
                  aria-label="Clear search"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    setSearch("");
                    setActiveSearch("");
                    setMailboxView("inbox");
                    setFilter("important");
                  }}
                >
                  ×
                </button>
              ) : null}
            </label>
            {searchFocused ? (
              <div className="mail-search-suggestions" role="listbox">
                {searchSuggestions.length ? (
                  searchSuggestions.map((suggestion, index) => (
                    <button
                      type="button"
                      role="option"
                      aria-selected={index === searchSuggestionIndex}
                      className={
                        index === searchSuggestionIndex ? "is-selected" : ""
                      }
                      key={suggestion}
                      onMouseDown={(event) => event.preventDefault()}
                      onMouseMove={() => setSearchSuggestionIndex(index)}
                      onClick={() => applySuggestion(suggestion)}
                    >
                      <strong>{suggestion}</strong>
                      {getInboxQuerySuggestionPlaceholder(suggestion) ? (
                        <span>
                          {getInboxQuerySuggestionPlaceholder(suggestion)}
                        </span>
                      ) : null}
                      {index === searchSuggestionIndex ? (
                        <kbd>Enter</kbd>
                      ) : null}
                    </button>
                  ))
                ) : parsedSearch.error ? (
                  <p role="alert">{parsedSearch.error}</p>
                ) : null}
              </div>
            ) : null}
          </div>
          <button
            className={`refresh-mail-btn ${isInboxSyncing ? "is-syncing" : ""}`}
            disabled={isInboxSyncing}
            onClick={() => setRefreshChoiceOpen(true)}
          >
            <RefreshIcon />
            <span>{isInboxSyncing ? "Syncing" : "Refresh"}</span>
          </button>
          <button
            className="refresh-mail-btn compose-open-btn"
            type="button"
            onClick={() => {
              if (composeCloseTimer.current)
                window.clearTimeout(composeCloseTimer.current);
              setComposeClosing(false);
              setComposeResumeDraft(null);
              setComposeOpen(true);
            }}
          >
            <ComposeIcon />
            <span>Compose</span>
          </button>
        </header>

        {refreshChoiceOpen ? (
          <div
            className="confirm-overlay refresh-choice-overlay"
            role="presentation"
          >
            <section
              className="confirm-dialog refresh-choice-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="refresh-choice-title"
            >
              <h3 id="refresh-choice-title">Refresh inbox</h3>
              <p>
                Choose how Anna should refresh the current mailbox cache and
                view.
              </p>
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
                <button
                  type="button"
                  onClick={() => setRefreshChoiceOpen(false)}
                >
                  Cancel
                </button>
              </div>
            </section>
          </div>
        ) : null}

        {batchConfirmDrafts ? (
          <div className="confirm-overlay" role="presentation">
            <section
              className="confirm-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="batch-send-title"
            >
              <h3 id="batch-send-title">
                Send {batchConfirmDrafts.length} draft
                {batchConfirmDrafts.length === 1 ? "" : "s"}?
              </h3>
              <p>
                Each email will be sent separately after the 10-second Undo
                window.
              </p>
              <ul className="batch-send-summary">
                {batchConfirmDrafts.map((draft) => (
                  <li key={draft.id}>
                    <strong>{draft.subject || "(no subject)"}</strong>&nbsp;
                    <span>{draft.recipients.join(", ")}</span>
                  </li>
                ))}
              </ul>
              <div className="refresh-choice-actions">
                <button
                  type="button"
                  onClick={() => setBatchConfirmDrafts(null)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="is-primary"
                  onClick={() => scheduleComposeBatch(true)}
                >
                  Confirm send
                </button>
              </div>
            </section>
          </div>
        ) : null}

        {mailboxView === "inbox" && filter !== "search" ? (
          <nav className="mail-tabs" aria-label="Inbox filters">
            {(
              [
                ["important", "Important", inboxSplitMessages.important.length],
                ["other", "Other", inboxSplitMessages.other.length],
                ...(state.inboxSettings.custom_categories || [])
                  .filter(
                    (split) =>
                      !split.hide_when_empty ||
                      (inboxSplitMessages.custom[split.id] || []).length > 0,
                  )
                  .map((split) => [
                    `category:${split.id}` as FeedFilter,
                    split.name,
                    (inboxSplitMessages.custom[split.id] || []).length,
                  ]),
              ] as Array<[FeedFilter, string, number]>
            ).map(([key, label, count]) => (
              <button
                key={key}
                className={filter === key ? "is-active" : ""}
                onClick={() => setFilter(key)}
              >
                {label}
                <span>{count}</span>
              </button>
            ))}
            <button
              className="icon-btn mail-tabs-manage"
              type="button"
              aria-label="Manage Splits"
              data-tooltip="Manage Splits"
              onClick={() => setSplitsOpen(true)}
            >
              <PlusIcon />
            </button>
            <div className="mail-tabs-meta">
              {lastSyncedLabel ? <span>{lastSyncedLabel}</span> : null}
              <p>{inboxRangeLabel(days)}</p>
            </div>
          </nav>
        ) : null}
        <SplitsManager
          open={splitsOpen}
          settings={state.inboxSettings}
          onChange={actions.saveInboxSettings}
          onClose={() => setSplitsOpen(false)}
        />

        <section
          className={`mail-feed ${mailboxView === "trash" ? "is-trash-view" : ""}`}
          aria-live="polite"
          ref={mailFeedRef}
          onScroll={handleFeedScroll}
        >
          {mailboxView === "drafts" && sourceMessages.length ? (
            <div className="draft-batch-bar">
              <button
                type="button"
                className="draft-select draft-select-all"
                aria-label="Select all drafts"
                aria-pressed={
                  sourceMessages.length > 0 &&
                  sourceMessages.every((message) =>
                    selectedComposeDraftIds.has(message.id),
                  )
                }
                onClick={() =>
                  setSelectedComposeDraftIds((current) =>
                    current.size === sourceMessages.length
                      ? new Set()
                      : new Set(sourceMessages.map((message) => message.id)),
                  )
                }
              >
                {sourceMessages.length > 0 &&
                sourceMessages.every((message) =>
                  selectedComposeDraftIds.has(message.id),
                )
                  ? "✓"
                  : ""}
              </button>
              <span>{selectedComposeDraftIds.size} selected</span>
              <button
                type="button"
                className="refresh-mail-btn draft-batch-send"
                disabled={!selectedComposeDraftIds.size}
                onClick={() => scheduleComposeBatch()}
              >
                Send selected
              </button>
            </div>
          ) : null}
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
                  onClick={() =>
                    void (cachedInboxRetryAction === "load-more"
                      ? loadMoreInbox()
                      : syncInbox(days))
                  }
                  disabled={isInboxSyncing || feedAction !== null}
                >
                  {cachedInboxRetryAction === "load-more"
                    ? "Retry loading older emails"
                    : "Retry inbox sync"}
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
                    <span>
                      Open <strong>More → Authorizations</strong>
                    </span>
                  </div>
                  <div className="auth-step">
                    <span className="auth-step-num">2</span>
                    <span>
                      Select <strong>Google</strong> →{" "}
                      <strong>Connect with OAuth</strong>
                    </span>
                  </div>
                  <div className="auth-step">
                    <span className="auth-step-num">3</span>
                    <span>Tick Gmail Read/Modify/Compose/Send</span>
                  </div>
                  <div className="auth-step">
                    <span className="auth-step-num">4</span>
                    <span>
                      Click <strong>Authorize</strong> and return here
                    </span>
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
          ) : !grouped.some((group) => group.messages.length) ? (
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
                {mailboxView === "inbox" && filter !== "search" ? (
                  <div className="mail-group-label">
                    <span>{group.label}</span>
                    <i />
                    {group.label !== "STARS" && group.label !== "TODOS" ? (
                      <button
                        title="Mark this timeline as done"
                        onClick={() => markTimelineDone(group.messages)}
                      >
                        <AllDoneIcon />
                      </button>
                    ) : null}
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
                      onThreadAction={(operation, message) =>
                        void handleGmailThreadAction(operation, message)
                      }
                      onSnooze={openSnoozePicker}
                      onSelect={() => openMessageDetail(message)}
                      selectable={
                        mailboxView === "drafts" && isDraftMessage(message)
                      }
                      selectedForBatch={selectedComposeDraftIds.has(message.id)}
                      onBatchToggle={() =>
                        setSelectedComposeDraftIds((current) => {
                          const next = new Set(current);
                          const id = message.id;
                          if (next.has(id)) next.delete(id);
                          else next.add(id);
                          return next;
                        })
                      }
                      onComposeDraftDelete={
                        message.id.startsWith("compose:")
                          ? () => {
                              const id = message.id.slice("compose:".length);
                              void actions
                                .deleteComposeDraft(mailbox, id)
                                .then(() => {
                                  setComposeDrafts((drafts) =>
                                    drafts.filter((draft) => draft.id !== id),
                                  );
                                  setSelectedComposeDraftIds((current) => {
                                    const next = new Set(current);
                                    next.delete(message.id);
                                    return next;
                                  });
                                })
                                .catch((reason) =>
                                  actions.showToast(
                                    reason instanceof Error
                                      ? reason.message
                                      : String(reason),
                                  ),
                                );
                            }
                          : undefined
                      }
                    />
                  );
                })}
              </div>
            ))
          )}
          {canLoadMoreInbox && canShowOlderInboxActions ? (
            <button
              className="older-mail-btn"
              onClick={() => void loadMoreInbox()}
              disabled={isInboxSyncing || feedAction !== null}
            >
              {feedAction === "more"
                ? "Loading older emails..."
                : "Show emails older than 30 days"}
            </button>
          ) : null}
          {mailboxView === "trash" ? (
            <footer className="trash-retention-notice">
              <p>Trash is deleted after 30 days.</p>
              <p>
                To empty your trash now,{" "}
                <button
                  type="button"
                  onClick={() => {
                    void copyTextToClipboard(gmailTrashUrl(mailbox))
                      .then(() =>
                        actions.showToast(
                          "Gmail link copied. Paste it into your browser.",
                        ),
                      )
                      .catch(() =>
                        actions.showToast("Could not copy the Gmail link."),
                      );
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
          latestThreadInternalDate={
            latestSelectedThreadMessage?.internal_date || ""
          }
        />
        {composeOpen || composeClosing ? (
          <ComposeView
            mailbox={mailbox}
            initialDraft={composeResumeDraft}
            open={composeOpen}
            onClose={closeComposeDrawer}
            onViewDrafts={() => {
              closeComposeDrawer();
              selectMailboxView("drafts");
            }}
            onScheduleSend={scheduleComposeSend}
            insertRequest={composeInsertRequest}
            onConsumeInsertRequest={(nonce) =>
              setComposeInsertRequest((current) =>
                current?.nonce === nonce ? null : current,
              )
            }
            onOpenAiDraft={openComposeAiDraft}
          />
        ) : null}
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
