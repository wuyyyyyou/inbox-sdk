import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { useApp } from "../../app/AppContext";
import { useI18n, tFallback, type Locale, type MessageKey, type TranslateFn } from "../../i18n";
import type {
  AiChatMessage,
  AiComposeContextRef,
  AiInboxListContext,
  AiMailContextRef,
  AiRoutingIntent,
  AskMailLink,
  ComposeDraftArtifact,
  ComposeDraft,
  DraftReplyArtifact,
  SendPlanArtifact,
  CustomRunResult,
  InboxMessage,
  InboxThreadStateOperation,
  InboxWorkflowState,
  OutgoingAttachmentMeta,
  SendAiMessageOptions,
} from "../../types/mail";
import { SnoozePicker } from "./SnoozePicker";
import { ComposeView } from "./ComposeView";
import { MailDetailDrawer } from "../mail-detail/MailDetailDrawer";
import { resolveMessageThreadId } from "../mail-detail/mailDetailHelpers";
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
  localizeInboxQueryError,
  matchInboxQuery,
  parseInboxQuery,
  splitInboxQueryTokens,
} from "../search/inboxQuery";
import { sortInboxMessagesDesc } from "./inboxMessageOrder";
import { PendingSendScheduler } from "./pendingSend";
import {
  measureAiMessageBlocks,
  parseAiMessageInline,
  parseAiMessageMarkdown,
  sliceAiMessageBlocks,
  type AiMessageBlock,
  type AiMessageInline,
} from "./aiMessageFormatting";
import { customExecutionSteps } from "../brief/runHelpers";
import {
  getCachedComposeDraftDirectory,
  getCachedMessageBody,
  getContactAvatarCache,
  getMailFlags,
  removeCachedInboxThreadDraft,
  setCachedComposeDraftDirectory,
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
  /** Legacy cache fields are retained only for migration compatibility. */
  todos: string[];
  snoozed: string[];
  snoozedUntil?: Record<string, string>;
  done: string[];
  doneRemoved: string[];
  drafts: string[];
  saved: Record<string, InboxMessage>;
};
type CompatibleInboxWorkflow = InboxWorkflowState & { doneRemoved: string[] };
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
/** 时间窗阶梯：从当前 display range 往后推一级 */
const FEED_RANGE_STEPS = [7, 30, 60, INBOX_ALL_TIME_DAYS] as const;
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
/** 列表触底自动加载更多：距底部 ≤ 该像素视为已到底 */
const MAIL_FEED_LOAD_MORE_THRESHOLD_PX = 80;

/** Inbox 标签/Split 角标：超过 99 显示 99+（纯前端展示，不截断真实列表） */
export function formatInboxTabCount(count: number): string {
  const n = Math.max(0, Math.floor(Number(count) || 0));
  return n > 99 ? "99+" : String(n);
}

/** 邮件列表是否已滚到接近底部（用于触底自动分页） */
export function isMailFeedNearBottom(
  scroller: Pick<HTMLElement, "scrollHeight" | "scrollTop" | "clientHeight">,
  thresholdPx = MAIL_FEED_LOAD_MORE_THRESHOLD_PX,
) {
  return (
    scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <=
    thresholdPx
  );
}

/** 下一个更大时间窗；已是 All time 则返回 null */
export function nextFeedRangeDays(currentDays: number): number | null {
  const current = Number(currentDays);
  if (current === INBOX_ALL_TIME_DAYS) return null;
  const exact = FEED_RANGE_STEPS.indexOf(current as (typeof FEED_RANGE_STEPS)[number]);
  if (exact >= 0 && exact < FEED_RANGE_STEPS.length - 1) {
    return FEED_RANGE_STEPS[exact + 1];
  }
  for (const step of FEED_RANGE_STEPS) {
    if (step === INBOX_ALL_TIME_DAYS || step > current) return step;
  }
  return INBOX_ALL_TIME_DAYS;
}

/** 需要底部「扩大时间窗」按钮的远程同步分类（本地 flags 分类除外） */
function isExpandableMailboxView(view: MailboxView): boolean {
  return (
    view === "inbox" ||
    view === "starred" ||
    view === "sent" ||
    view === "trash" ||
    view === "spam" ||
    view === "all"
  );
}
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

const MAILBOX_VIEW_IDS: MailboxView[] = [
  "inbox",
  "todos",
  "starred",
  "snoozed",
  "done",
  "drafts",
  "sent",
  "trash",
  "spam",
  "all",
];

const MAILBOX_VIEW_LABEL_KEYS: Record<MailboxView, MessageKey> = {
  inbox: "mail.folder.inbox",
  todos: "mail.folder.todos",
  starred: "mail.folder.starred",
  snoozed: "mail.folder.snoozed",
  done: "mail.folder.done",
  drafts: "mail.folder.drafts",
  sent: "mail.folder.sent",
  trash: "mail.folder.trash",
  spam: "mail.folder.spam",
  all: "mail.folder.all",
};

function mailboxViewLabel(view: MailboxView, t: TranslateFn): string {
  return t(MAILBOX_VIEW_LABEL_KEYS[view]);
}

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
const MoreDotsIcon = () => (
  <Icon>
    <circle cx="6" cy="12" r="1.4" />
    <circle cx="12" cy="12" r="1.4" />
    <circle cx="18" cy="12" r="1.4" />
  </Icon>
);
const CloseSmallIcon = () => (
  <Icon>
    <path d="M7 7l10 10M17 7 7 17" />
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
  t: TranslateFn = tFallback,
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

  const fallbackName = draft ? t("mail.sender.draft") : t("mail.sender.none");
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

function dateLabel(message: InboxMessage, t: TranslateFn = tFallback, locale: Locale = "en-US") {
  const date = messageDate(message);
  if (!date) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString())
    return date.toLocaleTimeString(locale, {
      hour: "2-digit",
      minute: "2-digit",
    });
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return t("mail.date.yesterday");
  return date.toLocaleDateString(locale, { month: "short", day: "numeric" });
}

function snoozeUntilLabel(value: string | undefined, t: TranslateFn = tFallback, locale: Locale = "en-US") {
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
      ? t("mail.snooze.today")
      : days === 1
        ? t("mail.snooze.tomorrow")
        : date.toLocaleDateString(
            locale,
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
  t: TranslateFn = tFallback,
  locale: Locale = "en-US",
) {
  const date = messageDate(message);
  if (!date) return t("mail.group.last30");
  const now = new Date();
  if (mode === "detailed" && date.toDateString() === now.toDateString())
    return t("mail.group.today");
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (mode === "detailed" && date.toDateString() === yesterday.toDateString())
    return t("mail.group.yesterday");
  const daysAgo = Math.floor(
    (now.getTime() - date.getTime()) / (24 * 60 * 60 * 1000),
  );
  if (daysAgo < 7 && mode !== "months_only")
    return t("mail.group.last7");
  if (mode !== "months_only" && daysAgo < INBOX_LAST_MONTH_DAYS)
    return t("mail.group.earlierThisMonth");
  const currentMonthLabel = date.toLocaleDateString(locale, { month: "long" });
  return t("mail.group.earlierIn", { month: currentMonthLabel });
}

function inboxRangeLabel(days: number, t: TranslateFn = tFallback) {
  if (days === INBOX_ALL_TIME_DAYS) return t("mail.range.allTime");
  if (days === INBOX_LAST_MONTH_DAYS) return t("mail.range.lastDays", { days });
  return t("mail.range.lastDays", { days });
}

function olderRangeButtonLabel(currentDays: number, nextDays: number | null, t: TranslateFn = tFallback) {
  if (nextDays === null) return "";
  if (nextDays === INBOX_ALL_TIME_DAYS) return t("mail.range.showAllOlder");
  if (currentDays <= 0) return t("mail.range.showFromLast", { days: nextDays });
  return t("mail.range.showOlderThan", { current: currentDays, next: nextDays });
}

export function inboxLastSyncedLabel(value?: string, t: TranslateFn = tFallback, locale: Locale = "en-US") {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  // 精确到秒，便于核对自动同步是否刚跑完
  const time = date.toLocaleTimeString(locale, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const datePrefix =
    date.toDateString() === now.toDateString()
      ? ""
      : `${date.toLocaleDateString(locale, { month: "short", day: "numeric" })}, `;
  return t("mail.lastSynced", { time: `${datePrefix}${time}` });
}

export function hasMessageLabel(message: InboxMessage, label: string) {
  const target = String(label || "").toUpperCase();
  return (message.label_ids || []).some(
    (item) => String(item).toUpperCase() === target,
  );
}

/** Gmail 部分返回仅提供 UNREAD 标签，列表展示需兼容两种未读字段。 */
export function isUnreadMessage(message: InboxMessage) {
  return Boolean(message.unread) || hasMessageLabel(message, "UNREAD");
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

function composeDraftBodyPreview(draft: ComposeDraft) {
  return String(draft.body_preview || draft.body || "");
}

function composeDraftHtmlToPlain(html: string) {
  return String(html || "")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&amp;/gi, "&")
    .replace(/\s+\n/g, "\n")
    .replace(/[ \t]{2,}/g, " ")
    .trim();
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

/** 列表投影键：有 thread_id 时按线程折叠（Gmail thread），否则回退 message.id。 */
export function inboxThreadProjectionKey(message: InboxMessage) {
  const mailboxKey = String(message.mailbox || "")
    .trim()
    .toLowerCase();
  const threadKey = String(message.thread_id || "").trim();
  if (threadKey) return `${mailboxKey}:thread:${threadKey}`;
  // compose 草稿无 thread_id，用稳定 id
  return `${mailboxKey}:message:${message.id}`;
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

function inboxMessageAttachmentCount(message: InboxMessage) {
  const attachments = (message as InboxMessage & { attachments?: unknown[] })
    .attachments;
  const listed = Array.isArray(attachments) ? attachments.length : 0;
  const stored = Number(message.attachment_count || 0) || 0;
  const count = Math.max(listed, stored);
  if (count > 0) return count;
  return hasInboxMessageAttachment(message) ? 1 : 0;
}

/** 同一 thread 只保留最新一条，避免同会话多行导致多选/详情错乱。
 *  附件标记按线程 OR：任一封有附件时最新行显示回形针（与 Gmail 一致）。 */
export function uniqueLatestInboxThreads<T extends InboxMessage>(messages: T[]) {
  const threadAttachment = new Map<string, number>();
  for (const message of messages) {
    const key = inboxThreadProjectionKey(message);
    const count = inboxMessageAttachmentCount(message);
    threadAttachment.set(key, Math.max(threadAttachment.get(key) || 0, count));
  }

  const seen = new Set<string>();
  const unique: T[] = [];
  for (const message of sortInboxMessagesDesc(messages)) {
    const key = inboxThreadProjectionKey(message);
    if (seen.has(key)) continue;
    seen.add(key);
    const threadCount = threadAttachment.get(key) || 0;
    if (threadCount > 0 && inboxMessageAttachmentCount(message) < threadCount) {
      unique.push({
        ...message,
        has_attachment: true,
        attachment_count: threadCount,
      });
      continue;
    }
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

/** 将带来源线程的 APS Compose 草稿投影为线程草稿，供收件箱显示“草稿”标记。 */
export function composeDraftOverlayMessages(drafts: ComposeDraft[]): InboxMessage[] {
  return drafts
    .filter((draft) => Boolean(draft.source_thread_id))
    .map((draft) => ({
      id: `compose-overlay:${draft.id}`,
      thread_id: draft.source_thread_id,
      mailbox: draft.mailbox,
      date: draft.updated_at || draft.created_at || "",
      draft_body: composeDraftBodyPreview(draft),
      draft_local: true,
      label_ids: ["DRAFT"],
    }));
}

export function isDoneMessage(
  message: InboxMessage,
  workflow: Pick<InboxWorkflowState, "done"> & { doneRemoved?: string[] },
) {
  if ((workflow.doneRemoved || []).includes(message.id)) return false;
  return workflow.done.includes(message.id) || isSentMessage(message);
}

/** Apply one workflow mutation without relying on a React state updater. */
export function transitionInboxWorkflow(
  current: InboxWorkflowState,
  kind: "todos" | "snoozed" | "done",
  ids: Iterable<string>,
  enabled: boolean,
  snoozeUntil?: string,
): InboxWorkflowState {
  const idSet = new Set([...ids].filter(Boolean));
  const next: InboxWorkflowState = {
    ...current,
    todos: current.todos.filter((id) => !idSet.has(id)),
    snoozed: current.snoozed.filter((id) => !idSet.has(id)),
    done: current.done.filter((id) => !idSet.has(id)),
    snoozedUntil: { ...(current.snoozedUntil || {}) },
  };
  if (enabled) next[kind] = [...next[kind], ...idSet];
  for (const id of idSet) delete next.snoozedUntil[id];
  if (enabled && kind === "snoozed" && snoozeUntil) {
    for (const id of idSet) next.snoozedUntil[id] = snoozeUntil;
  }
  return next;
}

/** Restore only the ids touched by an optimistic action; unrelated workflow state wins. */
export function restoreInboxWorkflow(
  current: InboxWorkflowState,
  previous: InboxWorkflowState,
  ids: Iterable<string>,
): InboxWorkflowState {
  const affected = new Set([...ids].filter(Boolean));
  const next: InboxWorkflowState = {
    ...current,
    todos: current.todos.filter((id) => !affected.has(id)),
    snoozed: current.snoozed.filter((id) => !affected.has(id)),
    done: current.done.filter((id) => !affected.has(id)),
    snoozedUntil: { ...(current.snoozedUntil || {}) },
  };
  for (const kind of ["todos", "snoozed", "done"] as const) {
    next[kind].push(...previous[kind].filter((id) => affected.has(id)));
  }
  for (const id of affected) {
    if (Object.prototype.hasOwnProperty.call(previous.snoozedUntil || {}, id)) {
      next.snoozedUntil[id] = previous.snoozedUntil[id];
    } else {
      delete next.snoozedUntil[id];
    }
  }
  return next;
}

export function expandInboxThreadMessages(
  selected: InboxMessage[],
  messagesInThread: (message: InboxMessage) => InboxMessage[],
) {
  const byId = new Map<string, InboxMessage>();
  for (const message of selected) {
    for (const item of messagesInThread(message)) byId.set(item.id, item);
  }
  return [...byId.values()];
}

export function resolveSourceMessages(
  mailboxView: MailboxView,
  inboxMessages: InboxMessage[],
  inboxSnapshotMessages: InboxMessage[],
  flags: MailUiFlags,
  workflow: Omit<InboxWorkflowState, "snoozedUntil"> & { snoozedUntil?: Record<string, string>; doneRemoved?: string[] } = { todos: [], done: [], snoozed: [] },
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
      workflow[mailboxView]
        .map((id) => currentMessages.get(id))
        .filter(
          (message): message is InboxMessage =>
            message !== undefined && !isTrashMessage(message),
        ),
    );
  }
  if (mailboxView === "done") {
    const doneIds = new Set(workflow.done);
    for (const message of currentMessages.values()) {
      if (isDoneMessage(message, workflow)) doneIds.add(message.id);
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

/**
 * Inbox 搜索（含 is:unread）数据源：Inbox + Todos + Snoozed。
 * 避免 workflow 未读被 inbox 视图排除后搜不到。
 */
export function mergeInboxSearchSourceMessages(
  inboxMessages: InboxMessage[],
  inboxSnapshotMessages: InboxMessage[],
  flags: MailUiFlags,
  workflow: Omit<InboxWorkflowState, "snoozedUntil"> & { snoozedUntil?: Record<string, string>; doneRemoved?: string[] } = { todos: [], done: [], snoozed: [] },
) {
  const inboxResolved = resolveSourceMessages(
    "inbox",
    inboxMessages,
    inboxSnapshotMessages,
    flags,
    workflow,
  );
  const byId = new Map(inboxResolved.map((message) => [message.id, message]));
  const currentMessages = new Map<string, InboxMessage>();
  for (const message of [
    ...Object.values(flags.saved),
    ...inboxSnapshotMessages,
    ...inboxMessages,
  ]) {
    if (message?.id) currentMessages.set(message.id, message);
  }
  for (const id of [...workflow.todos, ...workflow.snoozed]) {
    if (byId.has(id)) continue;
    const message = currentMessages.get(id);
    if (!message || isTrashMessage(message) || isDoneMessage(message, workflow))
      continue;
    byId.set(id, message);
  }
  return uniqueLatestInboxThreads([...byId.values()]);
}

function InboxRow({
  message,
  selected,
  flags,
  workflow,
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
  entering = false,
  onBatchToggle,
  onComposeDraftDelete,
}: {
  message: InboxMessage;
  selected: boolean;
  flags: MailUiFlags;
  workflow: CompatibleInboxWorkflow;
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
  /** 流式加载时的单行入场动画 */
  entering?: boolean;
  onBatchToggle?: () => void;
  onComposeDraftDelete?: () => void;
}) {
  const { actions } = useApp();
  const { t, locale } = useI18n();
  const [avatarFailed, setAvatarFailed] = useState(false);
  const participant = messageParticipant(message, mailboxView, mailbox, t);
  const fallbackAvatar = mailAvatarFallback(
    participant.name || participant.email || participant.initial,
    participant.name || participant.initial,
  );
  const sentView = participant.outgoing;
  const sentMessage = isSentMessage(message);
  const unread = isUnreadMessage(message);
  const isDone = isDoneMessage(message, workflow);
  const isTodo = workflow.todos.includes(message.id);
  const isSnoozed = workflow.snoozed.includes(message.id);
  const trashed = isTrashMessage(message);
  const snoozeLabel =
    !trashed && mailboxView === "snoozed"
      ? snoozeUntilLabel(workflow.snoozedUntil?.[message.id], t)
      : "";
  const important = isImportantMessage(message);
  const starred = isStarredMessage(message);
  const draft = isDraftMessage(message);
  const isComposeDraft = draft && message.id.startsWith("compose:");
  const preview =
    (draft ? message.draft_body : "") ||
    message.snippet ||
    message.body_preview ||
    t("mail.noPreview");
  return (
    <article
      className={`mail-row ${mailboxView === "all" ? "is-all-mail" : ""} ${unread ? "is-unread" : ""} ${selected ? "is-selected" : ""} ${entering ? "is-entering" : ""}`}
    >
      {selectable ? (
        <button
          type="button"
          className="draft-select"
          aria-label={t("mail.action.selectDraft", { subject: message.subject || "draft" })}
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
          <strong>{message.subject || t("mail.noSubject")}</strong>
          <span>
            —{" "}
            {draft && !trashed ? (
              <>
                <b className="mail-draft-label">{t("mail.draftLabel")}</b> {preview}
              </>
            ) : (
              preview
            )}
          </span>
        </span>
        <span className="mail-flags">
          {trashed ? (
            <span className="mail-trash-icon" title={t("mail.badge.trash")}>
              <TrashIcon />
            </span>
          ) : null}
          {!trashed && shouldShowImportantIcon(important, sentView, draft) ? (
            <span className="mail-important-icon" title={t("mail.badge.important")}>
              <ImportantIcon />
            </span>
          ) : null}
          {!trashed && draft ? (
            <span className="mail-draft-icon" title={t("mail.badge.draft")}>
              <DraftIcon />
            </span>
          ) : !trashed && sentMessage ? (
            <span className="mail-sent-badge" title={t("mail.badge.sentDone")}>
              <SentIcon />
              <span className="mail-sent-check">
                <CheckIcon />
              </span>
            </span>
          ) : !trashed && isDone ? (
            <span className="mail-sent-check" title={t("mail.badge.done")}>
              <CheckIcon />
            </span>
          ) : null}
          {!trashed && starred ? (
            <span className="mail-starred" title={t("mail.badge.starred")}>
              <StarIcon />
            </span>
          ) : null}
          {hasInboxMessageAttachment(message) ? (
            <span
              className="mail-attachment"
              title={t("mail.badge.attachments", { count: message.attachment_count || 1 })}
            >
              <PaperclipIcon />
            </span>
          ) : null}
        </span>
        {snoozeLabel ? (
          <span
            className="mail-snooze-until"
            title={t("mail.badge.snoozedUntil", { time: snoozeLabel })}
          >
            <ClockIcon />
            <span>{snoozeLabel}</span>
          </span>
        ) : (
          <time>{dateLabel(message, t, locale)}</time>
        )}
      </button>
      <span className="mail-row-actions">
        {isComposeDraft ? (
          <button
            aria-label={t("mail.action.deleteDraft")}
            data-tooltip={t("mail.action.deleteDraft")}
            onClick={onComposeDraftDelete}
          >
            <TrashIcon />
          </button>
        ) : trashed ? (
          <button
            className="is-trashed"
            aria-label={t("mail.action.removeFromTrash")}
            data-tooltip={t("mail.action.removeFromTrash")}
            onClick={() => onThreadAction("untrash", message)}
          >
            <TrashOffIcon />
          </button>
        ) : (
          <>
            <button
              className={starred ? "is-active is-starred" : ""}
              aria-label={starred ? t("mail.action.unstar") : t("mail.action.star")}
              data-tooltip={starred ? t("mail.action.unstar") : t("mail.action.star")}
              onClick={() =>
                onThreadAction(starred ? "unstar" : "star", message)
              }
            >
              <StarIcon />
            </button>
            <button
              className={important ? "is-active is-important" : ""}
              aria-label={important ? t("mail.action.markNotImportant") : t("mail.action.markImportant")}
              data-tooltip={important ? t("mail.action.markNotImportant") : t("mail.action.markImportant")}
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
              aria-label={isTodo ? t("mail.action.removeTodo") : t("mail.action.addTodo")}
              data-tooltip={isTodo ? t("mail.action.removeTodo") : t("mail.action.addTodo")}
              disabled={isTodo}
              onClick={() => onFlag("todos", message)}
            >
              <TodoIcon />
            </button>
            <button
              className={isSnoozed ? "is-active is-snoozed" : ""}
              aria-label={isSnoozed ? t("mail.action.removeSnooze") : t("mail.action.snooze")}
              data-tooltip={isSnoozed ? t("mail.action.removeSnooze") : t("mail.action.snooze")}
              onClick={() => onSnooze(message)}
            >
              <ClockIcon />
            </button>
            {unread ? (
              <button
                aria-label={t("mail.action.markRead")}
                data-tooltip={t("mail.action.markRead")}
                onClick={() => void actions.markInboxRead(message.id)}
              >
                <MailOpenIcon />
              </button>
            ) : null}
            <button
              aria-label={t("mail.action.moveToTrash")}
              data-tooltip={t("mail.action.moveToTrash")}
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

export function aiSearchStatus(result: CustomRunResult, userRequest = "") {
  const itemCount = aiResultCount(result);
  // 搜索状态是本地 UI 文案，应以用户请求为准，不能被模型返回的单个异常标题改变语言。
  const chinese = userRequest
    ? /[\u3400-\u9fff]/.test(userRequest)
    : /[\u3400-\u9fff]/.test(
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
      ? `找到 ${itemCount} 封相关邮件。`
      : `Found ${itemCount} relevant email${itemCount === 1 ? "" : "s"}.`;
  const queryCount = result.plan_gmail_queries?.length || 0;
  if (queryCount > 0)
    return chinese
      ? `已在本地缓存执行 ${queryCount} 个查询。`
      : `Searched local cache with ${queryCount} quer${queryCount === 1 ? "y" : "ies"}.`;
  return chinese ? "已完成本地缓存搜索。" : "Finished searching local inbox cache.";
}

/** Thinking 后展示检索依据（对齐 example/12.png：小字 + 搜索图标行，可点进主搜索框）。 */
function AiScanQueryChip({
  query,
  chinese,
  hitCount,
  onApply,
}: {
  query: string;
  chinese: boolean;
  hitCount?: number;
  onApply?: (query: string) => void;
}) {
  const text = query.trim();
  if (!text) return null;
  const countLine =
    typeof hitCount === "number" && hitCount > 0
      ? chinese
        ? `找到 ${hitCount} 封相关邮件`
        : `Found ${hitCount} matching email${hitCount === 1 ? "" : "s"}`
      : "";
  return (
    <div className="ai-scan-query-block">
      {countLine ? <p className="ai-scan-query-summary">{countLine}</p> : null}
      <button
        type="button"
        className="ai-scan-query-line"
        title={chinese ? "在收件箱搜索框中打开此条件" : "Open this query in inbox search"}
        onClick={() => onApply?.(text)}
      >
        <span className="ai-scan-query-icon" aria-hidden="true">
          <SearchIcon />
        </span>
        <span className="ai-scan-query-prefix">
          {chinese ? "搜索条件：" : "Search query: "}
        </span>
        <span className="ai-scan-query-text">{text}</span>
      </button>
    </div>
  );
}

function resolveMessageScanQuery(message: AiChatMessage): string {
  if (String(message.scanQuery || "").trim()) return String(message.scanQuery).trim();
  const fromResult = message.result?.scan_query
    || message.result?.plan_gmail_queries?.[0]?.query
    || "";
  return String(fromResult || "").trim();
}

function resolveMessageScanHitCount(message: AiChatMessage): number | undefined {
  const sections = message.result?.sections;
  if (Array.isArray(sections)) {
    const n = sections.reduce(
      (total, section) => total + (section.items?.length || 0),
      0,
    );
    if (n > 0) return n;
  }
  return undefined;
}

function aiTimeLabel(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function aiThinkingElapsedLabel(startedAt?: string, endedAt = Date.now()) {
  const startedMs = new Date(startedAt || "").getTime();
  if (!Number.isFinite(startedMs)) return "";
  const elapsedSeconds = Math.max(0, Math.floor((endedAt - startedMs) / 1000));
  const minutes = Math.floor(elapsedSeconds / 60);
  const seconds = elapsedSeconds % 60;
  return minutes ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

function AiThinkingElapsed({ startedAt }: { startedAt?: string }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  const elapsed = aiThinkingElapsedLabel(startedAt, now);
  return elapsed ? <span className="ai-thinking-elapsed">{elapsed}</span> : null;
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

function isDraftContentOnlyRequest(value: string) {
  return /(?:只(?:输出|提供|给我|要)|仅(?:输出|提供|给我|要)|不要卡片|不要生成卡片)(?:邮件)?(?:草稿)?(?:内容|正文)|draft\s+(?:content|body)\s+only|body\s+only/i.test(value);
}

function displayAssistantText(message: AiChatMessage) {
  const text = message.content || "";
  // 模型不支持图片输入时可能把底层错误直接回传到侧栏；不要把协议错误当成助手正文展示。
  if (/error:\s*cannot read ["']?image\.png["']?\s*\(this model does not support image input\)/i.test(text)
    || /cannot read ["']?image\.png["']?\s*\(this model does not support image input\)/i.test(text)) {
    return [
      "## Image attachment unavailable",
      "",
      "- This AI model cannot read image attachments.",
      "- Choose a text-capable model or remove the image, then try again.",
    ].join("\n");
  }
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

function truncateThreadReferenceLabel(subject: string) {
  const normalized = subject.replace(/\s+/g, " ").trim();
  const limit = 48;
  return normalized.length > limit ? `${normalized.slice(0, limit - 3).trimEnd()}...` : normalized;
}

export function renderAiUserMessageContent(text: string) {
  return String(text || "").split(/\r\n?|\n/).map((line, index) => (
    <Fragment key={`user-line-${index}`}>
      {index ? <br /> : null}
      {line}
    </Fragment>
  ));
}

function AiMessageInlineContent({
  content,
  onOpenThread,
  threadReferenceLabels,
}: {
  content: AiMessageInline[];
  onOpenThread?: (threadId: string) => void;
  threadReferenceLabels?: Record<string, string>;
}) {
  return content.map((node, index) => {
    const key = `${node.type}-${index}`;
    if (node.type === "bold") return <strong key={key}>{node.value}</strong>;
    if (node.type === "italic") return <em key={key}>{node.value}</em>;
    if (node.type === "strikethrough") return <del key={key}>{node.value}</del>;
    if (node.type === "code") return <code key={key}>{node.value}</code>;
    if (node.type === "link") {
      return (
        <a key={key} href={node.href} target="_blank" rel="noreferrer noopener">
          <AiMessageInlineContent
            content={parseAiMessageInline(node.label)}
            onOpenThread={onOpenThread}
            threadReferenceLabels={threadReferenceLabels}
          />
        </a>
      );
    }
    if (node.type === "thread_ref") {
      const fullLabel = threadReferenceLabels?.[node.threadId] || "Open email";
      const label = truncateThreadReferenceLabel(fullLabel);
      return (
        <button
          key={key}
          type="button"
          className="ai-thread-reference"
          title={fullLabel}
          onClick={() => onOpenThread?.(node.threadId)}
        >
          {label}
        </button>
      );
    }
    return node.value;
  });
}

function RichAssistantBlocks({
  blocks,
  onOpenThread,
  threadReferenceLabels,
}: {
  blocks: AiMessageBlock[];
  onOpenThread?: (threadId: string) => void;
  threadReferenceLabels?: Record<string, string>;
}) {
  return (
    <div className="ai-message-rich-text">
      {blocks.map((block, index) => {
        const key = `${block.type}-${index}`;
        if (block.type === "heading") {
          const Heading = `h${block.level + 2}` as "h3" | "h4" | "h5" | "h6";
          return (
            <Heading key={key}>
              <AiMessageInlineContent
                content={block.content}
                onOpenThread={onOpenThread}
                threadReferenceLabels={threadReferenceLabels}
              />
            </Heading>
          );
        }
        if (block.type === "unordered_list") {
          return (
            <ul key={key} style={{ marginLeft: block.indent ? `${block.indent * 0.5}rem` : undefined }}>
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>
                  <AiMessageInlineContent
                    content={item}
                    onOpenThread={onOpenThread}
                    threadReferenceLabels={threadReferenceLabels}
                  />
                </li>
              ))}
            </ul>
          );
        }
        if (block.type === "ordered_list") {
          return (
            <ol key={key} start={block.start} style={{ marginLeft: block.indent ? `${block.indent * 0.5}rem` : undefined }}>
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>
                  <AiMessageInlineContent
                    content={item}
                    onOpenThread={onOpenThread}
                    threadReferenceLabels={threadReferenceLabels}
                  />
                </li>
              ))}
            </ol>
          );
        }
        if (block.type === "metadata") {
          return (
            <div key={key} className="ai-message-metadata-row">
              {block.label ? <span className="ai-message-metadata-label">{block.label}</span> : null}
              <span className="ai-message-metadata-value">
                <AiMessageInlineContent content={block.content} onOpenThread={onOpenThread} threadReferenceLabels={threadReferenceLabels} />
              </span>
            </div>
          );
        }
        if (block.type === "blockquote") {
          return (
            <blockquote key={key}>
              <AiMessageInlineContent content={block.content} onOpenThread={onOpenThread} threadReferenceLabels={threadReferenceLabels} />
            </blockquote>
          );
        }
        if (block.type === "code_block") {
          return <pre key={key}><code className={block.language ? `language-${block.language}` : undefined}>{block.code}</code></pre>;
        }
        if (block.type === "divider") return <hr key={key} />;
        return (
          <p key={key}>
            <AiMessageInlineContent
              content={block.content}
              onOpenThread={onOpenThread}
              threadReferenceLabels={threadReferenceLabels}
            />
          </p>
        );
      })}
    </div>
  );
}

function RichAssistantText({
  text,
  onOpenThread,
  threadReferenceLabels,
}: {
  text: string;
  onOpenThread?: (threadId: string) => void;
  threadReferenceLabels?: Record<string, string>;
}) {
  return (
    <RichAssistantBlocks
      blocks={parseAiMessageMarkdown(text)}
      onOpenThread={onOpenThread}
      threadReferenceLabels={threadReferenceLabels}
    />
  );
}

function AnimatedAssistantText({
  text,
  animate,
  onComplete,
  onOpenThread,
  threadReferenceLabels,
}: {
  text: string;
  animate: boolean;
  onComplete?: () => void;
  onOpenThread?: (threadId: string) => void;
  threadReferenceLabels?: Record<string, string>;
}) {
  // Parse full markdown once so typewriter never re-parses incomplete prefixes.
  const blocks = useMemo(() => parseAiMessageMarkdown(text), [text]);
  const totalChars = useMemo(() => measureAiMessageBlocks(blocks), [blocks]);
  const [visibleChars, setVisibleChars] = useState(() =>
    animate ? 0 : totalChars,
  );
  const onCompleteRef = useRef(onComplete);

  useEffect(() => {
    onCompleteRef.current = onComplete;
  }, [onComplete]);

  useEffect(() => {
    if (!animate) {
      setVisibleChars(totalChars);
      onCompleteRef.current?.();
      return;
    }
    if (!totalChars) {
      setVisibleChars(0);
      onCompleteRef.current?.();
      return;
    }
    setVisibleChars(0);
    let frame = 0;
    const step = Math.max(1, Math.ceil(totalChars / 36));
    const timer = window.setInterval(() => {
      frame += step;
      if (frame >= totalChars) {
        window.clearInterval(timer);
        setVisibleChars(totalChars);
        onCompleteRef.current?.();
        return;
      }
      setVisibleChars(frame);
    }, 24);
    return () => window.clearInterval(timer);
  }, [animate, text, totalChars]);

  const visibleBlocks =
    !animate || visibleChars >= totalChars
      ? blocks
      : sliceAiMessageBlocks(blocks, visibleChars);

  return (
    <RichAssistantBlocks blocks={visibleBlocks} onOpenThread={onOpenThread} threadReferenceLabels={threadReferenceLabels} />
  );
}

function DraftReplyArtifactCard({
  artifact,
  onUse,
}: {
  artifact: DraftReplyArtifact;
  onUse: (artifact: DraftReplyArtifact, mode: "append" | "replace") => void;
}) {
  const { t } = useI18n();
  const [draft, setDraft] = useState(artifact);
  const messageRef = useRef<HTMLTextAreaElement | null>(null);
  const isForward = draft.composer_mode === "forward";

  useLayoutEffect(() => {
    const textarea = messageRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${textarea.scrollHeight}px`;
  }, [draft.body]);

  return (
    <div className="ai-draft-artifact">
      <strong className="ai-draft-artifact-title">{isForward ? t("ai.forwardDraft") : t("ai.replyDraft")}</strong>
      <label className="ai-draft-artifact-field">
        <span>{t("ai.draftTo")}</span>
        <input
          value={(isForward ? (draft.recipients || []) : []).join(", ")}
          placeholder={isForward ? t("ai.addRecipient") : t("ai.replyRecipientFromThread")}
          readOnly={!isForward}
          onChange={(event) => setDraft((current) => ({
            ...current,
            recipients: event.target.value.split(",").map((value) => value.trim()).filter(Boolean),
          }))}
        />
      </label>
      <label className="ai-draft-artifact-field">
        <span>{t("ai.draftSubject")}</span>
        <input value={draft.subject || ""} placeholder={t("ai.threadSubject")} readOnly />
      </label>
      <label className="ai-draft-artifact-field">
        <span>{t("ai.draftMessage")}</span>
        <textarea
          ref={messageRef}
          value={draft.body}
          onChange={(event) => setDraft((current) => ({ ...current, body: event.target.value }))}
          rows={1}
        />
      </label>
      <div className="ai-draft-artifact-actions">
        <button className="is-primary" onClick={() => onUse(draft, "replace")}>{t("ai.insertNewEmail")}</button>
        <button className="is-secondary" onClick={() => onUse(draft, "append")}>{t("ai.append")}</button>
      </div>
    </div>
  );
}

function ComposeDraftArtifactCard({
  artifact,
  onUse,
}: {
  artifact: ComposeDraftArtifact;
  onUse: (artifact: ComposeDraftArtifact) => void;
}) {
  const [draft, setDraft] = useState(artifact);
  const { t } = useI18n();
  return (
    <div className="ai-draft-artifact">
      <strong className="ai-draft-artifact-title">{t("mail.compose")}</strong>
      <label className="ai-draft-artifact-field">
        <span>{t("compose.to")}</span>
        <input value={(draft.recipients || []).join(", ")} placeholder={t("ai.addRecipient")} onChange={(event) => setDraft((current) => ({ ...current, recipients: event.target.value.split(",").map((value) => value.trim()).filter(Boolean) }))} />
      </label>
      <label className="ai-draft-artifact-field">
        <span>{t("compose.subject")}</span>
        <input value={draft.subject || ""} placeholder={t("ai.addSubject")} onChange={(event) => setDraft((current) => ({ ...current, subject: event.target.value }))} />
      </label>
      <label className="ai-draft-artifact-field">
        <span>{t("compose.content")}</span>
        <textarea className="ai-draft-artifact-body" rows={8} value={draft.body} onChange={(event) => setDraft((current) => ({ ...current, body: event.target.value }))} />
      </label>
      <div className="ai-draft-artifact-actions">
        <button className="is-primary" onClick={() => onUse(draft)}>{t("ai.insertNewEmail")}</button>
      </div>
    </div>
  );
}

function BatchComposeDraftArtifacts({
  artifacts,
  onSave,
}: {
  artifacts: ComposeDraftArtifact[];
  onSave: (drafts: ComposeDraftArtifact[]) => Promise<ComposeDraftArtifact[]>;
}) {
  const { t } = useI18n();
  const [drafts, setDrafts] = useState(artifacts);
  const [saving, setSaving] = useState(false);
  const [savedIds, setSavedIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    setDrafts((current) => artifacts.map((draft, index) => {
      const previous = current[index];
      return {
        ...draft,
        id: draft.id || previous?.id || `ai-compose-${index}`,
        etag: draft.etag || previous?.etag,
      };
    }));
  }, [artifacts]);

  const updateDraft = (index: number, patch: Partial<ComposeDraftArtifact>) => {
    setDrafts((current) => current.map((draft, currentIndex) => (
      currentIndex === index ? { ...draft, ...patch } : draft
    )));
    setSavedIds((current) => {
      const next = new Set(current);
      if (drafts[index]?.id) next.delete(drafts[index].id);
      return next;
    });
  };

  const saveAll = async () => {
    if (savedIds.size && !window.confirm(t("ai.reSaveDraftsConfirm", { count: savedIds.size }))) {
      return;
    }
    setSaving(true);
    try {
      const savedDrafts = await onSave(drafts);
      setDrafts(savedDrafts);
      setSavedIds(new Set(savedDrafts.map((draft) => draft.id).filter((id): id is string => Boolean(id))));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="ai-batch-draft-artifacts">
      {drafts.map((draft, index) => (
        <div className="ai-draft-artifact" key={`${draft.recipients?.join("-") || "compose"}-${index}`}>
          <strong className="ai-draft-artifact-title">{t("ai.draftFallbackTitle", { number: index + 1 })}</strong>
          <label className="ai-draft-artifact-field">
            <span>{t("compose.to")}</span>
            <input value={(draft.recipients || []).join(", ")} onChange={(event) => updateDraft(index, { recipients: event.target.value.split(",").map((value) => value.trim()).filter(Boolean) })} />
          </label>
          <label className="ai-draft-artifact-field">
            <span>{t("compose.subject")}</span>
            <input value={draft.subject || ""} onChange={(event) => updateDraft(index, { subject: event.target.value })} />
          </label>
          <label className="ai-draft-artifact-field">
            <span>{t("compose.content")}</span>
            <textarea className="ai-draft-artifact-body" rows={8} value={draft.body} onChange={(event) => updateDraft(index, { body: event.target.value })} />
          </label>
        </div>
      ))}
      <div className="ai-draft-artifact-actions">
        <button className="is-primary" onClick={() => void saveAll()} disabled={saving}>
          {saving ? t("ai.saveDraftsSaving") : savedIds.size ? t("ai.reSaveDrafts") : t("ai.saveDrafts")}
        </button>
        {savedIds.size ? <span>{t("ai.saveDraftsSaved", { count: savedIds.size })}</span> : null}
      </div>
    </div>
  );
}

function AiAssistantMessage({
  message,
  currentMailContext,
  onUseArtifact,
  onUseComposeArtifact,
  onSaveComposeArtifacts,
  onOpenMail,
  onConfirmSendPlan,
  onTextComplete,
  onApplyScanQuery,
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
  onSaveComposeArtifacts: (artifacts: ComposeDraftArtifact[]) => Promise<ComposeDraftArtifact[]>;
  onOpenMail: (target: AskMailLink) => void;
  onConfirmSendPlan: (plan: SendPlanArtifact) => void;
  onTextComplete?: () => void;
  onApplyScanQuery?: (query: string) => void;
}) {
  const { state, actions } = useApp();
  const { t } = useI18n();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submittingGap, setSubmittingGap] = useState(false);
  const [assistantTextComplete, setAssistantTextComplete] = useState(
    () => !shouldAnimateAssistantText(message.timestamp),
  );
  const [clarificationInput, setClarificationInput] = useState("");
  const [selectedSearchField, setSelectedSearchField] = useState("");
  const [clarificationSubmitting, setClarificationSubmitting] = useState(false);
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
        markDoneQ: (n: number) => `Mark ${n} email${n === 1 ? "" : "s"} as done?`,
        archiveQ: (n: number) => `Archive ${n} email${n === 1 ? "" : "s"}?`,
        trashQ: (n: number) => `Move ${n} email${n === 1 ? "" : "s"} to trash?`,
        marked: (n: number) => `Marked ${n} email${n === 1 ? "" : "s"} as done`,
        archived: (n: number) => `Archived ${n} email${n === 1 ? "" : "s"}`,
        trashed: (n: number) => `Moved ${n} email${n === 1 ? "" : "s"} to trash`,
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
      actions.showToast(t("toast.emailReferenceUnavailable"));
      return;
    }
    onOpenMail({
      label: "Referenced email",
      mailbox: referenceMailbox,
      thread_id: threadId,
      message_id: "",
    });
  };

  if (message.pending) {
    const pendingDraftArtifacts = message.artifacts || [];
    const pendingComposeArtifacts = message.composeArtifacts || [];
    const executionSteps = message.kind === "scan" && state.customRunProgress
      ? customExecutionSteps(state.customRunProgress.stage, state.customRunProgress.progress)
      : [];
    return (
      <div
        className="ai-message is-assistant is-thinking-inline has-draft-artifact"
        aria-live="polite"
        aria-busy="true"
      >
        {pendingDraftArtifacts.map((item, index) => (
          <DraftReplyArtifactCard
            key={`${item.thread_id}-${index}`}
            artifact={item}
            onUse={onUseArtifact}
          />
        ))}
        {pendingComposeArtifacts.map((item, index) => (
          <ComposeDraftArtifactCard key={`${item.recipients?.join("-") || "compose"}-${index}`} artifact={item} onUse={(artifact) => onUseComposeArtifact(artifact, null)} />
        ))}
        {executionSteps.length ? executionSteps.map((step) => (
          <p key={step.label}>{step.status === "complete" ? "Completed: " : step.status === "active" ? "In progress: " : ""}{step.label}</p>
        )) : <p>{t("ai.thinking")}</p>}
        <div className="ai-message-footer">
          <time>{aiTimeLabel(message.timestamp)}</time>
          <AiThinkingElapsed startedAt={message.thinkingStartedAt || message.timestamp} />
        </div>
      </div>
    );
  }

  const result = message.result;
  if (!result) {
    const text = displayAssistantText(message);
    const animate = shouldAnimateAssistantText(message.timestamp);
    const scanQuery = resolveMessageScanQuery(message);
    const scanChinese = /[\u3400-\u9fff]/.test(
      `${message.sourcePrompt || ""}${text}`,
    );
    const draftArtifact =
      message.artifact?.type === "draft_reply" &&
      !isDraftContentOnlyRequest(message.sourcePrompt || "") &&
        (!animate || assistantTextComplete)
        ? message.artifact
        : null;
    const cardDraftArtifact = draftArtifact;
    const composeArtifact =
      message.artifact?.type === "compose_draft" &&
      (!animate || assistantTextComplete)
        ? message.artifact
        : null;
    const composeArtifacts = message.composeArtifacts || (composeArtifact ? [composeArtifact] : []);
    // 草稿卡片存在时，把回答文字置于末尾，保证侧栏自动滚动后仍能看到状态与续批提示。
    const hasDraftArtifacts = Boolean(
      message.artifacts?.length ||
      composeArtifacts.length ||
      message.artifact?.type === "draft_reply" ||
      message.artifact?.type === "compose_draft",
    );
    const sendPlan =
      message.artifact?.type === "send_plan" &&
      (!animate || assistantTextComplete)
        ? message.artifact
        : null;
    const summaryLink = message.mailSummaryLink;
    const clarification = message.clarification;
    const submitClarification = async (actionId?: string) => {
      if (!clarification || clarificationSubmitting) return;
      const customInput = clarificationInput.trim();
      const searchField = actionId || selectedSearchField;
      const isSearchFieldClarification = clarification.kind === "search_field";
      if (
        isSearchFieldClarification
        && ["search_participants", "search_date"].includes(searchField)
        && !customInput
      ) {
        setSelectedSearchField(searchField);
        return;
      }
      const routingIntent = actionId && ["inbox", "current_thread", "compose", "chat"].includes(actionId)
        ? actionId as AiRoutingIntent
        : undefined;
      if (!isSearchFieldClarification && !routingIntent && !customInput) return;
      const searchFieldInstruction: Record<string, string> = {
        search_subject: "按邮件主题搜索",
        search_body: "按邮件正文内容搜索",
        search_participants: "按发件人或收件人搜索",
        search_date: "按日期范围搜索",
      };
      const searchFieldOption: Record<string, SendAiMessageOptions["searchField"]> = {
        search_subject: "subject",
        search_body: "body",
        search_participants: "participants",
        search_date: "date",
      };
      const prompt = isSearchFieldClarification
        ? [searchFieldInstruction[searchField], customInput].filter(Boolean).join("：")
        : customInput || clarification.original_input;
      const resolvedMessages = state.aiChatMessages.map((item) => (
        item.id === message.id && item.clarification?.status === "pending"
          ? {
              ...item,
              clarification: {
                ...item.clarification,
                status: "resolved" as const,
                resolved_action: searchField || routingIntent || "custom",
              },
            }
          : item
      ));
      setClarificationSubmitting(true);
      try {
        actions.resolveAiClarification(message.id, searchField || routingIntent || "custom");
        await actions.sendAiChatMessage({
          prompt,
          baseMessages: resolvedMessages,
          routingIntent: isSearchFieldClarification || customInput ? undefined : routingIntent,
          searchField: isSearchFieldClarification ? searchFieldOption[searchField] : undefined,
          agentPrompt: isSearchFieldClarification
            ? `${clarification.original_input}\n搜索条件：${searchFieldInstruction[searchField]}。${customInput ? `\n补充条件：${customInput}` : ""}`
            : undefined,
        });
      } finally {
        setClarificationSubmitting(false);
      }
    };
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
        actions.showToast(t("toast.answerQuestionsFirst"));
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
        className={`ai-message is-assistant ${message.kind === "error" ? "is-error" : ""} ${message.kind === "stopped" ? "is-stopped" : ""} ${(message.artifacts?.length || composeArtifacts.length) ? "has-draft-artifact" : ""}`}
      >
        {scanQuery ? (
          <AiScanQueryChip
            query={scanQuery}
            chinese={scanChinese}
            hitCount={resolveMessageScanHitCount(message)}
            onApply={onApplyScanQuery}
          />
        ) : null}
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
        {!hasDraftArtifacts ? (
          <AnimatedAssistantText
            text={text}
            animate={animate}
            onComplete={() => {
              setAssistantTextComplete(true);
              onTextComplete?.();
            }}
            onOpenThread={openThreadReference}
            threadReferenceLabels={message.threadReferenceLabels}
          />
        ) : null}
        {clarification && clarification.status === "pending" ? (
          <div className="ai-clarification ai-routing-dialog" role="group" aria-label={clarification.question}>
            {!selectedSearchField ? <p>{clarification.question}</p> : null}
            {!selectedSearchField ? (
              <div className="ai-clarification-actions">
                {clarification.actions.map((action) => (
                  <button
                    key={action.id}
                    type="button"
                    aria-pressed={clarification.kind === "search_field" && selectedSearchField === action.id}
                    disabled={clarificationSubmitting || state.aiChatLoading}
                    onClick={() => void submitClarification(action.id)}
                  >
                    {action.label}
                  </button>
                ))}
              </div>
            ) : null}
            {clarification.freeform_enabled ? (
              <div className="ai-routing-custom">
                <textarea
                  value={clarificationInput}
                  disabled={clarificationSubmitting || state.aiChatLoading}
                  placeholder={selectedSearchField === "search_participants"
                    ? "输入发件人或收件人"
                    : selectedSearchField === "search_date"
                      ? "输入日期或时间范围"
                      : "补充搜索条件"}
                  onChange={(event) => setClarificationInput(event.target.value)}
                />
                <div className="ai-routing-footer">
                  <button
                    type="button"
                    disabled={!clarificationInput.trim() || clarificationSubmitting || state.aiChatLoading}
                    onClick={() => void submitClarification()}
                  >
                    {clarification.kind === "search_field" ? "搜索" : "Continue"}
                  </button>
                  <button
                    type="button"
                    className="ai-routing-dismiss"
                    disabled={clarificationSubmitting}
                    onClick={() => actions.dismissAiClarification(message.id)}
                  >
                    {/[\u3400-\u9fff]/.test(clarification.question) ? "忽略" : "Dismiss"}
                  </button>
                </div>
              </div>
            ) : (
              <button
                type="button"
                className="ai-routing-dismiss"
                disabled={clarificationSubmitting}
                onClick={() => actions.dismissAiClarification(message.id)}
              >
                {/[\u3400-\u9fff]/.test(clarification.question) ? "忽略" : "Dismiss"}
              </button>
            )}
          </div>
        ) : clarification && clarification.status === "resolved" ? (
          <div className="ai-clarification is-resolved">
            {`已选择：${clarification.actions.find((action) => action.id === clarification.resolved_action)?.label || ""}`}
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
                        {item.subject || item.thread_id || item.message_id || "Email"}
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
              threadReferenceLabels={message.threadReferenceLabels}
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
        {(() => {
          // 批量写稿：优先 artifacts；单封仍用 artifact
          const batchArtifacts =
            Array.isArray(message.artifacts) && message.artifacts.length
              ? message.artifacts
              : cardDraftArtifact
                ? [cardDraftArtifact]
                : [];
          if (!batchArtifacts.length || (animate && !assistantTextComplete)) return null;
            return batchArtifacts.map((item, index) => {
              const itemOpen = Boolean(
              currentMailContext &&
              currentMailContext.kind === "gmail_thread" &&
              currentMailContext.mailbox.trim().toLowerCase() ===
                item.mailbox.trim().toLowerCase() &&
              currentMailContext.thread_id === item.thread_id,
            );
            return (
              <div key={`${item.thread_id}-${index}`}>
                {itemOpen ? (
                  <DraftReplyArtifactCard artifact={item} onUse={onUseArtifact} />
                ) : (
                  <div className="ai-draft-artifact">
                    <strong className="ai-draft-artifact-title">{item.subject || item.thread_id || t("ai.draftFallbackTitle", { number: index + 1 })}</strong>
                    <pre>{item.body}</pre>
                    <div className="ai-draft-artifact-actions">
                    <button
                      className="is-primary"
                      onClick={() =>
                        onOpenMail({
                          label: item.subject || t("ai.draftEmailLabel"),
                          mailbox: item.mailbox,
                          thread_id: item.thread_id,
                          message_id: item.message_id || "",
                        })
                      }
                    >
                      {t("ai.goToEmail")}
                    </button>
                    <button className="is-secondary" onClick={() => void actions.copyDraft(item.body)}>{t("ai.copyDraft")}</button>
                    </div>
                  </div>
                )}
              </div>
            );
          });
        })()}
        {composeArtifacts.length > 1 ? (
          <BatchComposeDraftArtifacts artifacts={composeArtifacts} onSave={onSaveComposeArtifacts} />
        ) : composeArtifacts.map((item, index) => (
          <ComposeDraftArtifactCard
            key={`${item.recipients?.join("-") || "compose"}-${index}`}
            artifact={item}
            onUse={(artifact) => onUseComposeArtifact(
              artifact,
              message.mailContext?.kind === "compose" ? message.mailContext : null,
            )}
          />
        ))}
        {sendPlan ? (
          <div className="ai-draft-artifact ai-send-plan">
            <strong>{t("ai.reviewBeforeSending")}</strong>
            {sendPlan.messages.map((item, index) => (
              <div key={index}>
                <p>
                  <b>{t("compose.to")}:</b> {item.recipients.join(", ") || t("ai.missingRecipient")}
                </p>
                <p>
                  <b>{t("compose.subject")}:</b> {item.subject || t("ai.missingSubject")}
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
                  placeholder={question.hint || t("ai.yourAnswer")}
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
        {hasDraftArtifacts ? (
          <AnimatedAssistantText
            text={text}
            animate={animate}
            onComplete={() => {
              setAssistantTextComplete(true);
              onTextComplete?.();
            }}
            onOpenThread={openThreadReference}
            threadReferenceLabels={message.threadReferenceLabels}
          />
        ) : null}
        <div className="ai-message-footer">
          <time>{aiTimeLabel(message.timestamp)}</time>
          {message.thinkingStartedAt ? <span className="ai-thinking-elapsed">{aiThinkingElapsedLabel(message.thinkingStartedAt, new Date(message.timestamp).getTime())}</span> : null}
          {message.kind === "error" && !hasDraftArtifacts ? (
            <button
              type="button"
              className="ai-retry-button"
              aria-label={t("ai.retry")}
              data-tooltip={t("ai.retry")}
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
  const scanQuery = resolveMessageScanQuery(message);
  const scanChinese = /[\u3400-\u9fff]/.test(
    `${message.sourcePrompt || ""}${summaryText}`,
  );
  return (
    <div className="ai-message is-assistant">
      <div className="ai-answer-meta">
        {aiSearchStatus(result, message.sourcePrompt)}
      </div>
      {scanQuery ? (
        <AiScanQueryChip
          query={scanQuery}
          chinese={scanChinese}
          hitCount={resolveMessageScanHitCount(message)}
          onApply={onApplyScanQuery}
        />
      ) : null}
      <AnimatedAssistantText
        text={summaryText}
        animate={animate}
        onOpenThread={openThreadReference}
        threadReferenceLabels={message.threadReferenceLabels}
        onComplete={onTextComplete}
      />
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
      <div className="ai-message-footer">
        <time>{aiTimeLabel(message.timestamp)}</time>
        {message.thinkingStartedAt ? <span className="ai-thinking-elapsed">{aiThinkingElapsedLabel(message.thinkingStartedAt, new Date(message.timestamp).getTime())}</span> : null}
      </div>
    </div>
  );
}

function AiMessageBubble({
  message,
  currentMailContext,
  onUseArtifact,
  onUseComposeArtifact,
  onSaveComposeArtifacts,
  onOpenMail,
  onConfirmSendPlan,
  onTextComplete,
  onApplyScanQuery,
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
  onSaveComposeArtifacts: (artifacts: ComposeDraftArtifact[]) => Promise<ComposeDraftArtifact[]>;
  onOpenMail: (target: AskMailLink) => void;
  onConfirmSendPlan: (plan: SendPlanArtifact) => void;
  onTextComplete?: () => void;
  onApplyScanQuery?: (query: string) => void;
}) {
  if (message.role === "user") {
    return <div className="ai-message is-user">{renderAiUserMessageContent(message.content)}</div>;
  }
  return (
    <AiAssistantMessage
      message={message}
      currentMailContext={currentMailContext}
      onUseArtifact={onUseArtifact}
      onUseComposeArtifact={onUseComposeArtifact}
      onSaveComposeArtifacts={onSaveComposeArtifacts}
      onOpenMail={onOpenMail}
      onConfirmSendPlan={onConfirmSendPlan}
      onTextComplete={onTextComplete}
      onApplyScanQuery={onApplyScanQuery}
    />
  );
}

function AiSidebar({
  collapsed,
  onToggle,
  currentMailContext,
  selectedThreads,
  inboxListContext,
  onUseArtifact,
  onUseComposeArtifact,
  onSaveComposeArtifacts,
  onOpenMail,
  onConfirmSendPlan,
  onApplyScanQuery,
  composerFocusKey,
}: {
  collapsed: boolean;
  onToggle: () => void;
  currentMailContext: AiMailContextRef | null;
  selectedThreads?: Array<{
    mailbox: string;
    message_id: string;
    thread_id: string;
    subject?: string;
  }>;
  inboxListContext: AiInboxListContext;
  onUseArtifact: (
    artifact: DraftReplyArtifact,
    mode: "append" | "replace",
  ) => void;
  onUseComposeArtifact: (
    artifact: ComposeDraftArtifact,
    context: AiComposeContextRef | null,
  ) => void;
  onSaveComposeArtifacts: (artifacts: ComposeDraftArtifact[]) => Promise<ComposeDraftArtifact[]>;
  onOpenMail: (target: AskMailLink) => void;
  onConfirmSendPlan: (plan: SendPlanArtifact) => void;
  onApplyScanQuery?: (query: string) => void;
  composerFocusKey: number;
}) {
  const { state, actions } = useApp();
  const { t } = useI18n();
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
  // 仅在明确探测失败时禁用输入；checking/unknown 不打断编辑
  const llmOffline = state.llmStatus.status === "unavailable" || state.llmStatus.status === "error";
  // 侧栏 starter 为按钮级显式意图：带 routingIntent，后端跳过 Router Sampling。
  const starters: Array<{ label: string; routingIntent: AiRoutingIntent }> = [
    { label: t("ai.starter.reply"), routingIntent: "inbox" },
    { label: t("ai.starter.urgent"), routingIntent: "inbox" },
    { label: t("ai.starter.organize"), routingIntent: "organize" },
  ];
  const conversation = state.aiChatMessages;
  const draftArtifactSignature = conversation
    .filter((message) =>
      message.artifact?.type === "draft_reply" ||
      message.artifacts?.some((artifact) => artifact.type === "draft_reply"),
    )
    .map((message) => {
      const artifactBodyLength =
        message.artifact?.type === "draft_reply"
          ? message.artifact.body.length
          : 0;
      const artifactCount = message.artifacts?.length || 0;
      return `${message.id}:${artifactBodyLength}:${artifactCount}`;
    })
    .join("|");
  const previousDraftArtifactSignatureRef = useRef("");

  const scrollConversationToBottom = useCallback(
    (behavior: ScrollBehavior = "smooth") => {
      const scroller = conversationRef.current;
      if (!scroller) return;
      scroller.scrollTo({
        top: Math.max(0, scroller.scrollHeight - scroller.clientHeight),
        behavior,
      });
      pinnedToBottomRef.current = true;
      setShowNewMessagePrompt(false);
    },
    [],
  );

  const previousRunningRef = useRef(running);

  useEffect(() => {
    let frame = window.requestAnimationFrame(() => {
      if (!pinnedToBottomRef.current && !scrollAfterSubmitRef.current) return;
      const behavior = scrollAfterSubmitRef.current ? "smooth" : "auto";
      scrollAfterSubmitRef.current = false;
      scrollConversationToBottom(behavior);
    });
    const scroller = conversationRef.current;
    if (!scroller || typeof ResizeObserver === "undefined") {
      return () => window.cancelAnimationFrame(frame);
    }
    const content = scroller.querySelector<HTMLElement>(".ai-message-stack") || scroller;
    const observer = new ResizeObserver(() => {
      if (pinnedToBottomRef.current) scrollConversationToBottom("auto");
    });
    observer.observe(content);
    const stopObserving = window.setTimeout(() => observer.disconnect(), 900);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(stopObserving);
      observer.disconnect();
    };
  }, [conversation, scrollConversationToBottom]);

  useEffect(() => {
    if (
      !draftArtifactSignature ||
      draftArtifactSignature === previousDraftArtifactSignatureRef.current
    ) {
      return;
    }
    previousDraftArtifactSignatureRef.current = draftArtifactSignature;
    pinnedToBottomRef.current = true;
    setShowNewMessagePrompt(false);
    const scroller = conversationRef.current;
    if (!scroller) return;
    const forceDraftArtifactScroll = () => scrollConversationToBottom("auto");
    let frame = window.requestAnimationFrame(forceDraftArtifactScroll);
    const content = scroller.querySelector<HTMLElement>(".ai-message-stack") || scroller;
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(forceDraftArtifactScroll);
    observer?.observe(content);
    const stopObserving = window.setTimeout(() => observer?.disconnect(), 1200);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(stopObserving);
      observer?.disconnect();
    };
  }, [draftArtifactSignature, scrollConversationToBottom]);

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
      actions.showToast(t("ai.offlineRetry"));
      return;
    }
    if (!state.customScanInput.trim() || running) return;
    setHistoryOpen(false);
    setSavedPromptsOpen(false);
    setShowNewMessagePrompt(false);
    pinnedToBottomRef.current = true;
    scrollAfterSubmitRef.current = true;
    window.requestAnimationFrame(() => scrollConversationToBottom("smooth"));
    void actions.sendAiChatMessage({
      currentMailContext,
      selectedThreads: selectedThreads?.length ? selectedThreads : undefined,
      inboxListContext,
    });
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
            title={collapsed ? t("ai.expandSidebar") : t("ai.collapseSidebar")}
            onClick={onToggle}
          >
            <SparkleIcon />
          </button>
          <label>Anna Inbox</label>
        </div>
        <button className="new-chat-btn" onClick={startNewChat}>
          <span>＋</span> {t("ai.newChat")}
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
                onSaveComposeArtifacts={onSaveComposeArtifacts}
                onOpenMail={onOpenMail}
                onConfirmSendPlan={onConfirmSendPlan}
                onTextComplete={scrollConversationToBottom}
                onApplyScanQuery={onApplyScanQuery}
              />
            ))}
          </div>
        ) : (
          <div className="ai-empty-state">
            <div className="ai-orb">
              <SparkleIcon />
            </div>
            <h1>{t("ai.emptyTitle")}</h1>
            <p>{t("ai.emptyDescription")}</p>
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
            aria-label={t("ai.backToAsk")}
          >
            <ChevronLeftIcon />
          </button>
          <div>
            <strong>{t("ai.askHistory")}</strong>
            <span>{t("ai.historySubtitle")}</span>
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
                    aria-label={t("ai.deleteChat")}
                    data-tooltip={t("ai.deleteChat")}
                    onClick={() => actions.deleteAiConversation(index)}
                  >
                    <TrashIcon />
                  </button>
                  {entry.pendingRun ? (
                    <button
                      className="ask-history-refresh"
                      aria-label={t("ai.refreshTask")}
                      data-tooltip={t("ai.refreshTask")}
                      onClick={() => {
                        actions.resumeAiConversation(index);
                        setHistoryOpen(false);
                      }}
                    >
                      <RefreshIcon />
                    </button>
                  ) : null}
                </div>
              );
            })
          ) : (
            <div className="ask-history-empty">
              <HistoryIcon />
              <span>{t("ai.noHistory")}</span>
            </div>
          )}
        </div>
      </section>

      <div className="ai-composer-wrap">
        {showNewMessagePrompt ? (
          <button
            className="ai-new-message-prompt"
            onClick={() => scrollConversationToBottom("smooth")}
            aria-label={t("ai.scrollToNew")}
          >
            有新消息 <span aria-hidden="true">↓</span>
          </button>
        ) : null}
        <div
          className={`ai-composer ${running ? "is-running" : ""} ${llmOffline ? "is-offline" : ""}`}
          data-tooltip={
            llmOffline
              ? t("ai.offlineRetry")
              : undefined
          }
        >
          {savedPromptsOpen ? (
            <div className="ai-saved-prompts-panel" ref={savedPromptsPanelRef} role="listbox" aria-label={t("ai.savedPrompts")}>
              <header>
                <span>{t("ai.savedPrompts")}</span>
                <button
                  type="button"
                  className="ai-saved-prompts-settings"
                  onClick={() => {
                    setSavedPromptsOpen(false);
                    actions.openSettings(true);
                  }}
                  aria-label={t("ai.savedPromptsSettings")}
                  title={t("ai.savedPromptsSettings")}
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
                        <strong>{item.title || t("ai.untitled")}</strong>
                        <div>{item.body.slice(0, 80)}</div>
                      </button>
                      <button
                        type="button"
                        className="ai-saved-prompt-delete"
                        aria-label={t("ai.deletePrompt", { title: item.title || t("ai.untitled") })}
                        data-tooltip={t("common.delete")}
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
                <div className="ai-saved-prompts-empty">{t("ai.noSavedPrompts")}</div>
              )}
            </div>
          ) : null}
          <textarea
            ref={composerInputRef}
            value={state.customScanInput}
            placeholder={composerFocused ? t("ai.savedPromptsPlaceholder") : t("ai.inputPlaceholder")}
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
                  aria-label={t("ai.stop")}
                  title={t("ai.stop")}
                >
                  <StopIcon />
                </button>
              ) : null}
              <button
                disabled={
                  llmOffline || running || !state.customScanInput.trim()
                }
                onClick={submit}
                aria-label={t("ai.send")}
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
                key={starter.label}
                disabled={llmOffline || running}
                onClick={() => {
                  if (llmOffline || running) return;
                  setHistoryOpen(false);
                  setSavedPromptsOpen(false);
                  setShowNewMessagePrompt(false);
                  pinnedToBottomRef.current = true;
                  scrollAfterSubmitRef.current = true;
                  window.requestAnimationFrame(() => scrollConversationToBottom("smooth"));
                  // 按钮级意图：prompt 仅作展示文案，选型由 routingIntent 决定。
                  void actions.sendAiChatMessage({
                    prompt: starter.label,
                    routingIntent: starter.routingIntent,
                    currentMailContext,
                    selectedThreads: selectedThreads?.length ? selectedThreads : undefined,
                  });
                }}
              >
                {starter.label}
              </button>
            ))}
          </div>
        ) : null}
      </div>

      <div className="ai-sidebar-foot">
        <div className="ai-connectivity-status">
          <button
            type="button"
            className="ai-conn-chip"
            title={state.llmStatus.message || t("ai.checkLlm")}
            onClick={() => void actions.refreshSamplingStatus()}
          >
            <i className={state.llmStatus.status === "connected" ? "is-live" : ""} />
            {state.llmStatus.status === "checking" || (state.llmStatus.status === "unknown" && !state.llmStatus.checked) ? (
              <span>LLM · <span className="conn-checking">{t("ai.checking")}</span></span>
            ) : state.llmStatus.status === "connected" ? (
              <span>
                LLM
                {typeof state.llmStatus.elapsed_ms === "number" ? (
                  <>
                    {" · "}
                    <span
                      className={
                        state.llmStatus.elapsed_ms < 300
                          ? "conn-latency is-fast"
                          : state.llmStatus.elapsed_ms <= 800
                            ? "conn-latency is-mid"
                            : "conn-latency is-slow"
                      }
                    >
                      {state.llmStatus.elapsed_ms}ms
                    </span>
                  </>
                ) : null}
              </span>
            ) : (
              <span>
                LLM · <span className="conn-timeout">{t("ai.timeout")}</span>
              </span>
            )}
          </button>
          <button
            type="button"
            className="ai-conn-chip"
            title={state.gmailApiStatus.message || t("ai.checkGmail")}
            onClick={() => void actions.refreshGmailApiStatus()}
          >
            <i className={state.gmailApiStatus.status === "connected" ? "is-live" : ""} />
            {state.gmailApiStatus.status === "checking" || (state.gmailApiStatus.status === "unknown" && !state.gmailApiStatus.checked) ? (
              <span>Gmail · <span className="conn-checking">{t("ai.checking")}</span></span>
            ) : state.gmailApiStatus.status === "connected" ? (
              <span>
                Gmail
                {typeof state.gmailApiStatus.elapsed_ms === "number" ? (
                  <>
                    {" · "}
                    <span
                      className={
                        state.gmailApiStatus.elapsed_ms < 300
                          ? "conn-latency is-fast"
                          : state.gmailApiStatus.elapsed_ms <= 800
                            ? "conn-latency is-mid"
                            : "conn-latency is-slow"
                      }
                    >
                      {state.gmailApiStatus.elapsed_ms}ms
                    </span>
                  </>
                ) : null}
              </span>
            ) : (
              <span>
                Gmail · <span className="conn-timeout">{t("ai.timeout")}</span>
              </span>
            )}
          </button>
        </div>
        <div>
          <button
            className={historyOpen ? "is-active" : ""}
            aria-label={t("ai.askHistory")}
            data-tooltip={t("ai.askHistory")}
            title={t("ai.askHistory")}
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
  const { t } = useI18n();
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
        title={t("account.switch")}
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
        aria-label={t("account.openSettings")}
        title={t("account.settings")}
        data-tooltip={t("account.settings")}
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
          aria-label={t("common.close")}
          onClick={() => setMenuOpen(false)}
        />
      ) : null}
      <section
        className={`account-menu ${menuOpen ? "is-open" : ""}`}
        aria-hidden={!menuOpen}
      >
        <header>
          <span>{t("account.accounts")}</span>
          <small>{t("account.switchInbox")}</small>
        </header>
        <div>
          {!mailboxes.length ? (
            <p className="account-menu-empty">{t("account.empty")}</p>
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
  const { t, locale } = useI18n();
  const [filter, setFilter] = useState<FeedFilter>("important");
  const [search, setSearch] = useState("");
  const [searchFocused, setSearchFocused] = useState(false);
  const [searchSuggestionIndex, setSearchSuggestionIndex] = useState(0);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const searchCaretPositionRef = useRef<number | null>(null);
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
  useLayoutEffect(() => {
    const position = searchCaretPositionRef.current;
    const input = searchInputRef.current;
    if (position === null || !input || document.activeElement !== input) return;
    input.setSelectionRange(position, position);
    searchCaretPositionRef.current = null;
  }, [search]);
  const applySearch = useCallback(
    (value: string) => {
      const parsed = parseInboxQuery(value);
      const directStatus =
        parsed.expression?.kind === "term" && parsed.expression.field === "is"
          ? parsed.expression.value
          : "";
      setSearch(value);
      setSearchFocused(false);
      if (value.trim() && !parsed.error && (!directStatus || directStatus === "unread")) {
        void searchIndexedEmailsRef.current(value.trim()).catch(() => undefined);
      }
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
        // is:todo → Todos 视图；其余 is: 状态与侧栏视图同名
        const view =
          directStatus === "todo"
            ? "todos"
            : directStatus === "is"
              ? "inbox"
              : directStatus;
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
    (suggestion: string, executeComplete = false) => {
      const next = applyInboxQuerySuggestion(search, suggestion);
      setSearch(next);
      const parsed = parseInboxQuery(next);
      if (executeComplete && parsed.expression && !parsed.error) {
        applySearch(next);
      } else {
        // A prefix completion keeps the input focused, but closes the menu so the user can type its value.
        setSearchFocused(false);
      }
      // 选择补全后 React 会保留旧光标偏移；显式移到新增 token 的末尾。
      window.requestAnimationFrame(() =>
        searchInputRef.current?.setSelectionRange(next.length, next.length),
      );
    },
    [applySearch, search],
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
  const [replyDraftRestore, setReplyDraftRestore] = useState<{
    nonce: string;
    threadId: string;
    body: string;
    bodyHtml?: string;
    cc?: string[];
    bcc?: string[];
    replyMode?: "reply_to_sender" | "reply_all";
    mode?: "reply" | "forward";
    recipients?: string[];
    composeDraftId?: string;
    composeDraftEtag?: string;
  } | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [authChecking, setAuthChecking] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(initialSidebarWidth);
  const [sidebarResizing, setSidebarResizing] = useState(false);
  const [folderOpen, setFolderOpen] = useState(false);

  const [composeOpen, setComposeOpen] = useState(false);
  /** 草稿详情按需加载后，先挂载关闭态一帧，再切到打开态以触发抽屉入场动画。 */
  const [composeOpening, setComposeOpening] = useState(false);
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
  const [draftSyncError, setDraftSyncError] = useState(false);
  const [draftSyncing, setDraftSyncing] = useState(false);
  const [composeDraftHasMore, setComposeDraftHasMore] = useState(false);
  const [composeDraftNextOffset, setComposeDraftNextOffset] = useState(0);
  const composeDraftRefreshSequenceRef = useRef(0);
  const composeDraftRefreshInFlightRef = useRef<{
    mailbox: string;
    requestId: number;
    promise: Promise<boolean>;
  } | null>(null);
  /** 撤销窗口内先在 UI 隐藏；刷新结果也不能把待删除草稿重新带回列表。 */
  const pendingComposeDraftDeletesRef = useRef(new Set<string>());
  const composeDraftActionsRef = useRef({
    listInboxThreadDrafts: actions.listInboxThreadDrafts,
    listComposeDrafts: actions.listComposeDrafts,
    getComposeDraft: actions.getComposeDraft,
  });
  composeDraftActionsRef.current = {
    listInboxThreadDrafts: actions.listInboxThreadDrafts,
    listComposeDrafts: actions.listComposeDrafts,
    getComposeDraft: actions.getComposeDraft,
  };
  const [selectedComposeDraftIds, setSelectedComposeDraftIds] = useState<
    Set<string>
  >(new Set());
  /**
   * 列表多选键：优先 mailbox|thread_id，无 thread 时用 mailbox|message:id。
   * 与 inboxThreadProjectionKey 对齐，避免同会话多行勾选。
   */
  const [selectedListKeys, setSelectedListKeys] = useState<Set<string>>(
    new Set(),
  );
  const [selectionMoreOpen, setSelectionMoreOpen] = useState(false);
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchConfirmDrafts, setBatchConfirmDrafts] = useState<
    ComposeDraft[] | null
  >(null);
  const pendingSendScheduler = useRef<PendingSendScheduler | null>(null);
  if (!pendingSendScheduler.current) {
    pendingSendScheduler.current = new PendingSendScheduler({
      showToast: actions.showToast,
      setTimeout: window.setTimeout.bind(window),
      clearTimeout: window.clearTimeout.bind(window),
      setInterval: window.setInterval.bind(window),
      clearInterval: window.clearInterval.bind(window),
    });
  }
  const composeCloseTimer = useRef<number | null>(null);
  const composeOpenFrame = useRef<number | null>(null);
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
  const searchIndexedEmailsRef = useRef(actions.searchIndexedEmails);
  const loadInboxWorkflowStateRef = useRef(actions.loadInboxWorkflowState);
  const pageLoadInFlight = useRef(false);
  const requestedGmailCursors = useRef(new Set<string>());
  const mailFeedRef = useRef<HTMLElement | null>(null);
  const avatarRequestKey = useRef("");
  const avatarMisses = useRef(new Set<string>());
  const avatarPermissionNoticeShown = useRef(false);
  const drawerCloseTimer = useRef<number | null>(null);
  // AI 侧栏打开详情是异步的；关闭或再次打开时递增 token，丢弃过期回写，避免关后自动重开
  const aiDetailOpenTokenRef = useRef(0);
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
  const workflow = state.inboxWorkflowState;
  const compatibleWorkflow = useMemo<CompatibleInboxWorkflow>(
    () => ({ ...workflow, doneRemoved: flags.doneRemoved }),
    [flags.doneRemoved, workflow],
  );
  const workflowRef = useRef(workflow);
  workflowRef.current = workflow;
  const workflowMutationQueue = useRef(Promise.resolve());
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
    searchIndexedEmailsRef.current = actions.searchIndexedEmails;
  }, [actions.searchIndexedEmails]);
  useEffect(() => {
    loadInboxWorkflowStateRef.current = actions.loadInboxWorkflowState;
  }, [actions.loadInboxWorkflowState]);

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
    void loadInboxWorkflowStateRef.current(mailbox);
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

  // 刷新邮箱缓存可能在当前邮箱内完成，需同步读取控制器预热的头像缓存。
  useEffect(() => {
    if (!mailbox || !state.inboxUpdatedAt) return;
    void getContactAvatarCache(mailbox, contactAvatarsKey).then((cached) => {
      if (!cached) return;
      setContactAvatars(cached.avatars && typeof cached.avatars === "object" ? cached.avatars : {});
      avatarMisses.current = new Set(Array.isArray(cached.missing) ? cached.missing : []);
    });
  }, [contactAvatarsKey, mailbox, state.inboxUpdatedAt]);

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
          !workflow.todos.includes(message.id) &&
          !isDoneMessage(message, compatibleWorkflow) &&
          !workflow.snoozed.includes(message.id),
      ).length,
      other: state.inboxMessages.filter(
        (message) =>
          !isImportantMessage(message) &&
          !workflow.todos.includes(message.id) &&
          !isDoneMessage(message, compatibleWorkflow) &&
          !workflow.snoozed.includes(message.id),
      ).length,
    }),
    [state.inboxMessages, workflow],
  );
  const localCategory = isLocalMailboxView(mailboxView);

  const composeDraftOverlays = useMemo(
    () => composeDraftOverlayMessages(composeDrafts),
    [composeDrafts],
  );

  const inboxMessagesWithDrafts = useMemo(
    () =>
      mergeDraftOverlayMessages(
        mergeDraftOverlayMessages(state.inboxMessages, state.inboxDraftMessages),
        composeDraftOverlays,
      ),
    [composeDraftOverlays, state.inboxDraftMessages, state.inboxMessages],
  );
  const inboxSnapshotMessagesWithDrafts = useMemo(
    () =>
      mergeDraftOverlayMessages(
        mergeDraftOverlayMessages(
          state.inboxSnapshotMessages,
          state.inboxDraftMessages,
        ),
        composeDraftOverlays,
      ),
    [composeDraftOverlays, state.inboxDraftMessages, state.inboxSnapshotMessages],
  );
  // 与 is:unread 一致：Inbox + Todos + Snoozed（排除 Done）
  const workflowAwareInboxMessages = useMemo(
    () =>
      mergeInboxSearchSourceMessages(
        inboxMessagesWithDrafts,
        inboxSnapshotMessagesWithDrafts,
        flags,
        compatibleWorkflow,
      ).filter((message) => !isDoneMessage(message, compatibleWorkflow)),
    [
      flags,
      compatibleWorkflow,
      inboxMessagesWithDrafts,
      inboxSnapshotMessagesWithDrafts,
    ],
  );
  const sourceMessages = useMemo(() => {
    // 搜索 / 自定义 Split：用 workflow 源，避免 todos 未读与 is:unread 不一致
    const useWorkflowSource =
      mailboxView === "inbox" &&
      (filter === "search" || filter.startsWith("category:"));
    const resolved = filter === "search"
      ? state.indexedSearchQuery === activeSearch
        ? state.indexedSearchMessages
        : []
      : useWorkflowSource
        ? workflowAwareInboxMessages
      : resolveSourceMessages(
          mailboxView,
          inboxMessagesWithDrafts,
          inboxSnapshotMessagesWithDrafts,
          flags,
          compatibleWorkflow,
        );
    if (mailboxView !== "drafts") return resolved;
    const composeMessages: InboxMessage[] = composeDrafts.map((draft) => ({
      id: draft.draft_mode === "forward" ? `forward:${draft.id}` : `compose:${draft.id}`,
      mailbox,
      date: draft.updated_at || draft.created_at || "",
      from: mailbox,
      to: draft.recipients.join(", "),
      thread_id: draft.source_thread_id || undefined,
      subject: draft.subject || "(no subject)",
      snippet: composeDraftBodyPreview(draft).slice(0, 120),
      body_preview: composeDraftBodyPreview(draft).slice(0, 120),
      // 目录页不含完整正文，但仍要标识为 Draft，供分类和批量操作投影使用。
      draft_body: composeDraftBodyPreview(draft),
      draft_local: true,
      label_ids: ["DRAFT"],
    }));
    return [...composeMessages, ...resolved];
  }, [
    composeDrafts,
    filter,
    flags,
    inboxMessagesWithDrafts,
    inboxSnapshotMessagesWithDrafts,
    mailbox,
    mailboxView,
    workflowAwareInboxMessages,
    compatibleWorkflow,
    state.indexedSearchMessages,
    state.indexedSearchQuery,
    activeSearch,
  ]);
  const inboxSplitMessages = useMemo(() => {
    // Important/Other 仍排除 todos/snoozed（由置顶区展示）；自定义 Split 与 is:unread 同数据源
    const plainInbox = workflowAwareInboxMessages.filter(
      (message) =>
        !workflow.todos.includes(message.id) &&
        !workflow.snoozed.includes(message.id),
    );
    return splitInboxMessages(
      plainInbox,
      state.inboxSettings,
      workflowAwareInboxMessages,
    );
  }, [workflow, state.inboxSettings, workflowAwareInboxMessages]);
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
      const snapshot: InboxWorkflowState = {
        ...workflowRef.current,
        todos: [...workflowRef.current.todos],
        snoozed: [...workflowRef.current.snoozed],
        done: [...workflowRef.current.done],
        snoozedUntil: { ...workflowRef.current.snoozedUntil },
      };
      const ids = messages.map((item) => item.id);
      workflowMutationQueue.current = workflowMutationQueue.current.then(async () => {
        const nextWorkflow = transitionInboxWorkflow(workflowRef.current, kind, ids, enabled, snoozeUntil);
        workflowRef.current = nextWorkflow;
        const sentIds = kind === "done" ? messages.filter(isSentMessage).map((item) => item.id) : [];
        setFlags((current) => {
          const doneRemoved = enabled
            ? current.doneRemoved.filter((id) => !ids.includes(id))
            : [...current.doneRemoved.filter((id) => !ids.includes(id)), ...sentIds];
          const next = {
            ...current,
            // Only saved/doneRemoved remain in this compatibility cache.
            doneRemoved: [...new Set(doneRemoved)],
            saved: { ...current.saved, ...Object.fromEntries(messages.map((item) => [item.id, item])) },
          };
          void setMailFlags(mailbox, next);
          return next;
        });
        const saved = await actions.saveInboxWorkflowState(nextWorkflow, mailbox);
        if (saved === false) {
          const reloaded = await actions.loadInboxWorkflowState(mailbox);
          if (reloaded) workflowRef.current = reloaded;
          actions.showToast(t("toast.workflowSaveFailed"));
        }
      }).catch(() => undefined);
      return snapshot;
    },
    [actions, mailbox],
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
    (messages: InboxMessage[], previous: InboxWorkflowState) => {
      const ids = new Set(messages.map((item) => item.id));
      const next = restoreInboxWorkflow(workflowRef.current, previous, ids);
      workflowRef.current = next;
      workflowMutationQueue.current = workflowMutationQueue.current.then(async () => {
        const saved = await actions.saveInboxWorkflowState(next, mailbox);
        if (saved === false) {
          const reloaded = await actions.loadInboxWorkflowState(mailbox);
          if (reloaded) workflowRef.current = reloaded;
          actions.showToast(t("toast.workflowSaveFailed"));
        }
      }).catch(() => undefined);
    },
    [actions, mailbox],
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
    // 搜索 / 自定义 Split 与 is:unread 对齐：保留 todos/snoozed
    const includeWorkflowInInbox =
      filter === "search" || filter.startsWith("category:");
    return sourceMessages.filter((message) => {
      // 索引搜索已经在后端对全部本地缓存完成过滤；不能再按当前文件夹
      // 的 Todo/Done 投影二次排除，否则 is:done 等查询会丢失合法结果。
      const matchesView = filter === "search"
        ? true
        :
        mailboxView === "inbox"
          ? includeWorkflowInInbox
            ? !isDoneMessage(message, compatibleWorkflow)
            : !workflow.todos.includes(message.id) &&
              !isDoneMessage(message, compatibleWorkflow) &&
              !workflow.snoozed.includes(message.id)
          : mailboxView === "todos"
            ? workflow.todos.includes(message.id)
            : mailboxView === "snoozed"
              ? workflow.snoozed.includes(message.id)
              : mailboxView === "done"
                ? isDoneMessage(message, compatibleWorkflow)
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
      if (filter === "search") return true;
      if (!activeSearch.trim() || parsedActiveSearch.error) return true;
      return matchInboxQuery(message, parsedActiveSearch, undefined, {
        todoIds: workflow.todos,
      });
    });
  }, [
    activeSearch,
    filter,
    flags,
    inboxSplitMessages,
    mailboxView,
    parsedActiveSearch,
    sourceMessages,
    state.indexedSearchMessages,
  ]);

  useEffect(() => {
    if (!filter.startsWith("category:")) return;
    const id = filter.slice("category:".length);
    const categories = state.inboxSettings.custom_categories || [];
    if (!categories.some((split) => split.id === id))
      setFilter("important");
  }, [filter, state.inboxSettings.custom_categories]);

  // 首屏阈值 + Show more：所有列表视图统一按 localLimit 截断展示
  const displayedVisible = useMemo(
    () => visible.slice(0, feedWindow.localLimit),
    [feedWindow.localLimit, visible],
  );
  // 单行入场：新出现的 id 做 stagger 动画（切分类不播，见 skipEnterAnimRef）
  const [enteringIds, setEnteringIds] = useState<Set<string>>(() => new Set());
  const seenRowIdsRef = useRef<Set<string>>(new Set());
  const skipEnterAnimRef = useRef(true);
  useEffect(() => {
    const nextIds = displayedVisible.map((message) => message.id).filter(Boolean);
    if (skipEnterAnimRef.current) {
      seenRowIdsRef.current = new Set(nextIds);
      skipEnterAnimRef.current = false;
      return;
    }
    const fresh = nextIds.filter((id) => !seenRowIdsRef.current.has(id));
    for (const id of nextIds) seenRowIdsRef.current.add(id);
    if (!fresh.length) return;
    // 按单封依次挂上 is-entering，制造流式入场感
    let cancelled = false;
    const timers: number[] = [];
    fresh.forEach((id, index) => {
      timers.push(
        window.setTimeout(() => {
          if (cancelled) return;
          setEnteringIds((current) => {
            const next = new Set(current);
            next.add(id);
            return next;
          });
          timers.push(
            window.setTimeout(() => {
              if (cancelled) return;
              setEnteringIds((current) => {
                if (!current.has(id)) return current;
                const next = new Set(current);
                next.delete(id);
                return next;
              });
            }, 280),
          );
        }, index * 28),
      );
    });
    return () => {
      cancelled = true;
      for (const timer of timers) window.clearTimeout(timer);
    };
  }, [displayedVisible]);
  const pinnedImportantMessages = useMemo(() => {
    // 必须先按 message.id 合并，再按 thread 折叠：否则同线程星标旧信与最新信会各占一行
    const byId = new Map<string, InboxMessage>();
    for (const message of [
      ...Object.values(flags.saved),
      ...state.inboxSnapshotMessages,
      ...state.inboxMessages,
    ]) {
      if (message.id && !isTrashMessage(message)) byId.set(message.id, message);
    }
    return uniqueLatestInboxThreads(
      [...byId.values()].filter(
        (message) =>
          isStarredMessage(message) || workflow.todos.includes(message.id),
      ),
    );
  }, [
    flags.saved,
    workflow.todos,
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
      // 已有 All mail 快照时禁止重载（避免 append:false 冲掉快照或触发假刷新）
      if (
        state.inboxSnapshotMessages.length > 0 ||
        state.inboxMessages.length > 0
      ) {
        setFeedWindow((current) => ({
          ...current,
          days: targetDays,
          hasMore: false,
          localLimit: INBOX_FEED_PAGE_SIZE,
        }));
        return true;
      }
      // 冷启动：统一读 All mail 缓存，再由 resolveSourceMessages 做标签投影
      const result = await actions.loadCachedInboxEmails(
        "all",
        targetDays,
        0,
        false,
      );
      if (!result.ok || mailboxViewRef.current !== category) return result.ok;
      if (result.count === 0 && !result.hasMore) {
        // 缓存为空时拉 All mail 并写入统一缓存，不按 category 打 Gmail
        const gmail = await loadGmailPage("all", targetDays, "", 0, []);
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
        hasMore: result.hasMore,
        localLimit: INBOX_FEED_PAGE_SIZE,
        source: result.hasMore ? "cache" : "gmail",
        gmailPageToken: "",
        gmailPageOffset: 0,
      });
      return true;
    },
    [
      actions,
      loadGmailPage,
      state.inboxMessages.length,
      state.inboxSnapshotMessages.length,
    ],
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
        // All time 扩展同样走 All mail 缓存/Gmail，Inbox 视图由标签投影
        const result =
          currentSource === "cache"
            ? await actions.loadCachedInboxEmails(
                "all",
                INBOX_ALL_TIME_DAYS,
                currentNextOffset,
                true,
              )
            : await loadGmailPage(
                "all",
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

  const refreshStoredDrafts = useCallback(async () => {
    const inFlight = composeDraftRefreshInFlightRef.current;
    if (inFlight?.mailbox === mailbox) return inFlight.promise;

    const refreshSequence = ++composeDraftRefreshSequenceRef.current;
    setDraftSyncing(true);
    // 线程草稿只补充当前设备的未发送回复，不应阻塞 APS Compose 草稿目录的显示。
    void composeDraftActionsRef.current
      .listInboxThreadDrafts(mailbox, 100)
      .catch(() => undefined);

    let refreshPromise: Promise<boolean>;
    refreshPromise = (async () => {
      try {
        const payload = await composeDraftActionsRef.current.listComposeDrafts(mailbox);
        if (refreshSequence === composeDraftRefreshSequenceRef.current) {
          const nextDrafts = (payload.drafts || []).filter(
            (draft) => !pendingComposeDraftDeletesRef.current.has(draft.id),
          );
          const nextOffset = Number(payload.next_offset || payload.drafts?.length || 0);
          setComposeDrafts(nextDrafts);
          setDraftSyncError(false);
          setComposeDraftHasMore(Boolean(payload.has_more));
          setComposeDraftNextOffset(nextOffset);
          void setCachedComposeDraftDirectory(mailbox, {
            ...payload,
            drafts: nextDrafts,
            next_offset: nextOffset,
          });
        }
        return true;
      } catch {
        // APS 连续重试均失败时保留最后一次成功列表，并提示用户检查网络后手动重试。
        if (refreshSequence === composeDraftRefreshSequenceRef.current) {
          setDraftSyncError(true);
          setComposeDraftHasMore(false);
        }
        return false;
      } finally {
        if (composeDraftRefreshInFlightRef.current?.requestId === refreshSequence) {
          composeDraftRefreshInFlightRef.current = null;
          if (refreshSequence === composeDraftRefreshSequenceRef.current) {
            setDraftSyncing(false);
          }
        }
      }
    })();
    composeDraftRefreshInFlightRef.current = {
      mailbox,
      requestId: refreshSequence,
      promise: refreshPromise,
    };
    return refreshPromise;
  }, [mailbox]);

  const loadMoreComposeDrafts = useCallback(async () => {
    if (!composeDraftHasMore || draftSyncing || pageLoadInFlight.current) return;
    pageLoadInFlight.current = true;
    setFeedAction("more");
    try {
      const payload = await composeDraftActionsRef.current.listComposeDrafts(
        mailbox,
        100,
        composeDraftNextOffset,
      );
      setComposeDrafts((current) => {
        const byId = new Map(current.map((draft) => [draft.id, draft]));
        for (const draft of payload.drafts || []) {
          if (!pendingComposeDraftDeletesRef.current.has(draft.id)) byId.set(draft.id, draft);
        }
        const nextDrafts = [...byId.values()];
        void setCachedComposeDraftDirectory(mailbox, {
          mailbox,
          count: nextDrafts.length,
          drafts: nextDrafts,
          has_more: Boolean(payload.has_more),
          next_offset: Number(payload.next_offset || composeDraftNextOffset),
        });
        return nextDrafts;
      });
      setComposeDraftHasMore(Boolean(payload.has_more));
      setComposeDraftNextOffset(Number(payload.next_offset || composeDraftNextOffset));
      setDraftSyncError(false);
    } catch {
      // 目录续页和首次读取使用同一 APS 自动重试策略；仍失败时提示用户手动重试。
      setDraftSyncError(true);
    } finally {
      pageLoadInFlight.current = false;
      setFeedAction((current) => (current === "more" ? null : current));
    }
  }, [composeDraftHasMore, composeDraftNextOffset, draftSyncing, mailbox]);

  const syncInbox = useCallback(
    async (targetDays = feedWindow.days, clearCache = false) => {
      requestedGmailCursors.current.clear();
      setFeedAction("refresh");
      const refreshActiveSearch = () => {
        const value = activeSearch.trim();
        if (value && !parseInboxQuery(value).error) {
          void searchIndexedEmailsRef.current(value).catch(() => undefined);
        }
      };
      try {
        // 静默同步：与自动刷新同一路径，合并快照、不整表清空
        if (!clearCache) {
          if (localCategory && mailboxView === "drafts") {
            await actions.listInboxThreadDrafts(mailbox, 100);
            refreshActiveSearch();
            return true;
          }
          const synced = await actions.silentSyncInbox(targetDays);
          if (synced) refreshActiveSearch();
          return synced;
        }
        // 硬刷新：仅显式刷新、换邮箱等操作才清缓存后整表重载。
        if (localCategory) {
          if (mailboxView === "drafts") {
            await actions.listInboxThreadDrafts(mailbox, 100);
            setFeedWindow((current) => ({
              ...current,
              localLimit: INBOX_FEED_PAGE_SIZE,
            }));
            refreshActiveSearch();
            return true;
          }
          const result = await actions.refreshInboxEmails("all", 7, true);
          if (result.ok) {
            setFeedWindow((current) => ({
              ...current,
              localLimit: INBOX_FEED_PAGE_SIZE,
            }));
          }
          if (result.ok) refreshActiveSearch();
          return result.ok;
        }
        if (mailboxView === "inbox" && targetDays === INBOX_ALL_TIME_DAYS) {
          const result = await actions.refreshInboxEmails(
            "all",
            INBOX_ALL_TIME_DAYS,
            true,
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
          if (!result.hasMore) {
            refreshActiveSearch();
            return true;
          }
          const loadedAll = await loadRemainingAllTimeInbox(
            source,
            result.nextOffset,
            "",
            0,
            excludeMessageIds,
          );
          if (loadedAll) actions.showToast(t("toast.loadedAllEmails"));
          if (loadedAll) refreshActiveSearch();
          return loadedAll;
        }
        const result = await actions.refreshInboxEmails(
          "all",
          targetDays,
          true,
        );
        if (result.ok) {
          setFeedWindow({
            days: targetDays,
            nextOffset: result.nextOffset,
            hasMore: result.hasMore,
            localLimit: INBOX_FEED_PAGE_SIZE,
            source: result.hasMore ? "cache" : "gmail",
            gmailPageToken: "",
            gmailPageOffset: 0,
          });
        }
        if (result.ok) refreshActiveSearch();
        return result.ok;
      } finally {
        setFeedAction((current) => (current === "refresh" ? null : current));
      }
    },
    [
      actions,
      feedWindow.days,
      loadRemainingAllTimeInbox,
      localCategory,
      mailbox,
      mailboxView,
      activeSearch,
    ],
  );

  useEffect(() => {
    let cancelled = false;
    const bootstrapDrafts = async () => {
      // 先投影本机镜像，避免 APS 往返让草稿标签和编辑页短暂空白。
      const cached = await getCachedComposeDraftDirectory(mailbox);
      if (!cancelled && cached) {
        setComposeDrafts(
          cached.drafts.filter((draft) => !pendingComposeDraftDeletesRef.current.has(draft.id)),
        );
        setComposeDraftHasMore(Boolean(cached.has_more));
        setComposeDraftNextOffset(Number(cached.next_offset || cached.drafts.length || 0));
      }
      if (!cancelled) void refreshStoredDrafts();
    };
    void bootstrapDrafts();
    return () => {
      cancelled = true;
    };
  }, [mailbox, refreshStoredDrafts]);


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

  /** 将时间窗推进一级（7→30→60→ALL），只追加更早邮件，不清空已有列表 */
  const expandFeedRange = useCallback(async () => {
    const nextDays = nextFeedRangeDays(feedWindow.days);
    if (nextDays === null || pageLoadInFlight.current) return;
    pageLoadInFlight.current = true;
    setFeedAction("more");
    setCachedInboxRetryAction("load-more");
    try {
      const result = await actions.expandInboxFeedWindow(nextDays);
      if (!result.ok) {
        actions.showToast(
          nextDays === INBOX_ALL_TIME_DAYS
            ? "Failed to load older emails."
            : `Failed to load emails older than ${feedWindow.days} days.`,
        );
        return;
      }
      setFeedWindow((current) => ({
        ...current,
        days: nextDays,
        nextOffset: result.nextOffset,
        hasMore: result.hasMore,
        localLimit: Math.max(current.localLimit, INBOX_FEED_PAGE_SIZE),
        source: result.hasMore ? "cache" : "gmail",
        gmailPageToken: "",
        gmailPageOffset: 0,
      }));
      actions.showToast(
        nextDays === INBOX_ALL_TIME_DAYS
          ? `Loaded older emails. ${result.count} email${result.count === 1 ? "" : "s"} in this period.`
          : `Showing last ${nextDays} days. ${result.count} email${result.count === 1 ? "" : "s"} in this period.`,
      );
    } finally {
      pageLoadInFlight.current = false;
      setFeedAction((current) => (current === "more" ? null : current));
    }
  }, [actions, feedWindow.days]);

  const isInboxSyncing = state.inboxSnapshotLoading || feedAction === "refresh";
  const days = feedWindow.days;
  // 仅在「设置 display_range」或「邮箱」真正变更时重置窗口。
  // 故意不依赖 feedWindow.days：用户底部扩窗后不得被拉回设置值。
  // 启动时默认 state 为 30，存储 hydrate 到 7 时只对齐标签，禁止 clearCache 闪屏。
  const appliedDisplayRangeRef = useRef<{ mailbox: string; days: number } | null>(
    null,
  );
  const settingsHydratedRef = useRef(false);
  useEffect(() => {
    const configuredDays = state.inboxSettings.display_range_days;
    if (!mailbox) return;
    const hasStoredSettings = Boolean(state.inboxSettingsEtag);
    const applied = appliedDisplayRangeRef.current;
    if (
      applied &&
      applied.mailbox === mailbox &&
      applied.days === configuredDays
    ) {
      if (hasStoredSettings) settingsHydratedRef.current = true;
      return;
    }
    const isFirstBind = !applied;
    const mailboxChanged = Boolean(applied && applied.mailbox !== mailbox);
    const settingsChanged = Boolean(applied && applied.days !== configuredDays);
    appliedDisplayRangeRef.current = { mailbox, days: configuredDays };
    setFeedWindow((current) => {
      if (current.days === configuredDays && !mailboxChanged) return current;
      return {
        ...current,
        days: configuredDays,
        ...(mailboxChanged
          ? {
              nextOffset: DEFAULT_INBOX_FEED_WINDOW.nextOffset,
              hasMore: DEFAULT_INBOX_FEED_WINDOW.hasMore,
              localLimit: DEFAULT_INBOX_FEED_WINDOW.localLimit,
              source: DEFAULT_INBOX_FEED_WINDOW.source,
              gmailPageToken: "",
              gmailPageOffset: 0,
            }
          : {}),
      };
    });
    if (isFirstBind) {
      if (hasStoredSettings) settingsHydratedRef.current = true;
      return;
    }
    if (mailboxChanged) {
      // switchMailbox has already rendered this mailbox's cache. Keep it while
      // History sync merges fresh changes instead of deleting the cache again.
      void syncInbox(configuredDays);
      return;
    }
    if (settingsChanged) {
      // 首次从默认 settings 被存储值覆盖：preload 已按正确天数加载，只改标签
      if (!settingsHydratedRef.current && hasStoredSettings) {
        settingsHydratedRef.current = true;
        return;
      }
      // display range 仅是列表渲染窗口：只从已有 All-mail 缓存重新投影，
      // 不触发 Gmail History、priority/backfill 或清缓存重扫。
      void (async () => {
        const result = await actions.loadCachedInboxEmails("all", configuredDays, 0, false);
        if (!result.ok) return;
        setFeedWindow((current) => ({
          ...current,
          days: configuredDays,
          nextOffset: result.nextOffset,
          hasMore: result.hasMore,
          localLimit: Math.max(current.localLimit, INBOX_FEED_PAGE_SIZE),
          source: "cache",
          gmailPageToken: "",
          gmailPageOffset: 0,
        }));
      })();
    }
  }, [
    actions,
    mailbox,
    state.inboxSettings.display_range_days,
    state.inboxSettingsEtag,
    syncInbox,
  ]);
  const lastSyncedLabel = inboxLastSyncedLabel(state.inboxUpdatedAt, t, locale);
  const nextRangeDays = nextFeedRangeDays(days);
  const canExpandFeedRange =
    isExpandableMailboxView(mailboxView) &&
    filter !== "search" &&
    nextRangeDays !== null &&
    !state.inboxError &&
    !state.inboxLoading &&
    (feedAction === null || feedAction === "refresh");
  // 触底自动加载：所有分类（含 Inbox 下 Important/Other/自定义 Split、本地 Todos 等）统一 localLimit 分页
  const canShowMoreEmails =
    mailboxView !== "drafts" &&
    filter !== "search" &&
    (feedWindow.localLimit < visible.length ||
      (isExpandableMailboxView(mailboxView) && feedWindow.hasMore)) &&
    !state.inboxError &&
    !state.inboxLoading &&
    !isInboxSyncing &&
    feedAction === null;
  const showMoreEmails = useCallback(async () => {
    if (pageLoadInFlight.current) return;
    pageLoadInFlight.current = true;
    const nextLimit = feedWindow.localLimit + INBOX_FEED_PAGE_SIZE;
    setFeedWindow((current) => ({
      ...current,
      localLimit: nextLimit,
    }));
    // 快照已够展示则只抬上限；否则续读缓存并写入快照（本地分类仅抬 localLimit）
    const snapshotCount =
      state.inboxSnapshotMessages.length || state.inboxMessages.length;
    if (snapshotCount >= nextLimit || !isExpandableMailboxView(mailboxView)) {
      pageLoadInFlight.current = false;
      return;
    }
    setFeedAction("more");
    try {
      const result = await actions.loadCachedInboxEmails(
        "all",
        feedWindow.days,
        feedWindow.nextOffset,
        true,
      );
      if (result.ok) {
        setFeedWindow((current) => ({
          ...current,
          nextOffset: result.nextOffset,
          hasMore: result.hasMore,
          source: result.hasMore ? "cache" : "gmail",
        }));
        if (!result.hasMore && result.count === 0) {
          // 缓存耗尽：拉一页 Gmail All mail 并 append
          const excludeIds = [
            ...state.inboxSnapshotMessages,
            ...state.inboxMessages,
          ]
            .map((message) => message.id)
            .filter(Boolean);
          const gmail = await actions.loadGmailInboxEmailsPage(
            "all",
            feedWindow.days,
            feedWindow.gmailPageToken,
            feedWindow.gmailPageOffset,
            excludeIds,
          );
          if (gmail.ok) {
            setFeedWindow((current) => ({
              ...current,
              hasMore: gmail.hasMore,
              source: "gmail",
              gmailPageToken: gmail.pageToken,
              gmailPageOffset: gmail.pageOffset,
            }));
          }
        }
      }
    } finally {
      pageLoadInFlight.current = false;
      setFeedAction((current) => (current === "more" ? null : current));
    }
  }, [
    actions,
    feedWindow.days,
    feedWindow.gmailPageOffset,
    feedWindow.gmailPageToken,
    feedWindow.localLimit,
    feedWindow.nextOffset,
    mailboxView,
    state.inboxMessages,
    state.inboxSnapshotMessages,
  ]);
  // 列表触底或内容不足以填满视口时自动续页（不展示 Show more 按钮）
  const tryAutoLoadMoreEmails = useCallback(() => {
    const canLoadMore = mailboxView === "drafts" ? composeDraftHasMore : canShowMoreEmails;
    if (!canLoadMore || pageLoadInFlight.current) return;
    const scroller = mailFeedRef.current;
    if (!scroller || !isMailFeedNearBottom(scroller)) return;
    if (mailboxView === "drafts") {
      void loadMoreComposeDrafts();
      return;
    }
    void showMoreEmails();
  }, [canShowMoreEmails, composeDraftHasMore, loadMoreComposeDrafts, mailboxView, showMoreEmails]);
  useEffect(() => {
    tryAutoLoadMoreEmails();
  }, [
    tryAutoLoadMoreEmails,
    displayedVisible.length,
    feedWindow.localLimit,
    feedWindow.hasMore,
    feedAction,
    composeDraftHasMore,
  ]);
  const refreshDrafts = useCallback(async () => {
    if (mailboxView !== "drafts" || pageLoadInFlight.current) return;
    pageLoadInFlight.current = true;
    setFeedAction("more");
    try {
      const synced = await refreshStoredDrafts();
      actions.showToast(
        synced ? t("mail.draftsRefreshed") : t("mail.draftsApsSyncFailed"),
      );
    } finally {
      pageLoadInFlight.current = false;
      setFeedAction((current) => (current === "more" ? null : current));
    }
  }, [actions, mailboxView, refreshStoredDrafts, t]);

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
        t,
        locale,
      );
    }
    const normalImportant = displayedVisible.filter(
      (message) =>
        !isStarredMessage(message) && !workflow.todos.includes(message.id),
    );
    // pinned（星标/Todo）与列表最新条可能是同 thread 不同 message_id，合并后先按 thread 折叠
    const source =
      filter === "important"
        ? splitImportantMessages(
            uniqueLatestInboxThreads([
              ...pinnedImportantMessages,
              ...normalImportant,
            ]),
            state.inboxSettings,
            new Set(workflow.todos),
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
        groupLabel(message, state.inboxSettings.time_section_mode, t, locale);
      const current = groups[groups.length - 1];
      if (!current || current.label !== label)
        groups.push({ label, messages: [message] });
      else current.messages.push(message);
    }
    return groups;
  }, [
    displayedVisible,
    filter,
    workflow.todos,
    locale,
    mailboxView,
    pinnedImportantMessages,
    state.inboxSettings,
    t,
  ]);
  const hasGroupedMessages = grouped.some((group) => group.messages.length);

  const aiInboxListContext = useMemo<AiInboxListContext>(() => {
    const customCategory = filter.startsWith("category:")
      ? (state.inboxSettings.custom_categories || []).find(
          (category) => category.id === filter.slice("category:".length),
        )
      : undefined;
    const doneIds = new Set(workflow.done);
    for (const message of sourceMessages) {
      if (isDoneMessage(message, compatibleWorkflow)) doneIds.add(message.id);
    }
    for (const id of flags.doneRemoved) doneIds.delete(id);
    const compactIds = (ids: Iterable<string>) => Array.from(new Set(ids))
      .map(String)
      .filter(Boolean)
      .slice(0, 500);
    return {
      mailbox_view: mailboxView,
      inbox_group: filter,
      search_input: search.slice(0, 500),
      active_search: activeSearch.slice(0, 500),
      todo_message_ids: compactIds(workflow.todos),
      done_message_ids: compactIds(doneIds),
      snoozed_message_ids: compactIds(workflow.snoozed),
      ...(customCategory ? {
        custom_category: {
          id: customCategory.id,
          name: customCategory.name,
          query: customCategory.query,
          bundling_behavior: customCategory.bundling_behavior,
        },
      } : {}),
    };
  }, [activeSearch, filter, flags, mailboxView, search, sourceMessages, state.inboxSettings.custom_categories]);

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
    // 仅用户清空 selectedId 时关抽屉；列表同步短暂找不到邮件时保留详情/附件预览
    if (selectedId || !drawerMessage || !drawerOpen) return;
    setDrawerOpen(false);
    if (drawerCloseTimer.current) window.clearTimeout(drawerCloseTimer.current);
    drawerCloseTimer.current = window.setTimeout(() => {
      setDrawerMessage(null);
      drawerCloseTimer.current = null;
    }, DETAIL_DRAWER_TRANSITION_MS);
  }, [drawerMessage, drawerOpen, selectedId]);

  useEffect(
    () => () => {
      if (drawerCloseTimer.current)
        window.clearTimeout(drawerCloseTimer.current);
    },
    [],
  );

  const detailFlags = useMemo(
    () => ({
      todos: workflow.todos,
      snoozed: workflow.snoozed,
      done: [
        ...new Set([
          ...workflow.done,
          ...sourceMessages
            .filter((message) => isDoneMessage(message, compatibleWorkflow))
            .map((message) => message.id),
          ...(selectedMessage && isDoneMessage(selectedMessage, compatibleWorkflow)
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
    const threadId = resolveMessageThreadId(selectedMessage);
    return {
      kind: "gmail_thread",
      mailbox: detailMailbox,
      thread_id: threadId,
      anchor_message_id: selectedMessage.id,
      latest_message_id: latestSelectedThreadMessage?.id || selectedMessage.id,
    };
  }, [latestSelectedThreadMessage?.id, mailbox, selectedMessage]);

  const sidebarMailContext = composeAiContext || currentMailContext;

  const listSelectionKey = useCallback(
    (message: InboxMessage) => inboxThreadProjectionKey({
      ...message,
      mailbox: message.mailbox || mailbox,
    }),
    [mailbox],
  );

  // 勾选解析池：STARS/TODOS 可能来自 flags.saved / snapshot，不能只查 sourceMessages
  const batchResolveMessages = useMemo(() => {
    const byKey = new Map<string, InboxMessage>();
    // 后写覆盖：优先用列表/快照里的新状态
    for (const message of [
      ...Object.values(flags.saved),
      ...sourceMessages,
      ...state.inboxDraftMessages,
      ...state.inboxSnapshotMessages,
      ...state.inboxMessages,
      ...pinnedImportantMessages,
      ...displayedVisible,
    ]) {
      if (!message?.id) continue;
      byKey.set(listSelectionKey(message), message);
    }
    return byKey;
  }, [
    displayedVisible,
    flags.saved,
    listSelectionKey,
    pinnedImportantMessages,
    sourceMessages,
    state.inboxDraftMessages,
    state.inboxMessages,
    state.inboxSnapshotMessages,
  ]);

  // 当前视图中已勾选的行（按 thread 键）
  const selectedListMessages = useMemo(() => {
    const resolved: InboxMessage[] = [];
    for (const key of selectedListKeys) {
      const message = batchResolveMessages.get(key);
      if (message) resolved.push(message);
    }
    return resolved;
  }, [batchResolveMessages, selectedListKeys]);
  const selectedInboxMessages = selectedListMessages;

  /** 同步本地 flags.saved 上的已读/星标，避免 STARS/TODOS 时间线仍读旧副本 */
  const patchSavedMessages = useCallback(
    (
      messageIds: string[],
      patch: (message: InboxMessage) => InboxMessage,
    ) => {
      const idSet = new Set(messageIds.filter(Boolean));
      if (!idSet.size) return;
      setFlags((current) => {
        let changed = false;
        const saved = { ...current.saved };
        for (const id of idSet) {
          const prev = saved[id];
          if (!prev) continue;
          saved[id] = patch(prev);
          changed = true;
        }
        if (!changed) return current;
        const next = { ...current, saved };
        void setMailFlags(mailbox, next);
        return next;
      });
    },
    [mailbox],
  );
  const aiSelectedThreads = useMemo(
    () =>
      selectedInboxMessages
        .filter((message) => !String(message.id || "").startsWith("compose:"))
        .slice(0, 20)
        .map((message) => ({
          mailbox: message.mailbox || mailbox,
          message_id: message.id,
          thread_id: resolveMessageThreadId(message),
          subject: message.subject || "",
        })),
    [mailbox, selectedInboxMessages],
  );

  const clearListSelection = useCallback(() => {
    setSelectedListKeys(new Set());
    setSelectionMoreOpen(false);
  }, []);

  useEffect(() => {
    if (!selectionMoreOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      const wrap = document.querySelector(".inbox-selection-more-wrap");
      if (wrap && !wrap.contains(target)) setSelectionMoreOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [selectionMoreOpen]);

  const runBatchInboxAction = useCallback(
    async (
      action: "mark_read" | "mark_unread" | "star" | "unstar" | "archive" | "trash" | "mark_done",
    ) => {
      if (!selectedInboxMessages.length || batchBusy) return;
      setBatchBusy(true);
      setSelectionMoreOpen(false);
      try {
        const expandedMessages = expandInboxThreadMessages(selectedInboxMessages, messagesInThread);
        const ids = expandedMessages.map((message) => message.id);
        const toastMessage = (count: number) =>
          action === "trash" ? t("toast.movedCountTrash", { count })
            : action === "mark_done" ? t("toast.markedCountDone", { count })
              : action === "star" ? t("toast.starredCount", { count })
                : action === "unstar" ? t("toast.unstarredCount", { count })
                  : action === "mark_unread" ? t("toast.markedCountUnread", { count })
                    : t("toast.markedCountRead", { count });
        const showUndoToast = (
          result: { count: number; undo?: () => Promise<boolean> },
          previousWorkflow?: InboxWorkflowState,
        ) => {
          actions.showToast(toastMessage(result.count), {
            actionLabel: t("toast.undo"),
            durationMs: 6_000,
            onAction: () => {
              void result.undo?.().then((restored) => {
                if (restored && previousWorkflow) {
                  restoreWorkflowFlags(expandedMessages, previousWorkflow);
                }
              });
            },
          });
        };
        if (action === "mark_done") {
          // Gmail mark_read / 移出 INBOX 成功后再写本地 Done 样式
          const previous = workflow;
          const result = await actions.batchInboxActions(ids, "mark_done");
          if (!result.ok) {
            actions.showToast(t("toast.failedMarkDone"));
            return;
          }
          setWorkflowFlag("done", expandedMessages, true);
          clearListSelection();
          showUndoToast(result, previous);
          return;
        }
        const result = await actions.batchInboxActions(ids, action);
        if (!result.ok) {
          actions.showToast(
            action === "mark_read" ? "Failed to mark as read."
              : action === "mark_unread" ? "Failed to mark as unread."
                : action === "star" ? "Failed to star."
                  : action === "unstar" ? "Failed to remove star."
                    : action === "trash" ? "Failed to move to trash."
                      : "Batch action failed.",
          );
          return;
        }
        // STARS/TODOS 时间线读 flags.saved，需同步已读/星标
        if (action === "mark_read" || action === "mark_unread" || action === "star" || action === "unstar") {
          patchSavedMessages(ids, (message) => {
            const labels = new Set(
              (message.label_ids || []).map((label) => String(label).toUpperCase()),
            );
            if (action === "mark_read") labels.delete("UNREAD");
            if (action === "mark_unread") labels.add("UNREAD");
            if (action === "star") labels.add("STARRED");
            if (action === "unstar") labels.delete("STARRED");
            return {
              ...message,
              label_ids: [...labels],
              unread: labels.has("UNREAD"),
              starred: labels.has("STARRED"),
            };
          });
        }
        clearListSelection();
        showUndoToast(result);
      } finally {
        setBatchBusy(false);
      }
    },
    [
      actions,
      batchBusy,
      clearListSelection,
      flags,
      patchSavedMessages,
      restoreWorkflowFlags,
      selectedInboxMessages,
      setWorkflowFlag,
    ],
  );

  const batchSnoozeSelected = useCallback(() => {
    if (!selectedInboxMessages.length) return;
    // 多选延后：打开第一个的 snooze 选择器，确认后应用到全部
    const first = selectedInboxMessages[0];
    setSnoozeTarget({
      ...first,
      // 用特殊标记：SnoozePicker 提交时若多选则批量
      id: first.id,
    });
    setSelectionMoreOpen(false);
  }, [selectedInboxMessages]);

  const applyDraftReplyArtifact = useCallback(
    (artifact: DraftReplyArtifact, mode: "append" | "replace") => {
      if (
        !selectedMessage ||
        !currentMailContext ||
        currentMailContext.kind !== "gmail_thread" ||
        currentMailContext.mailbox.trim().toLowerCase() !==
          artifact.mailbox.trim().toLowerCase() ||
        currentMailContext.thread_id !== artifact.thread_id
      ) {
        actions.showToast(t("toast.openEmailFirst"));
        return;
      }
      if (drawerCloseTimer.current) {
        window.clearTimeout(drawerCloseTimer.current);
        drawerCloseTimer.current = null;
      }
      setDrawerMessage(selectedMessage);
      setDrawerOpen(true);
      setInsertRequest({ nonce: crypto.randomUUID(), artifact, mode });
    },
    [actions, currentMailContext, selectedMessage],
  );

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
    // 作废进行中的 AI 打开详情请求，防止 await 结束后重新 setSelectedId
    aiDetailOpenTokenRef.current += 1;
    setDrawerOpen(false);
    setSelectedId("");
    setExternalDetailMessage(null);
    setInsertRequest(null);
    setReplyDraftRestore(null);
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

  const scheduleInboxThreadReply = useCallback(
    (args: {
      mailbox: string;
      threadId: string;
      to: string;
      body: string;
      bodyHtml?: string;
      cc?: string[];
      bcc?: string[];
      replyMode?: "reply_to_sender" | "reply_all";
      message: InboxMessage;
      attachments?: OutgoingAttachmentMeta[];
    }) => {
      const scheduled = pendingSendScheduler.current?.schedule({
        countdownMessage: (seconds) =>
          t("toast.willSendIn", { seconds }),
        sendingMessage: t("toast.sending"),
        pendingMessage: t("toast.pendingSend"),
        undoLabel: t("toast.undo"),
        onUndo: () => {
          setExternalDetailMessage(args.message);
          setReplyDraftRestore({
            nonce: crypto.randomUUID(),
            threadId: args.threadId,
            body: args.body,
            bodyHtml: args.bodyHtml,
            cc: args.cc,
            bcc: args.bcc,
            replyMode: args.replyMode,
            mode: "reply",
          });
          setSelectedId(args.message.id);
        },
        onSend: async () => {
          const result = await actions.sendInboxThreadReply({
            mailbox: args.mailbox,
            threadId: args.threadId,
            to: args.to,
            body: args.body,
            bodyHtml: args.bodyHtml,
            cc: args.cc,
            bcc: args.bcc,
            replyMode: args.replyMode || "reply_to_sender",
            dryRun: false,
            attachments: args.attachments,
          });
          if (!result.ok) {
            throw new Error(result.error || "Failed to send reply");
          }
          void actions.silentSyncInbox();
          await actions.deleteInboxThreadDraft(args.mailbox, args.threadId);
          void removeCachedInboxThreadDraft(args.mailbox, args.threadId);
          actions.showToast(t("toast.emailSent"));
        },
        onError: (reason) =>
          {
            setExternalDetailMessage(args.message);
            setReplyDraftRestore({
              nonce: crypto.randomUUID(),
              threadId: args.threadId,
              body: args.body,
              bodyHtml: args.bodyHtml,
              cc: args.cc,
              bcc: args.bcc,
              replyMode: args.replyMode,
              mode: "reply",
            });
            setDrawerMessage(args.message);
            setDrawerOpen(true);
            setSelectedId(args.message.id);
            actions.showToast(t("toast.emailSendFailedKeep", { detail: reason instanceof Error ? reason.message : String(reason) }));
          },
      });
      if (!scheduled) return false;
      closeDetailDrawer();
      return true;
    },
    [actions, closeDetailDrawer],
  );

  const scheduleInboxThreadForward = useCallback(
    (args: {
      mailbox: string;
      recipients: string[];
      cc?: string[];
      bcc?: string[];
      subject: string;
      body: string;
      body_html?: string;
      message: InboxMessage;
      attachments?: OutgoingAttachmentMeta[];
    }) => {
      const draftId = crypto.randomUUID().replace(/-/g, "");
      const scheduled = pendingSendScheduler.current?.schedule({
        countdownMessage: (seconds) =>
          t("toast.willSendIn", { seconds }),
        sendingMessage: t("toast.sending"),
        pendingMessage: t("toast.pendingSend"),
        undoLabel: t("toast.undo"),
        onUndo: () => {
          setExternalDetailMessage(args.message);
          setReplyDraftRestore({
            nonce: crypto.randomUUID(),
            threadId: args.message.thread_id || args.message.id,
            body: args.body,
            cc: args.cc,
            bcc: args.bcc,
            mode: "forward",
            recipients: args.recipients,
          });
          setSelectedId(args.message.id);
        },
        onSend: async () => {
          const results = await actions.sendComposeEmails(args.mailbox, [
            {
              id: draftId,
              mailbox: args.mailbox,
              recipients: args.recipients,
              cc: args.cc || [],
              bcc: args.bcc || [],
              subject: args.subject,
              body: args.body,
              body_html: args.body_html,
              attachments: args.attachments || [],
            },
          ]);
          const result = results[0];
          if (!result?.ok) {
            throw new Error(result?.error || "Failed to forward email");
          }
          void actions.silentSyncInbox();
          actions.showToast(t("toast.emailSent"));
        },
        onError: (reason) =>
          actions.showToast(
            reason instanceof Error ? reason.message : String(reason),
          ),
      });
      if (!scheduled) return false;
      closeDetailDrawer();
      return true;
    },
    [actions, closeDetailDrawer],
  );

  const saveInboxThreadForwardDraft = useCallback(
    async (args: {
      id?: string;
      ifMatch?: string;
      mailbox: string;
      recipients: string[];
      cc?: string[];
      bcc?: string[];
      subject: string;
      body: string;
      bodyHtml?: string;
      sourceThreadId: string;
      sourceMessageId: string;
      attachments?: ComposeDraft["attachments"];
    }) => {
      const saved = await actions.saveComposeDraft(args.mailbox, {
        id: args.id,
        draft_mode: "forward",
        source_thread_id: args.sourceThreadId,
        source_message_id: args.sourceMessageId,
        recipients: args.recipients,
        cc: args.cc || [],
        bcc: args.bcc || [],
        subject: args.subject,
        body: args.body,
        body_html: args.bodyHtml,
        attachments: args.attachments || [],
      }, args.ifMatch);
      setComposeDrafts((current) => {
        const nextDrafts = [saved, ...current.filter((draft) => draft.id !== saved.id)];
        void setCachedComposeDraftDirectory(args.mailbox, {
          mailbox: args.mailbox,
          count: nextDrafts.length,
          drafts: nextDrafts,
          has_more: composeDraftHasMore,
          next_offset: composeDraftNextOffset,
        });
        return nextDrafts;
      });
      return saved;
    },
    [actions, composeDraftHasMore, composeDraftNextOffset],
  );

  const setMailDetailOpenRef = useRef(actions.setMailDetailOpen);
  useEffect(() => {
    setMailDetailOpenRef.current = actions.setMailDetailOpen;
  }, [actions.setMailDetailOpen]);
  useEffect(() => {
    const detailMessageId = drawerOpen
      ? (drawerMessage?.id || selectedId || "")
      : "";
    setMailDetailOpenRef.current(drawerOpen, detailMessageId);
    return () => setMailDetailOpenRef.current(false, "");
  }, [drawerMessage?.id, drawerOpen, selectedId]);

  const openComposeDraftDrawer = useCallback((draft: ComposeDraft) => {
    if (composeCloseTimer.current) window.clearTimeout(composeCloseTimer.current);
    if (composeOpenFrame.current !== null) {
      window.cancelAnimationFrame(composeOpenFrame.current);
    }
    setComposeClosing(false);
    setComposeResumeDraft(draft);
    setComposeOpening(true);
    // CSS transition 需要先让 .mail-detail-drawer 以关闭态完成一次绘制；若和
    // 异步 get_compose_draft 的结果在同一提交内直接设为 open，浏览器不会产生位移动画。
    composeOpenFrame.current = window.requestAnimationFrame(() => {
      composeOpenFrame.current = window.requestAnimationFrame(() => {
        composeOpenFrame.current = null;
        setComposeOpen(true);
        setComposeOpening(false);
      });
    });
  }, []);

  const openMessageDetail = useCallback(
    async (message: InboxMessage) => {
      const loadComposeDraft = async (prefix: "compose:" | "forward:") => {
        const draftId = message.id.slice(prefix.length);
        if (!composeDrafts.some((item) => item.id === draftId)) return null;
        try {
          const result = await composeDraftActionsRef.current.getComposeDraft(mailbox, draftId);
          if (!result.draft) throw new Error(t("mail.draftsApsSyncFailed"));
          let complete = result.draft;
          const bodyUrl = String(complete.body_url || "").trim();
          if (bodyUrl) {
            const controller = new AbortController();
            const timeout = window.setTimeout(() => controller.abort(), 15_000);
            try {
              const response = await fetch(bodyUrl, { cache: "no-store", signal: controller.signal });
              if (!response.ok) throw new Error(`Failed to load draft body (${response.status}).`);
              const bodyHtml = await response.text();
              complete = {
                ...complete,
                body_html: bodyHtml,
                body: composeDraftHtmlToPlain(bodyHtml),
              };
            } finally {
              window.clearTimeout(timeout);
            }
          }
          setComposeDrafts((current) => current.map((item) => item.id === complete.id ? complete : item));
          return complete;
        } catch (reason) {
          actions.showToast(reason instanceof Error ? reason.message : String(reason));
          return null;
        }
      };
      if (message.id.startsWith("forward:")) {
        const draft = await loadComposeDraft("forward:");
        if (draft) {
          const source = [
            ...inboxMessagesWithDrafts,
            ...inboxSnapshotMessagesWithDrafts,
            ...state.inboxMessages,
          ].find((item) =>
            (draft.source_message_id && item.id === draft.source_message_id)
            || (draft.source_thread_id && item.thread_id === draft.source_thread_id),
          );
          if (source) {
            setExternalDetailMessage(null);
            setSelectedId(source.id);
            setDrawerMessage(source);
            setDrawerOpen(true);
            setReplyDraftRestore({
              nonce: crypto.randomUUID(),
              threadId: source.thread_id || draft.source_thread_id || source.id,
              body: draft.body,
              bodyHtml: draft.body_html,
              cc: draft.cc,
              bcc: draft.bcc,
              mode: "forward",
              recipients: draft.recipients,
              composeDraftId: draft.id,
              composeDraftEtag: draft.etag,
            });
          } else {
            openComposeDraftDrawer(draft);
          }
        }
        return;
      }
      if (message.id.startsWith("compose:")) {
        const draft = await loadComposeDraft("compose:");
        if (draft) {
          openComposeDraftDrawer(draft);
        }
        return;
      }
      setExternalDetailMessage(null);
      setSelectedId(message.id);
      if (!(message.unread || hasMessageLabel(message, "UNREAD"))) return;
      // Gmail 标已读成功后再改 STARS/TODOS 本地 saved 样式
      const markSavedRead = () => {
        patchSavedMessages([message.id], (item) => ({
          ...item,
          unread: false,
          label_ids: (item.label_ids || []).filter(
            (label) => String(label).toUpperCase() !== "UNREAD",
          ),
        }));
      };
      const threadId = resolveMessageThreadId(message);
      const targetMailbox = message.mailbox || mailbox;
      if (message.thread_id) {
        void actions
          .updateInboxThreadState(targetMailbox, message.thread_id, "mark_read")
          .then(() => markSavedRead())
          .catch((reason) => {
            actions.showToast(
              reason instanceof Error ? reason.message : String(reason),
            );
          });
        return;
      }
      // 无 thread_id 时按 message id 标已读（与 Done 同步路径一致）
      if (threadId || message.id) {
        void actions
          .markInboxRead(message.id)
          .then(() => markSavedRead())
          .catch((reason) => {
            actions.showToast(
              reason instanceof Error ? reason.message : String(reason),
            );
          });
      }
    },
    [actions, composeDrafts, inboxMessagesWithDrafts, inboxSnapshotMessagesWithDrafts, mailbox, openComposeDraftDrawer, patchSavedMessages, state.inboxMessages, t],
  );

  const openMailDetailFromAi = useCallback(
    async (target: AskMailLink) => {
      const targetMailbox = target.mailbox.trim().toLowerCase();
      if (!targetMailbox || !target.thread_id) {
        actions.showToast(t("toast.emailReferenceIncomplete"));
        return;
      }
      const openToken = ++aiDetailOpenTokenRef.current;
      const isStaleOpen = () => openToken !== aiDetailOpenTokenRef.current;
      try {
        if (targetMailbox !== mailbox.trim().toLowerCase()) {
          await actions.switchMailbox(targetMailbox);
          if (isStaleOpen()) return;
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
          if (isStaleOpen()) return;
          setExternalDetailMessage(null);
          setSelectedId(known.id);
          return;
        }
        // THREAD_REF 仅含 thread id。先打开可渲染占位详情，再异步解析真实锚点；
        // 不让点击动作被完整线程/Gmail 刷新阻塞。
        const openingPlaceholder: InboxMessage = {
          id: target.message_id || target.thread_id,
          thread_id: target.thread_id,
          mailbox: targetMailbox,
          internal_date: "",
          from: "",
          to: "",
          subject: target.label || "Referenced email",
          label_ids: [],
          snippet: "",
        };
        if (isStaleOpen()) return;
        setExternalDetailMessage(openingPlaceholder);
        setSelectedId(openingPlaceholder.id);
        setMailboxView("inbox");
        const page = await actions.loadInboxThreadPage(
          targetMailbox,
          target.thread_id,
          {
            anchorMessageId: target.message_id || undefined,
            limit: 5,
            includeDisplayBody: true,
          },
        );
        // 用户已关闭详情或点了另一封引用时，禁止用过期结果重新打开
        if (isStaleOpen()) return;
        const anchor =
          page.messages.find((item) => item.id === target.message_id) ||
          page.messages.find((item) => item.id === page.latest_message_id) ||
          page.messages.at(-1);
        if (!anchor)
          throw new Error("The referenced email could not be loaded.");
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
        if (isStaleOpen()) return;
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
      const targetMailbox = message.mailbox || mailbox;
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
        mark_read: t("toast.markedAsRead"),
        mark_unread: t("toast.markedAsUnread"),
        star: t("toast.threadStarred"),
        unstar: t("toast.starsRemoved"),
        mark_important: t("toast.markedImportant"),
        mark_not_important: t("toast.markedNotImportant"),
        trash: t("toast.movedToTrash"),
        untrash: t("toast.removedFromTrash"),
      };
      // Gmail 成功后再改本地样式（含 STARS/TODOS 的 flags.saved）
      const applyLocalAfterGmail = () => {
        const threadMessages = messagesInThread(message);
        const ids = threadMessages.length
          ? threadMessages.map((item) => item.id)
          : [message.id];
        patchSavedMessages(ids, (item) => {
          const labels = new Set(
            (item.label_ids || []).map((label) => String(label).toUpperCase()),
          );
          if (operation === "mark_read") labels.delete("UNREAD");
          if (operation === "mark_unread") labels.add("UNREAD");
          if (operation === "star") labels.add("STARRED");
          if (operation === "unstar") labels.delete("STARRED");
          if (operation === "mark_important") labels.add("IMPORTANT");
          if (operation === "mark_not_important") labels.delete("IMPORTANT");
          if (operation === "trash") {
            labels.delete("INBOX");
            labels.add("TRASH");
          }
          if (operation === "untrash") labels.delete("TRASH");
          return {
            ...item,
            label_ids: [...labels],
            unread: labels.has("UNREAD"),
            starred: labels.has("STARRED"),
            important: labels.has("IMPORTANT"),
          };
        });
      };
      try {
        if (message.thread_id) {
          await actions.updateInboxThreadState(
            targetMailbox,
            message.thread_id,
            operation,
          );
        } else if (operation === "star" || operation === "unstar") {
          // 列表星标无 thread_id 时按 message 级 STARRED 同步 Gmail
          await actions.setInboxStarred(message.id, operation === "star");
        } else {
          return;
        }
        applyLocalAfterGmail();
        if (message.thread_id) closeDetailDrawer();
        actions.showToast(notices[operation], {
          actionLabel: t("toast.undo"),
          onAction: () => {
            void (async () => {
              try {
                if (message.thread_id) {
                  await actions.updateInboxThreadState(
                    targetMailbox,
                    message.thread_id || message.id,
                    reverse[operation],
                  );
                } else if (operation === "star" || operation === "unstar") {
                  await actions.setInboxStarred(
                    message.id,
                    operation === "unstar",
                  );
                }
                const undoOp = reverse[operation];
                const threadMessages = messagesInThread(message);
                const ids = threadMessages.length
                  ? threadMessages.map((item) => item.id)
                  : [message.id];
                patchSavedMessages(ids, (item) => {
                  const labels = new Set(
                    (item.label_ids || []).map((label) =>
                      String(label).toUpperCase(),
                    ),
                  );
                  if (undoOp === "mark_read") labels.delete("UNREAD");
                  if (undoOp === "mark_unread") labels.add("UNREAD");
                  if (undoOp === "star") labels.add("STARRED");
                  if (undoOp === "unstar") labels.delete("STARRED");
                  if (undoOp === "mark_important") labels.add("IMPORTANT");
                  if (undoOp === "mark_not_important") labels.delete("IMPORTANT");
                  if (undoOp === "trash") {
                    labels.delete("INBOX");
                    labels.add("TRASH");
                  }
                  if (undoOp === "untrash") labels.delete("TRASH");
                  return {
                    ...item,
                    label_ids: [...labels],
                    unread: labels.has("UNREAD"),
                    starred: labels.has("STARRED"),
                    important: labels.has("IMPORTANT"),
                  };
                });
              } catch (reason) {
                actions.showToast(
                  reason instanceof Error ? reason.message : String(reason),
                );
              }
            })();
          },
        });
      } catch (reason) {
        actions.showToast(
          reason instanceof Error ? reason.message : String(reason),
        );
      }
    },
    [
      actions,
      closeDetailDrawer,
      mailbox,
      messagesInThread,
      patchSavedMessages,
    ],
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
      const wasDone = isDoneMessage(message, compatibleWorkflow);
      const previous = workflow;
      const messages = messagesInThread(message);
      setWorkflowFlag("done", messages, !wasDone);
      if (!wasDone) {
        void syncDoneMessagesRead(messages);
      }
      if (closeAfter) {
        closeDetailDrawer();
      }
      actions.showToast(wasDone ? t("toast.movedToInbox") : t("toast.markedDone"), {
        actionLabel: t("toast.undo"),
        onAction: () => restoreWorkflowFlags(messages, previous),
        secondaryActionLabel: t("toast.view"),
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
      const enabled = workflow[kind].includes(message.id);
      if (kind === "todos") {
        if (enabled) return;
        const previous = workflow;
        setWorkflowFlag("todos", messages, true);
        actions.showToast(t("toast.addedTodo"), {
          actionLabel: t("toast.undo"),
          onAction: () => restoreWorkflowFlags(messages, previous),
          secondaryActionLabel: t("toast.view"),
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
      if (workflow.todos.includes(message.id)) return;
      const previous = workflow;
      const messages = messagesInThread(message);
      setWorkflowFlag("todos", messages, true);
      closeDetailDrawer();
      actions.showToast(t("toast.addedTodo"), {
        actionLabel: t("toast.undo"),
        onAction: () => restoreWorkflowFlags(messages, previous),
        secondaryActionLabel: t("toast.view"),
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
      if (mailboxView === "snoozed" || workflow.snoozed.includes(message.id)) {
        const previous = workflow;
        const messages = messagesInThread(message);
        setWorkflowFlag("snoozed", messages, false);
        actions.showToast(t("toast.snoozeRemoved"), {
          actionLabel: t("toast.undo"),
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
      const previous = workflow;
      // 多选延后：若当前有勾选且包含目标，对全部勾选生效
      const bulk = expandInboxThreadMessages(
        (
        selectedInboxMessages.length > 1 &&
        selectedInboxMessages.some((message) => message.id === target.id)
          ? selectedInboxMessages
          : [target]
        ),
        messagesInThread,
      );
      setSnoozeTarget(null);
      setWorkflowFlag("snoozed", bulk, true, isoTime);
      if (bulk.some((message) => message.id === selectedId)) {
        closeDetailDrawer();
      }
      if (bulk.length > 1) clearListSelection();
      actions.showToast(
        bulk.length > 1
          ? t("mail.snoozedCount", {
              count: bulk.length,
              time: new Date(isoTime).toLocaleString(locale, {
                month: "short",
                day: "numeric",
                year: "numeric",
                hour: "numeric",
                minute: "2-digit",
              }),
            })
          : t("mail.snoozedOne", {
              time: new Date(isoTime).toLocaleString(locale, {
                month: "short",
                day: "numeric",
                year: "numeric",
                hour: "numeric",
                minute: "2-digit",
              }),
            }),
        {
          actionLabel: t("mail.undo"),
          onAction: () => restoreWorkflowFlags(bulk, previous),
        },
      );
    },
    [
      actions,
      clearListSelection,
      closeDetailDrawer,
      flags,
      locale,
      messagesInThread,
      restoreWorkflowFlags,
      t,
      selectedId,
      selectedInboxMessages,
      setWorkflowFlag,
      snoozeTarget,
    ],
  );

  const selectMailboxView = (next: MailboxView) => {
    if (next !== mailboxView) clearListSelection();
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
    if (next === "drafts") {
      // 草稿箱只展示 APS Compose 草稿，不能沿用其他视图的搜索条件。
      setSearch("");
      setActiveSearch("");
    }
    setFilter("important");
    // 切分类：瞬间切换，不播入场动画
    skipEnterAnimRef.current = true;
    setEnteringIds(new Set());
    const snapshotCount =
      state.inboxSnapshotMessages.length || state.inboxMessages.length;
    // 切分类不改 days，沿用当前 All mail 窗口（与 display_range 一致），避免触发强制刷新
    const targetDays =
      feedWindow.days ||
      state.inboxSettings.display_range_days ||
      INBOX_LAST_MONTH_DAYS;
    setFeedWindow((current) => ({
      ...current,
      days: targetDays,
      localLimit: INBOX_FEED_PAGE_SIZE,
    }));
    if (next === "done") {
      if (!snapshotCount) {
        void loadRemoteCategory("done", targetDays);
      }
      return;
    }
    if (next === "drafts") {
      void refreshStoredDrafts();
      return;
    }
    if (isLocalMailboxView(next)) {
      return;
    }
    // 已有 All mail 快照：纯本地标签投影，立即展示
    if (snapshotCount) {
      return;
    }
    // 冷启动无快照：读缓存 / 必要时拉 All mail
    void loadRemoteCategory(next, targetDays);
  };

  const markTimelineDone = (messages: InboxMessage[]) => {
    const previous = workflow;
    setWorkflowFlag("done", messages, true);
    void syncDoneMessagesRead(messages);
    actions.showToast(t("toast.markedDone"), {
      actionLabel: t("toast.undo"),
      onAction: () => restoreWorkflowFlags(messages, previous),
      secondaryActionLabel: t("toast.view"),
      onSecondaryAction: () => {
        setMailboxView("done");
        setFolderOpen(false);
        setSelectedId("");
      },
    });
  };

  const scheduleComposeSend = useCallback(
    (draft: ComposeDraft) => {
      const scheduled = pendingSendScheduler.current?.schedule({
        countdownMessage: (seconds) =>
          t("toast.willSendIn", { seconds }),
        sendingMessage: t("toast.sending"),
        pendingMessage: t("toast.pendingSend"),
        undoLabel: t("toast.undo"),
        onUndo: () => {
          void actions.deleteComposeDraft(mailbox, draft.id).catch(() => undefined);
          if (composeCloseTimer.current)
            window.clearTimeout(composeCloseTimer.current);
          setComposeClosing(false);
          setComposeResumeDraft(draft);
          setComposeOpen(true);
        },
        onSend: async () => {
          const results = await actions.sendComposeEmails(mailbox, [draft]);
          const result = results[0];
          if (result?.ok) {
            await actions.deleteComposeDraft(mailbox, draft.id);
            setComposeDrafts((current) => {
              const nextDrafts = current.filter((item) => item.id !== draft.id);
              void setCachedComposeDraftDirectory(mailbox, {
                mailbox,
                count: nextDrafts.length,
                drafts: nextDrafts,
                has_more: composeDraftHasMore,
                next_offset: composeDraftNextOffset,
              });
              return nextDrafts;
            });
            actions.showToast(t("toast.emailSent"));
            return;
          }
          actions.showToast(
            result?.error
              ? t("toast.emailSendFailedKeep", { detail: result.error })
              : t("toast.emailSendFailedKeep", { detail: "" }),
          );
        },
        onError: (reason) =>
          actions.showToast(
            reason instanceof Error ? reason.message : String(reason),
          ),
      });
      if (!scheduled) return;
      setComposeResumeDraft(null);
      setComposeOpen(false);
      setComposeClosing(true);
    },
    [actions, composeDraftHasMore, composeDraftNextOffset, mailbox],
  );

  const closeComposeDrawer = useCallback(() => {
    if (composeOpenFrame.current !== null) {
      window.cancelAnimationFrame(composeOpenFrame.current);
      composeOpenFrame.current = null;
    }
    setComposeOpening(false);
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

  const restoreBackgroundComposeDraft = useCallback((draft: ComposeDraft) => {
    if (composeCloseTimer.current)
      window.clearTimeout(composeCloseTimer.current);
    setComposeClosing(false);
    setComposeResumeDraft(draft);
    setComposeOpen(true);
  }, []);

  const saveComposeDraftAfterClose = useCallback(
    (draft: Partial<ComposeDraft>, ifMatch?: string) => {
      const recoveryDraft: ComposeDraft = {
        id: String(draft.id || ""),
        mailbox,
        draft_mode: draft.draft_mode,
        source_thread_id: draft.source_thread_id,
        source_message_id: draft.source_message_id,
        gmail_draft_id: draft.gmail_draft_id,
        gmail_message_id: draft.gmail_message_id,
        recipients: [...(draft.recipients || [])],
        cc: [...(draft.cc || [])],
        bcc: [...(draft.bcc || [])],
        subject: String(draft.subject || ""),
        body: String(draft.body || ""),
        body_html: draft.body_html,
        attachments: draft.attachments ? [...draft.attachments] : [],
        etag: ifMatch,
      };
      const recoveryKey = crypto.randomUUID();
      const localDraft = { ...recoveryDraft, id: recoveryDraft.id || recoveryKey };
      const existingDraft = composeDrafts.find((item) => item.id === localDraft.id);
      const hasChanged = JSON.stringify({
        recipients: localDraft.recipients,
        cc: localDraft.cc,
        bcc: localDraft.bcc,
        subject: localDraft.subject,
        body: localDraft.body,
        body_html: localDraft.body_html || "",
        attachments: localDraft.attachments,
      }) !== JSON.stringify({
        recipients: existingDraft?.recipients || [],
        cc: existingDraft?.cc || [],
        bcc: existingDraft?.bcc || [],
        subject: existingDraft?.subject || "",
        body: existingDraft?.body || "",
        body_html: existingDraft?.body_html || "",
        attachments: existingDraft?.attachments || [],
      });
      if (!hasChanged) return;

      // 先更新本机镜像，关闭抽屉后草稿箱和会话中的“草稿”标记无需等待 APS。
      setComposeDrafts((current) => {
        const nextDrafts = [localDraft, ...current.filter((item) => item.id !== localDraft.id)];
        void setCachedComposeDraftDirectory(mailbox, {
          mailbox,
          count: nextDrafts.length,
          drafts: nextDrafts,
          has_more: composeDraftHasMore,
          next_offset: composeDraftNextOffset,
        });
        return nextDrafts;
      });

      // 关闭动作不等待远端。失败时只保留这个不可变快照，用户可从 Toast 手动恢复。
      void actions
        .saveComposeDraft(mailbox, draft, ifMatch)
        .then((saved) => {
          setComposeDrafts((current) => {
            const nextDrafts = [
              saved,
              ...current.filter((item) => item.id !== saved.id && item.id !== localDraft.id),
            ];
            void setCachedComposeDraftDirectory(mailbox, {
              mailbox,
              count: nextDrafts.length,
              drafts: nextDrafts,
              has_more: composeDraftHasMore,
              next_offset: composeDraftNextOffset,
            });
            return nextDrafts;
          });
        })
        .catch(() => {
          const recovery = { ...recoveryDraft, id: recoveryKey };
          actions.showToast(t("compose.backgroundSaveFailed"), {
            actionLabel: t("compose.restoreDraft"),
            onAction: () => restoreBackgroundComposeDraft(recovery),
          });
        });
    },
    [actions, composeDraftHasMore, composeDraftNextOffset, composeDrafts, mailbox, restoreBackgroundComposeDraft, t],
  );

  const applySavedComposeDraft = useCallback(
    (saved: ComposeDraft) => {
      setComposeDrafts((current) => {
        const nextDrafts = [saved, ...current.filter((item) => item.id !== saved.id)];
        void setCachedComposeDraftDirectory(mailbox, {
          mailbox,
          count: nextDrafts.length,
          drafts: nextDrafts,
          has_more: composeDraftHasMore,
          next_offset: composeDraftNextOffset,
        });
        return nextDrafts;
      });
    },
    [composeDraftHasMore, composeDraftNextOffset, mailbox],
  );

  const requestComposeDraftDelete = useCallback(
    (draft: ComposeDraft) => {
      if (!draft.id || pendingComposeDraftDeletesRef.current.has(draft.id)) return;
      pendingComposeDraftDeletesRef.current.add(draft.id);
      let restoreIndex = 0;
      setComposeDrafts((current) => {
        const index = current.findIndex((item) => item.id === draft.id);
        restoreIndex = index < 0 ? current.length : index;
        const nextDrafts = current.filter((item) => item.id !== draft.id);
        void setCachedComposeDraftDirectory(mailbox, {
          mailbox,
          count: nextDrafts.length,
          drafts: nextDrafts,
          has_more: composeDraftHasMore,
          next_offset: composeDraftNextOffset,
        });
        return nextDrafts;
      });
      setSelectedComposeDraftIds((current) => {
        const next = new Set(current);
        next.delete(`compose:${draft.id}`);
        return next;
      });
      setSelectedListKeys((current) => {
        const next = new Set(current);
        next.delete(
          inboxThreadProjectionKey({
            id: `compose:${draft.id}`,
            mailbox,
          }),
        );
        return next;
      });
      setSelectedId((current) =>
        current === `compose:${draft.id}` ? "" : current,
      );

      const restore = () => {
        pendingComposeDraftDeletesRef.current.delete(draft.id);
        setComposeDrafts((current) => {
          if (current.some((item) => item.id === draft.id)) return current;
          const index = Math.min(restoreIndex, current.length);
          const nextDrafts = [
            ...current.slice(0, index),
            draft,
            ...current.slice(index),
          ];
          void setCachedComposeDraftDirectory(mailbox, {
            mailbox,
            count: nextDrafts.length,
            drafts: nextDrafts,
            has_more: composeDraftHasMore,
            next_offset: composeDraftNextOffset,
          });
          return nextDrafts;
        });
      };

      actions.showToast(t("compose.deleted"), {
        actionLabel: t("toast.undo"),
        onAction: restore,
        onExpire: async () => {
          try {
            await actions.deleteComposeDraft(mailbox, draft.id);
            pendingComposeDraftDeletesRef.current.delete(draft.id);
          } catch {
            restore();
            actions.showToast(t("compose.deleteFailed"));
          }
        },
      });
    },
    [actions, composeDraftHasMore, composeDraftNextOffset, mailbox, t],
  );

  const scheduleComposeBatch = useCallback(
    (confirmed = false) => {
      const selected = composeDrafts.filter((draft) =>
        selectedComposeDraftIds.has(`compose:${draft.id}`),
      );
      if (!selected.length) {
        actions.showToast(t("toast.selectComposeDraft"));
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
      pendingSendScheduler.current?.schedule({
        countdownMessage: (seconds) =>
          t("toast.batchWillSendIn", { count: selected.length, seconds }),
        sendingMessage: t("toast.sendingDrafts"),
        pendingMessage: t("toast.pendingSend"),
        undoLabel: t("toast.undo"),
        onUndo: () => undefined,
        onSend: async () => {
          const results = await actions.sendComposeEmails(mailbox, selected);
          const succeeded = results
            .filter((result) => result.ok)
            .map((result) => result.id);
          await Promise.all(
            succeeded.map((id) => actions.deleteComposeDraft(mailbox, id)),
          );
          setComposeDrafts((drafts) =>
            {
              const nextDrafts = drafts.filter((draft) => !succeeded.includes(draft.id));
              void setCachedComposeDraftDirectory(mailbox, {
                mailbox,
                count: nextDrafts.length,
                drafts: nextDrafts,
                has_more: composeDraftHasMore,
                next_offset: composeDraftNextOffset,
              });
              return nextDrafts;
            },
          );
          setSelectedComposeDraftIds(new Set());
          const failures = results.filter((result) => !result.ok);
          actions.showToast(
            failures.length
              ? `${succeeded.length} sent; ${failures.length} draft${failures.length === 1 ? "" : "s"} failed and were kept.`
              : `${succeeded.length} drafts sent.`,
          );
        },
        onError: (reason) =>
          actions.showToast(
            reason instanceof Error ? reason.message : String(reason),
          ),
      });
    },
    [actions, composeDraftHasMore, composeDraftNextOffset, composeDrafts, mailbox, selectedComposeDraftIds],
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

  const saveBatchComposeArtifacts = useCallback(
    async (artifacts: ComposeDraftArtifact[]) => {
      const valid = artifacts.filter((artifact) =>
        artifact.recipients?.length && artifact.subject?.trim() && artifact.body.trim(),
      );
      if (valid.length !== artifacts.length) {
        throw new Error("请先补全每封草稿的收件人、主题和正文。");
      }
      const saved = await actions.saveComposeDraftBatch(
        valid[0]?.mailbox || mailbox,
        valid.map((artifact) => ({
          id: artifact.id || crypto.randomUUID(),
          recipients: artifact.recipients || [],
          subject: artifact.subject || "",
          body: artifact.body,
          etag: artifact.etag,
        })),
      );
      setComposeDrafts((current) => [
        ...saved,
        ...current.filter((draft) => !saved.some((item) => item.id === draft.id)),
      ]);
      actions.showToast(t("ai.saveDraftsSaved", { count: saved.length }));
      return saved.map((draft) => ({
        type: "compose_draft" as const,
        mailbox: draft.mailbox,
        id: draft.id,
        etag: draft.etag,
        body: draft.body,
        recipients: draft.recipients,
        subject: draft.subject,
        source_prompt: "",
        mode: "insert" as const,
      }));
    },
    [actions, mailbox, t],
  );

  useEffect(
    () => () => {
      pendingSendScheduler.current?.dispose();
      if (composeCloseTimer.current)
        window.clearTimeout(composeCloseTimer.current);
      if (composeOpenFrame.current !== null)
        window.cancelAnimationFrame(composeOpenFrame.current);
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
        selectedThreads={aiSelectedThreads}
        inboxListContext={aiInboxListContext}
        onUseArtifact={applyDraftReplyArtifact}
        onApplyScanQuery={applySearch}
        onUseComposeArtifact={(artifact, sourceContext) => {
          if (!sourceContext && !composeOpen) {
            if (composeCloseTimer.current) window.clearTimeout(composeCloseTimer.current);
            setComposeClosing(false);
            setComposeResumeDraft(null);
            setComposeOpen(true);
            setComposeInsertRequest({ nonce: crypto.randomUUID(), artifact });
            return;
          }
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
        onSaveComposeArtifacts={saveBatchComposeArtifacts}
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
                    ? t("mail.search")
                    : mailboxViewLabel(mailboxView, t)}
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
                aria-label={t("mail.closeFolders")}
                onClick={() => setFolderOpen(false)}
              />
            ) : null}
            <div
              className={`mailbox-picker-menu ${folderOpen ? "is-open" : ""}`}
            >
              {MAILBOX_VIEW_IDS.map((viewId) => (
                <button
                  key={viewId}
                  className={mailboxView === viewId ? "is-active" : ""}
                  onClick={() => selectMailboxView(viewId)}
                >
                  <span className={`folder-glyph is-${viewId}`}>
                    <FolderIcon view={viewId} />
                  </span>
                  <span>{mailboxViewLabel(viewId, t)}</span>
                  {mailboxView === viewId ? <CheckIcon /> : null}
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
                  const next = event.target.value;
                  // 高亮层重渲染后仍保留浏览器计算的输入位置，避免光标跳到开头。
                  searchCaretPositionRef.current = event.target.selectionStart ?? next.length;
                  setSearch(next);
                  if (!next.trim() && (activeSearch || filter === "search")) {
                     setActiveSearch("");
                     setFilter(filterBeforeSearch);
                   } else {
                     setActiveSearch("");
                   }
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
                    if (parsedSearch.expression && !parsedSearch.error) {
                      const hasIncompleteSuggestion = searchSuggestions.some((suggestion) => {
                        const next = applyInboxQuerySuggestion(search, suggestion);
                        const parsed = parseInboxQuery(next);
                        return !parsed.expression || Boolean(parsed.error);
                      });
                      if (hasIncompleteSuggestion) {
                        applySuggestion(searchSuggestions[searchSuggestionIndex]);
                      } else {
                        applySearch(search);
                      }
                    } else if (searchSuggestions.length) {
                      applySuggestion(searchSuggestions[searchSuggestionIndex]);
                    } else {
                      setSearchFocused(false);
                    }
                  }
                }}
                placeholder={t("mail.searchPlaceholder")}
              />
              {activeSearch || search ? (
                <button
                  type="button"
                  className="mail-search-clear"
                  aria-label={t("mail.action.clearSearch")}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    setSearch("");
                    setActiveSearch("");
                    setFilter(filterBeforeSearch);
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
                      onClick={() => applySuggestion(suggestion, true)}
                    >
                      <strong>{suggestion}</strong>
                      {getInboxQuerySuggestionPlaceholder(suggestion, t) ? (
                        <span>
                          {getInboxQuerySuggestionPlaceholder(suggestion, t)}
                        </span>
                      ) : null}
                      {index === searchSuggestionIndex ? (
                        <kbd>Enter</kbd>
                      ) : null}
                    </button>
                  ))
                ) : parsedSearch.error ? (
                  <p role="alert">{localizeInboxQueryError(parsedSearch.error, t)}</p>
                ) : null}
              </div>
            ) : null}
          </div>
          <button
            className={`refresh-mail-btn ${isInboxSyncing ? "is-syncing" : ""}`}
            disabled={isInboxSyncing}
            onClick={() => void syncInbox(days)}
          >
            <RefreshIcon />
            <span>{isInboxSyncing ? t("mail.syncing") : t("mail.refresh")}</span>
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
            <span>{t("mail.compose")}</span>
          </button>
        </header>

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
          <nav className="mail-tabs" aria-label={t("mail.filters")}>
            {(
              [
                ["important", t("mail.important"), inboxSplitMessages.important.length],
                ["other", t("mail.other"), inboxSplitMessages.other.length],
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
                onClick={() => {
                  // 切换 Inbox 标签/Split 时重置首屏分页，触底自动加载对该分类重新生效
                  if (key !== filter) {
                    skipEnterAnimRef.current = true;
                    setFeedWindow((current) => ({
                      ...current,
                      localLimit: INBOX_FEED_PAGE_SIZE,
                    }));
                  }
                  setFilter(key);
                }}
              >
                {label}
                <span>{formatInboxTabCount(count)}</span>
              </button>
            ))}
            <button
              className="icon-btn mail-tabs-manage"
              type="button"
              aria-label={t("mail.manageSplits")}
              data-tooltip={t("mail.manageSplits")}
              onClick={() => setSplitsOpen(true)}
            >
              <PlusIcon />
            </button>
            <div className="mail-tabs-meta">
              {lastSyncedLabel ? <span>{lastSyncedLabel}</span> : null}
              <p>{inboxRangeLabel(days, t)}</p>
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
          onScroll={() => tryAutoLoadMoreEmails()}
        >
          {(mailboxView === "inbox" || mailboxView === "drafts") &&
          selectedListKeys.size > 0 ? (
            <div className="inbox-selection-bar" role="toolbar" aria-label={t("mail.bulkActions")}>
              <button
                type="button"
                className="inbox-selection-clear"
                aria-label={t("mail.bulk.clearSelection")}
                data-tooltip={t("mail.bulk.clear")}
                disabled={batchBusy}
                onClick={clearListSelection}
              >
                <CloseSmallIcon />
              </button>
              <span className="inbox-selection-count">
                {mailboxView === "drafts"
                  ? t("mail.draftCount", { count: selectedListKeys.size })
                  : t("mail.emailCount", { count: selectedListKeys.size })}
              </span>
              <div className="inbox-selection-actions">
                {mailboxView === "drafts" ? (
                  <button
                    type="button"
                    className="inbox-selection-icon-btn is-primary"
                    aria-label={t("mail.action.sendSelected")}
                    data-tooltip={t("mail.action.sendShort")}
                    disabled={batchBusy || !selectedListMessages.length}
                    onClick={() => {
                      // 同步 compose 多选 id，复用既有批量发送
                      const composeIds = selectedListMessages
                        .filter((message) => String(message.id).startsWith("compose:"))
                        .map((message) => message.id);
                      setSelectedComposeDraftIds(new Set(composeIds));
                      scheduleComposeBatch();
                    }}
                  >
                    <SendIcon />
                  </button>
                ) : (
                  <>
                    <div className="inbox-selection-more-wrap">
                      <button
                        type="button"
                        className="inbox-selection-icon-btn"
                        aria-label={t("mail.action.moreActions")}
                        data-tooltip={t("mail.action.moreShort")}
                        aria-expanded={selectionMoreOpen}
                        disabled={batchBusy}
                        onClick={() => setSelectionMoreOpen((open) => !open)}
                      >
                        <MoreDotsIcon />
                      </button>
                      {selectionMoreOpen ? (
                        <div className="inbox-selection-menu" role="menu">
                          <button
                            type="button"
                            role="menuitem"
                            disabled={batchBusy}
                            onClick={() => void runBatchInboxAction("mark_read")}
                          >
                            <MailOpenIcon />
                            <span>{t("mail.action.markRead")}</span>
                          </button>
                          <button
                            type="button"
                            role="menuitem"
                            disabled={batchBusy}
                            onClick={() => void runBatchInboxAction("mark_unread")}
                          >
                            <AllMailIcon />
                            <span>{t("mail.action.markUnread")}</span>
                          </button>
                          <button
                            type="button"
                            role="menuitem"
                            disabled={batchBusy}
                            onClick={() => void runBatchInboxAction("unstar")}
                          >
                            <StarIcon />
                            <span>{t("mail.action.unstar")}</span>
                          </button>
                        </div>
                      ) : null}
                    </div>
                    <button
                      type="button"
                      className="inbox-selection-icon-btn"
                      aria-label={t("mail.bulk.batchAiDraft")}
                      data-tooltip={t("mail.aiDraftShort")}
                      disabled={batchBusy}
                      onClick={() => {
                        const previousInput = state.customScanInput;
                        const wasSidebarCollapsed = sidebarCollapsed;
                        setSidebarCollapsed(false);
                        actions.setInput(
                          "customScanInput",
                          t("mail.bulk.batchAiDraftPrompt"),
                        );
                        setAiComposerFocusKey((key) => key + 1);
                        actions.showToast(t("toast.aiDraftPromptReady"), {
                          actionLabel: t("toast.undo"),
                          onAction: () => {
                            actions.setInput("customScanInput", previousInput);
                            setSidebarCollapsed(wasSidebarCollapsed);
                          },
                        });
                      }}
                    >
                      <SparkleIcon />
                    </button>
                    <button
                      type="button"
                      className="inbox-selection-icon-btn"
                      aria-label="Star selected"
                      data-tooltip="Star"
                      disabled={batchBusy}
                      onClick={() => void runBatchInboxAction("star")}
                    >
                      <StarIcon />
                    </button>
                    <button
                      type="button"
                      className="inbox-selection-icon-btn"
                      aria-label="Snooze selected"
                      data-tooltip="Snooze"
                      disabled={batchBusy}
                      onClick={batchSnoozeSelected}
                    >
                      <ClockIcon />
                    </button>
                    <button
                      type="button"
                      className="inbox-selection-icon-btn"
                      aria-label="Move selected to trash"
                      data-tooltip="Trash"
                      disabled={batchBusy}
                      onClick={() => void runBatchInboxAction("trash")}
                    >
                      <TrashIcon />
                    </button>
                    <button
                      type="button"
                      className="inbox-selection-icon-btn is-primary"
                      aria-label="Mark selected done"
                      data-tooltip="Done"
                      disabled={batchBusy}
                      onClick={() => void runBatchInboxAction("mark_done")}
                    >
                      <CheckIcon />
                    </button>
                  </>
                )}
              </div>
            </div>
          ) : null}
          {state.inboxError &&
          sourceMessages.length > 0 &&
          !cachedInboxBannerDismissed ? (
            <div className="mail-sync-banner">
              <span>{t("mail.syncFailed")}</span>
              <div className="mail-sync-banner-actions">
                <button
                  className="mail-sync-banner-skip"
                  onClick={dismissCachedInboxBanner}
                >
                  {t("mail.skip")}
                </button>
                <button
                  onClick={() =>
                    void (cachedInboxRetryAction === "load-more"
                      ? expandFeedRange()
                      : syncInbox(days))
                  }
                  disabled={isInboxSyncing || feedAction !== null}
                >
                  {cachedInboxRetryAction === "load-more"
                    ? t("mail.retryOlder")
                    : t("mail.retrySync")}
                </button>
              </div>
            </div>
          ) : null}
          {filter === "search" && activeSearch && state.indexedSearchHasMore ? (
            <div className="mail-sync-banner" role="status">
              <span>Showing the first 200 matches from the local mail index. Refine your search to narrow the results.</span>
            </div>
          ) : null}
          {gmailAuthorizationRequired ? (
            <div
              className="mail-empty mail-auth-guide"
              aria-label="Gmail authorization required"
            >
              <InboxIcon />
              <h2>{t("mail.connectGmail")}</h2>
              <p>{t("mail.authorizeHint")}</p>
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
                      {t("mail.click")} <strong>{t("mail.authorize")}</strong>{" "}
                      {t("mail.andReturn")}
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
          ) : filter === "search" && activeSearch && state.indexedSearchLoading ? (
            <div className="mail-loading" aria-label="Searching the local mail index">
              {[1, 2, 3].map((item) => (
                <span key={item} />
              ))}
            </div>
          ) : filter === "search" && activeSearch && state.indexedSearchError ? (
            <div className="mail-empty">
              <SearchIcon />
              <h2>Search could not be completed</h2>
              <p>{state.indexedSearchError}</p>
              <button onClick={() => void searchIndexedEmailsRef.current(activeSearch).catch(() => undefined)}>
                {t("mail.tryAgain")}
              </button>
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
              <h2>{t("mail.loadFailed")}</h2>
              <p>{state.inboxError}</p>
              <button
                onClick={() => void syncInbox(days)}
                disabled={isInboxSyncing}
              >
                {t("mail.tryAgain")}
              </button>
            </div>
          ) : draftSyncError && mailboxView === "drafts" && !hasGroupedMessages ? (
            <div className="mail-empty is-category-empty">
              <SearchIcon />
              <h2>{t("mail.draftsApsSyncFailed")}</h2>
              <p>{t("mail.draftsApsSyncHint")}</p>
              <button
                type="button"
                onClick={() => void refreshDrafts()}
                disabled={draftSyncing || feedAction !== null}
              >
                {draftSyncing ? t("mail.refreshingDrafts") : t("mail.tryAgain")}
              </button>
            </div>
          ) : !hasGroupedMessages ? (
            filter === "search" && activeSearch ? (
              <div className="mail-empty">
                <SearchIcon />
                <h2>{t("mail.noResults")}</h2>
                <p>Searches cover the local indexed mailbox and do not fetch Gmail while you type.</p>
              </div>
            ) :
            mailboxView !== "inbox" && mailboxView !== "all" ? (
              <div className="mail-empty is-category-empty">
                <SearchIcon />
                <h2>{t("mail.noResults")}</h2>
              </div>
            ) : (
              <div className="mail-empty">
                <InboxIcon />
                <h2>{t("mail.noMessages")}</h2>
                <p>
                  {search
                    ? t("mail.tryDifferentSearch")
                    : t("mail.filterEmpty", { days })}
                </p>
              </div>
            )
          ) : (
            grouped.map((group) => (
              <div className="mail-group" key={group.label}>
                {mailboxView === "inbox" && filter !== "search" ? (
                  <div className="mail-group-label">
                    <span>{group.label === "STARS" ? t("mail.group.stars") : group.label === "TODOS" ? t("mail.group.todos") : group.label}</span>
                    <i />
                    {group.label !== "STARS" && group.label !== "TODOS" ? (
                      <button
                        title={t("mail.markTimelineDone")}
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
                    t,
                  ).email.toLowerCase();
                  return (
                    <InboxRow
                      key={listSelectionKey(message)}
                      message={message}
                      mailboxView={mailboxView}
                      mailbox={mailbox}
                      flags={flags}
                      workflow={compatibleWorkflow}
                      selected={selectedId === message.id}
                      avatarUrl={contactAvatars[avatarEmail]}
                      onPrefetch={() => void prefetchMessageBody(message)}
                      onFlag={updateFlag}
                      onThreadAction={(operation, message) =>
                        void handleGmailThreadAction(operation, message)
                      }
                      onSnooze={openSnoozePicker}
                      onSelect={() => void openMessageDetail(message)}
                      entering={enteringIds.has(message.id)}
                      selectable={
                        (mailboxView === "drafts" && isDraftMessage(message)) ||
                        mailboxView === "inbox"
                      }
                      selectedForBatch={selectedListKeys.has(
                        listSelectionKey(message),
                      )}
                      onBatchToggle={() => {
                        const key = listSelectionKey(message);
                        setSelectedListKeys((current) => {
                          const next = new Set(current);
                          if (next.has(key)) next.delete(key);
                          else if (next.size < 50) next.add(key);
                          return next;
                        });
                        // drafts 发送路径仍依赖 compose id 集合
                        if (mailboxView === "drafts") {
                          setSelectedComposeDraftIds((current) => {
                            const next = new Set(current);
                            const id = message.id;
                            if (next.has(id)) next.delete(id);
                            else next.add(id);
                            return next;
                          });
                        }
                      }}
                      onComposeDraftDelete={
                        message.id.startsWith("compose:")
                          ? () => {
                              const draft = composeDrafts.find(
                                (item) =>
                                  item.id === message.id.slice("compose:".length),
                              );
                              if (draft) requestComposeDraftDelete(draft);
                            }
                          : undefined
                      }
                    />
                  );
                })}
              </div>
            ))
          )}
          {/* 当前时间窗内更多由触底自动加载；底部仅保留扩时间窗 / Drafts */}
          {canExpandFeedRange ? (
            <button
              className="older-mail-btn"
              onClick={() => void expandFeedRange()}
              disabled={isInboxSyncing || feedAction !== null}
            >
              {isInboxSyncing
                ? t("mail.syncingDots")
                : olderRangeButtonLabel(days, nextRangeDays, t)}
            </button>
          ) : null}
          {mailboxView === "drafts" && !state.inboxLoading && (!draftSyncError || hasGroupedMessages) ? (
            <button
              className="older-mail-btn"
              type="button"
              onClick={() => void (composeDraftHasMore ? loadMoreComposeDrafts() : refreshDrafts())}
              disabled={isInboxSyncing || feedAction !== null}
            >
              {feedAction === "more"
                ? t("mail.refreshingDrafts")
                : composeDraftHasMore
                  ? t("mail.loadMore")
                  : t("mail.refreshDrafts")}
            </button>
          ) : null}
          {mailboxView === "trash" ? (
            <footer className="trash-retention-notice">
              <p>{t("mail.trashRetention")}</p>
              <p>
                {t("mail.emptyTrashPrefix")}{" "}
                <button
                  type="button"
                  onClick={() => {
                    void copyTextToClipboard(gmailTrashUrl(mailbox))
                      .then(() =>
                        actions.showToast(t("mail.gmailLinkCopied")),
                      )
                      .catch(() =>
                        actions.showToast(t("mail.gmailLinkCopyFailed")),
                      );
                  }}
                >
                  {t("mail.copyGmailLink")}
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
          onRequestClose={closeDetailDrawer}
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
          onScheduleReply={scheduleInboxThreadReply}
          onScheduleForward={scheduleInboxThreadForward}
          onSaveForwardDraft={saveInboxThreadForwardDraft}
          replyDraftRestore={replyDraftRestore}
          onConsumeReplyDraftRestore={(nonce) =>
            setReplyDraftRestore((current) =>
              current?.nonce === nonce ? null : current,
            )
          }
          contactAvatars={contactAvatars}
          loadContactAvatars={actions.loadContactAvatars}
          searchComposeContacts={actions.searchComposeContacts}
          latestThreadMessageId={latestSelectedThreadMessage?.id || ""}
          autoOpenDraftComposer={mailboxView === "drafts"}
        />
        {composeOpen || composeOpening || composeClosing ? (
          <ComposeView
            mailbox={mailbox}
            initialDraft={composeResumeDraft}
            open={composeOpen}
            onClose={closeComposeDrawer}
            onCloseWithSave={saveComposeDraftAfterClose}
            onDiscardDraft={requestComposeDraftDelete}
            onSavedDraft={applySavedComposeDraft}
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
