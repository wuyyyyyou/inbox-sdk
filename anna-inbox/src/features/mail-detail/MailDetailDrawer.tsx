import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type {
  AiMailContextRef,
  AttachmentDownloadPayload,
  DraftReplyArtifact,
  InboxMessage,
  InboxMessageDisplayBodyPayload,
  InboxThreadAssistPayload,
  InboxThreadMessage,
  InboxThreadPagePayload,
  InboxThreadStateOperation,
  MailAttachmentMeta,
  SubmitMailPromptRequest,
} from "../../types/mail";
import { SafeEmailHtml, SafeEmailText } from "../../shared/SafeEmailHtml";
import {
  getCachedMessageBody,
  getCachedThreadPage,
  setCachedMessageBody,
  setCachedThreadPage,
} from "../../shared/browserStorage";
import { mailAvatarFallback } from "../../shared/mailIdentity";
import { PdfAttachmentPreview } from "./PdfAttachmentPreview";
import {
  buildQuickReplyPrompt,
  deriveReplyToAddress,
  estimateAttachmentPreviewMemory,
  formatAbsoluteDateTime,
  formatAttachmentSize,
  hasNewerThreadMessage,
  isOutboundMessageForMailbox,
  isPreviewableAttachment,
  materializeAttachmentAccess,
  matchesDraftArtifact,
  mergeDraftArtifactBody,
  normalizeAttachmentKind,
  resolveAttachmentAccess,
  resolveMessageThreadId,
  senderParts,
  splitAddresses,
  triggerAttachmentDownload,
  type ResolvedAttachmentAccess,
} from "./mailDetailHelpers";

type MailUiFlags = {
  todos: string[];
  snoozed: string[];
  done: string[];
};

type InsertRequest = {
  nonce: string;
  artifact: DraftReplyArtifact;
  mode: "append" | "replace";
} | null;

type PreviewAttachmentRef = {
  attachment: MailAttachmentMeta;
  messageId: string;
};

type PreviewTransition = "idle" | "exit-previous" | "exit-next" | "enter-previous" | "enter-next";

const PREVIEW_EXIT_MS = 170;
const PREVIEW_ENTER_MS = 240;
const COMPOSER_TRANSITION_MS = 300;

function waitForPreviewAnimation(duration: number) {
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  if (reduceMotion) return Promise.resolve();
  return new Promise<void>((resolve) => window.setTimeout(resolve, duration));
}

type PreviewCacheEntry = {
  key: string;
  item: PreviewAttachmentRef;
  status: "loading" | "ready" | "error";
  access: ResolvedAttachmentAccess | null;
  text: string;
  textTruncated: boolean;
  error: string;
  shouldRender: boolean;
  promise?: Promise<PreviewCacheEntry>;
};

const MAX_BACKGROUND_RENDERED_PREVIEWS = 3;
const MAX_BACKGROUND_PREVIEW_BYTES = 80 * 1024 * 1024;

function previewCacheKey(item: PreviewAttachmentRef) {
  return `${item.messageId}:${item.attachment.id}`;
}

function DownloadIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 3v11m0 0 4-4m-4 4-4-4" />
      <path d="M5 13v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="m6 6 12 12M18 6 6 18" />
    </svg>
  );
}

function DownloadButton({ onClick, disabled = false }: { onClick: () => void; disabled?: boolean }) {
  return (
    <button
      className="attachment-download-button"
      type="button"
      aria-label="Download"
      data-tooltip={disabled ? "Preparing download" : "Download"}
      disabled={disabled}
      onClick={onClick}
    >
      <DownloadIcon />
    </button>
  );
}

function PreviewToolbarButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      className="attachment-preview-toolbar-button"
      type="button"
      aria-label={label}
      data-tooltip={label}
      onClick={onClick}
    >
      {children}
    </button>
  );
}

function PreviewArrow({ direction, onClick, disabled }: { direction: "previous" | "next"; onClick: () => void; disabled: boolean }) {
  const previous = direction === "previous";
  const label = previous ? "Previous attachment" : "Next attachment";
  return (
    <button
      className={`attachment-preview-arrow is-${direction}`}
      type="button"
      aria-label={label}
      data-tooltip={label}
      disabled={disabled}
      onClick={onClick}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d={previous ? "m15 18-6-6 6-6" : "m9 18 6-6-6-6"} /></svg>
    </button>
  );
}

function ThreadMessageAvatar({
  identity,
  label,
  avatarUrl,
}: {
  identity: string;
  label?: string;
  avatarUrl?: string;
}) {
  const [failed, setFailed] = useState(false);
  const avatar = mailAvatarFallback(identity, label);
  return avatarUrl && !failed
    ? <img className="sender-avatar is-photo" src={avatarUrl} alt="" referrerPolicy="no-referrer" onError={() => setFailed(true)} />
    : <span className={`sender-avatar tone-${avatar.tone}`} aria-hidden="true">{avatar.initial}</span>;
}

function MailDetailLoadingSkeleton() {
  return (
    <div className="mail-detail-loading-skeleton" role="status" aria-label="Loading thread">
      <article className="mail-detail-loading-card">
        <div className="mail-detail-loading-head">
          <span className="mail-detail-loading-avatar" />
          <span className="mail-detail-loading-line is-author" />
          <span className="mail-detail-loading-line is-date" />
        </div>
        <div className="mail-detail-loading-body">
          <span className="mail-detail-loading-line" />
          <span className="mail-detail-loading-line" />
          <span className="mail-detail-loading-line is-short" />
        </div>
      </article>
    </div>
  );
}

function MailThreadBodyLoading() {
  return (
    <div className="mail-thread-message-loading" role="status" aria-label="Loading full message">
      <div className="mail-detail-loading-body">
        <span className="mail-detail-loading-line" />
        <span className="mail-detail-loading-line" />
        <span className="mail-detail-loading-line is-short" />
      </div>
      <p>Loading full message…</p>
    </div>
  );
}

function AttachmentSection({
  attachments,
  expectedAttachmentCount = 0,
  onRefresh,
  onPreview,
  onDownload,
  isDownloading,
}: {
  attachments: MailAttachmentMeta[];
  expectedAttachmentCount?: number;
  onRefresh?: () => void;
  onPreview: (attachment: MailAttachmentMeta) => void;
  onDownload: (attachment: MailAttachmentMeta) => void;
  isDownloading: (attachment: MailAttachmentMeta) => boolean;
}) {
  if (!attachments.length) {
    if (!expectedAttachmentCount) return null;
    return (
      <div className="mail-detail-attachments">
        <article className="mail-detail-attachment is-pending">
          <div>
            <strong>{expectedAttachmentCount > 1 ? `${expectedAttachmentCount} attachments` : "Attachment"}</strong>
            <span>Attachment metadata is loading.</span>
          </div>
          {onRefresh ? (
            <div className="mail-detail-attachment-actions">
              <button type="button" onClick={onRefresh}>Refresh</button>
            </div>
          ) : null}
        </article>
      </div>
    );
  }
  return (
    <div className="mail-detail-attachments">
      {attachments.map((attachment) => {
        const previewable = isPreviewableAttachment(attachment);
        return (
          <article key={attachment.id} className={`mail-detail-attachment${previewable ? " is-previewable" : ""}`}>
            {previewable ? (
              <button
                className="attachment-preview-hitarea"
                type="button"
                aria-label={`Preview ${attachment.filename}`}
                onClick={() => onPreview(attachment)}
              />
            ) : null}
            <div>
              <strong>{attachment.filename}</strong>
              <span>
                {[attachment.mime_type !== "application/octet-stream" ? attachment.mime_type : "", attachment.size ? formatAttachmentSize(attachment.size) : ""]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            </div>
            <div className="mail-detail-attachment-actions">
              <DownloadButton disabled={isDownloading(attachment)} onClick={() => onDownload(attachment)} />
            </div>
          </article>
        );
      })}
    </div>
  );
}

function firstAddress(value: string | undefined) {
  const candidates = splitAddresses(value).map(senderParts);
  return candidates[0] || null;
}

function displayAddress(parts: { name: string; email: string } | null) {
  if (!parts) return { title: "Unknown", subtitle: "" };
  return {
    title: parts.name && parts.name !== parts.email ? parts.name : parts.email || "Unknown",
    subtitle: parts.name && parts.email && parts.name !== parts.email ? parts.email : "",
  };
}

function normalizeComparableSubject(value: unknown) {
  return String(value || "")
    .trim()
    .replace(/^(?:(?:re|fw|fwd):\s*)+/i, "")
    .replace(/\s+/g, " ")
    .toLowerCase();
}

function isLocalDraftMessage(message: Pick<InboxMessage, "draft_body" | "draft_local"> | null | undefined) {
  return Boolean(message?.draft_local || message?.draft_body);
}

function HeaderContactRow({
  label,
  address,
  avatarUrl,
}: {
  label: string;
  address: { name: string; email: string } | null;
  avatarUrl?: string;
}) {
  const display = displayAddress(address);
  const sender = address?.name || address?.email || "Unknown";
  return (
    <div className="mail-detail-contact-row">
      <span className="mail-detail-contact-label">{label}:</span>
      <span className="mail-detail-contact-avatar">
        <ThreadMessageAvatar identity={address?.name || address?.email || sender} label={address?.name || sender} avatarUrl={avatarUrl} />
      </span>
      <span className="mail-detail-contact-text">
        <strong>{display.title}</strong>
        {display.subtitle ? <span>{display.subtitle}</span> : null}
      </span>
    </div>
  );
}

function HeaderRecipientRows({
  label,
  addresses,
  avatarUrls,
}: {
  label: string;
  addresses: Array<{ name: string; email: string }>;
  avatarUrls: Record<string, string | undefined>;
}) {
  if (!addresses.length) return null;
  return (
    <div className="mail-detail-contact-row mail-detail-contact-row--recipients">
      <span className="mail-detail-contact-label">{label}:</span>
      <div className="mail-detail-contact-recipient-list">
        {addresses.map((address, index) => {
          const display = displayAddress(address);
          const sender = address.name || address.email || "Unknown";
          return (
            <div className="mail-detail-contact-recipient" key={`${address.email}-${index}`}>
              <span className="mail-detail-contact-avatar">
                <ThreadMessageAvatar identity={address.name || address.email || sender} label={address.name || sender} avatarUrl={avatarUrls[address.email.toLowerCase()]} />
              </span>
              <span className="mail-detail-contact-text">
                <strong>{display.title}</strong>
                {display.subtitle ? <span>{display.subtitle}</span> : null}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function buildMailContext(mailbox: string, message: InboxMessage, page: InboxThreadPagePayload | null): AiMailContextRef | null {
  if (!mailbox || !message.thread_id) return null;
  return {
    kind: "gmail_thread",
    mailbox,
    thread_id: message.thread_id,
    anchor_message_id: message.id,
    latest_message_id: page?.latest_message_id || message.id,
  };
}

function inboxMessageAttachments(message: InboxMessage): MailAttachmentMeta[] {
  const attachments = (message as InboxMessage & { attachments?: MailAttachmentMeta[] }).attachments;
  return Array.isArray(attachments) ? attachments : [];
}

function hasInboxMessageAttachment(message: InboxMessage | null | undefined) {
  if (!message) return false;
  return Boolean(
    message.has_attachment
    || Number(message.attachment_count || 0) > 0
    || inboxMessageAttachments(message).length > 0
  );
}

function singleMessagePageFromBody(
  mailbox: string,
  anchorMessage: InboxMessage,
  body: { body_text?: string; body_html?: string; body_truncated?: boolean; attachments?: MailAttachmentMeta[] },
): InboxThreadPagePayload {
  const messageId = anchorMessage.id;
  const threadId = anchorMessage.thread_id || messageId;
  const attachments = body.attachments?.length ? body.attachments : inboxMessageAttachments(anchorMessage);
  return {
    mailbox,
    thread_id: threadId,
    subject: anchorMessage.subject || "(no subject)",
    latest_subject: anchorMessage.subject || "(no subject)",
    latest_message_id: messageId,
    returned_count: 1,
    has_earlier: false,
    next_before_index: null,
    messages: [{
      id: messageId,
      thread_id: threadId,
      internal_date: anchorMessage.internal_date || "",
      from: anchorMessage.from || "",
      to: anchorMessage.to || "",
      subject: anchorMessage.subject || "(no subject)",
      label_ids: anchorMessage.label_ids || [],
      body_html: body.body_html || "",
      body_text: body.body_text || "",
      body_truncated: Boolean(body.body_truncated),
      attachments,
    }],
  };
}

async function resolveDisplayBodyPayload(payload: InboxMessageDisplayBodyPayload) {
  const bodyUrl = String(payload.body_url || "").trim();
  if (!bodyUrl) return payload;
  // 超大正文不经过 JSON-RPC，而是从 Executa 的短期 loopback URL 读取；
  // 即使读取成功也保留 body_url 标记，调用方据此避免写入 browserStorage。
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await fetch(bodyUrl, { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error(`Failed to load message body (${response.status}).`);
    return { ...payload, body_html: await response.text(), body_url: bodyUrl };
  } finally {
    window.clearTimeout(timeout);
  }
}

function expectedAttachmentCountForMessage(item: InboxThreadMessage, anchorMessage: InboxMessage | null) {
  if (!anchorMessage || item.id !== anchorMessage.id || item.attachments.length) return 0;
  if (!hasInboxMessageAttachment(anchorMessage)) return 0;
  return Math.max(1, Number(anchorMessage.attachment_count || 0));
}

function pageMayBeMissingAnchorAttachments(page: InboxThreadPagePayload, anchorMessage: InboxMessage) {
  if (!hasInboxMessageAttachment(anchorMessage)) return false;
  const anchor = page.messages.find((item) => item.id === anchorMessage.id);
  return Boolean(anchor && !anchor.attachments.length);
}

function pageMayBeMissingThreadContext(page: InboxThreadPagePayload, anchorMessage: InboxMessage) {
  if (!anchorMessage.thread_id) return false;
  if (page.has_earlier) return false;
  return page.messages.length <= 1 && page.messages.some((item) => item.id === anchorMessage.id);
}

function isGmailDraftThreadMessage(message: InboxThreadMessage) {
  return (message.label_ids || []).some((label) => label.toUpperCase() === "DRAFT");
}

function withoutGmailDraftThreadMessages(page: InboxThreadPagePayload): InboxThreadPagePayload {
  const messages = (page.messages || []).filter((item) => !isGmailDraftThreadMessage(item));
  if (messages.length === (page.messages || []).length) return page;
  const latest = messages[messages.length - 1];
  return {
    ...page,
    messages,
    returned_count: messages.length,
    latest_message_id: latest?.id || "",
    latest_subject: latest?.subject || page.latest_subject || page.subject,
  };
}

function threadMessageToInboxMessage(message: InboxThreadMessage, mailbox: string): InboxMessage {
  return {
    id: message.id,
    thread_id: message.thread_id,
    mailbox,
    internal_date: message.internal_date,
    from: message.from,
    to: message.to,
    subject: message.subject,
    label_ids: message.label_ids,
    has_attachment: Boolean(message.attachments?.length),
    attachment_count: message.attachments?.length || 0,
  };
}

async function cacheThreadPage(mailbox: string, page: InboxThreadPagePayload) {
  const visiblePage = withoutGmailDraftThreadMessages(page);
  await setCachedThreadPage(mailbox, visiblePage);
  await Promise.all((visiblePage.messages || []).map((item) =>
    setCachedMessageBody(mailbox, threadMessageToInboxMessage(item, mailbox), {
      body_text: item.body_text || "",
      body_html: item.body_html || "",
      body_truncated: Boolean(item.body_truncated),
      attachments: item.attachments || [],
    }),
  ));
}

async function hydrateCachedThreadPageBodies(mailbox: string, page: InboxThreadPagePayload): Promise<InboxThreadPagePayload> {
  const visiblePage = withoutGmailDraftThreadMessages(page);
  const messages = await Promise.all((visiblePage.messages || []).map(async (item) => {
    const cached = await getCachedMessageBody(mailbox, item.id, item.internal_date);
    if (!cached || (!cached.body_text && !cached.body_html)) return item;
    if (!cached.body_html && (item.body_html || item.body_text)) return item;
    if (
      cached.body_text === item.body_text
      && (cached.body_html || "") === (item.body_html || "")
      && Boolean(cached.body_truncated) === Boolean(item.body_truncated)
    ) return item;
    return {
      ...item,
      body_text: cached.body_text || "",
      body_html: cached.body_html || "",
      body_truncated: Boolean(cached.body_truncated),
      attachments: item.attachments,
    };
  }));
  return messages.every((item, index) => item === visiblePage.messages[index])
    ? visiblePage
    : { ...visiblePage, messages };
}

function scrollToLatestMessage(
  container: HTMLDivElement | null,
  latestMessageId: string,
  behavior: ScrollBehavior = "auto",
) {
  if (!container || !latestMessageId) return;
  const target = Array.from(container.querySelectorAll<HTMLElement>("[data-message-id]"))
    .find((element) => element.dataset.messageId === latestMessageId);
  if (target) {
    target.scrollIntoView({ block: "start", behavior });
  }
}

function scrollTargetForPage(page: InboxThreadPagePayload, fallbackMessageId: string) {
  return page.messages.some((item) => item.id === page.latest_message_id)
    ? page.latest_message_id
    : fallbackMessageId;
}

const TEXT_PREVIEW_MAX_BYTES = 256 * 1024;

function AiSparkleIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d="M12 3c.6 4.5 2.8 6.7 7.2 7.2-4.4.5-6.6 2.7-7.2 7.2-.6-4.5-2.8-6.7-7.2-7.2C9.2 9.7 11.4 7.5 12 3Z" />
      <path d="M19 3v4M17 5h4" />
    </svg>
  );
}

function MailOverviewAction({ text, onClick }: { text: string; onClick: () => void }) {
  const [visibleText, setVisibleText] = useState("");

  useEffect(() => {
    setVisibleText("");
    let frame = 0;
    const timer = window.setInterval(() => {
      frame += Math.max(1, Math.ceil(text.length / 36));
      if (frame >= text.length) {
        window.clearInterval(timer);
        setVisibleText(text);
        return;
      }
      setVisibleText(text.slice(0, frame));
    }, 24);
    return () => window.clearInterval(timer);
  }, [text]);

  return (
    <button
      type="button"
      className="mail-detail-overview-result"
      data-tooltip="Expand and discuss summary"
      aria-label="Expand and discuss summary"
      onClick={onClick}
    >
      <AiSparkleIcon />
      <span>{visibleText}</span>
    </button>
  );
}

function ToolbarIcon({ children }: { children: ReactNode }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>;
}

const CloseThreadIcon = () => <ToolbarIcon><path d="m8 6 6 6-6 6M14 6l6 6-6 6" /></ToolbarIcon>;
const MarkUnreadIcon = () => <ToolbarIcon><rect x="3" y="5" width="18" height="14" rx="4" /><path d="m4 8 8 6 5-3.8" /><circle cx="18.5" cy="6" r="2.2" fill="currentColor" stroke="white" /></ToolbarIcon>;
const StarIcon = () => <ToolbarIcon><path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3Z" /></ToolbarIcon>;
const ImportantIcon = () => <ToolbarIcon><path d="M5 6h10l4 6-4 6H5l4-6-4-6Z" /></ToolbarIcon>;
const TodoIcon = () => <ToolbarIcon><rect x="4" y="4" width="16" height="16" rx="4" /><path d="m8 12 2.5 2.5L16 9" /></ToolbarIcon>;
const ClockIcon = () => <ToolbarIcon><circle cx="12" cy="12" r="8" /><path d="M12 7v5l3 2" /></ToolbarIcon>;
const TrashIcon = () => <ToolbarIcon><path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13" /></ToolbarIcon>;
const TrashOffIcon = () => <ToolbarIcon><path d="M9 7V4h6v3M7.5 7H19M7 10l1 10h8l.6-6M4 4l16 16" /></ToolbarIcon>;
const DoneIcon = () => <ToolbarIcon><path d="m4 12 5 5L20 6" /></ToolbarIcon>;
const ExpandInlineIcon = () => <ToolbarIcon><path d="M9 4H4v5" /><path d="M4 4l6 6" /><path d="M15 20h5v-5" /><path d="m20 20-6-6" /></ToolbarIcon>;
const CollapseInlineIcon = () => <ToolbarIcon><path d="M10 4v6H4" /><path d="m4 10 6-6" /><path d="M14 20v-6h6" /><path d="m20 14-6 6" /></ToolbarIcon>;
const AiDraftIcon = () => <ToolbarIcon><path d="M14.5 4.5 19.5 9.5" /><path d="M5 15.5 15.5 5a2.1 2.1 0 0 1 3 3L8 18.5 4 20l1-4.5Z" /><path d="M19 16v4" /><path d="M17 18h4" /></ToolbarIcon>;

export function MailDetailDrawer({
  open,
  mailbox,
  message,
  flags,
  aiBusy,
  insertRequest,
  onConsumeInsertRequest,
  onClose,
  onTodoMessage,
  onDoneMessage,
  onSnoozeMessage,
  onThreadAction,
  showToast,
  loadInboxEmailBody,
  loadInboxThreadPage,
  loadInboxMessageDisplayBody,
  loadInboxThreadAssist,
  getInboxThreadDraft,
  saveInboxThreadDraft,
  deleteInboxThreadDraft,
  prepareInboxAttachmentAccess,
  submitMailContextPrompt,
  sendInboxThreadReply,
  contactAvatars,
  loadContactAvatars,
  latestThreadMessageId = "",
  latestThreadInternalDate = "",
}: {
  open: boolean;
  mailbox: string;
  message: InboxMessage | null;
  flags: MailUiFlags;
  aiBusy: boolean;
  insertRequest: InsertRequest;
  onConsumeInsertRequest: (nonce: string) => void;
  onClose: () => void;
  onTodoMessage: (message: InboxMessage) => void;
  onDoneMessage: (message: InboxMessage) => void;
  onSnoozeMessage: (message: InboxMessage) => void;
  onThreadAction: (operation: InboxThreadStateOperation, message: InboxMessage) => Promise<void>;
  showToast: (message: string, options?: { actionLabel?: string; onAction?: () => void; secondaryActionLabel?: string; onSecondaryAction?: () => void }) => void;
  loadInboxEmailBody: (messageId: string, mailbox?: string) => Promise<string>;
  loadInboxThreadPage: (mailbox: string, threadId: string, options?: {
    anchorMessageId?: string;
    beforeIndex?: number | null;
    limit?: number;
    includeDisplayBody?: boolean;
    forceRefresh?: boolean;
  }) => Promise<InboxThreadPagePayload>;
  loadInboxMessageDisplayBody: (mailbox: string, messageId: string) => Promise<InboxMessageDisplayBodyPayload>;
  loadInboxThreadAssist: (mailbox: string, threadId: string, latestMessageId: string, anchorMessageId?: string) => Promise<InboxThreadAssistPayload>;
  getInboxThreadDraft: (mailbox: string, threadId: string) => Promise<{ exists: boolean; body: string; etag?: string }>;
  saveInboxThreadDraft: (mailbox: string, threadId: string, body: string, ifMatch?: string, message?: Record<string, unknown>) => Promise<{ etag?: string }>;
  deleteInboxThreadDraft: (mailbox: string, threadId: string) => Promise<{ ok?: boolean }>;
  prepareInboxAttachmentAccess: (mailbox: string, messageId: string, attachmentId: string, mode: "preview" | "download") => Promise<AttachmentDownloadPayload>;
  submitMailContextPrompt: (request: SubmitMailPromptRequest) => Promise<unknown>;
  sendInboxThreadReply: (args: { mailbox: string; threadId: string; to: string; body: string; replyMode?: string; dryRun?: boolean }) => Promise<{ ok?: boolean; error?: string }>;
  contactAvatars?: Record<string, string>;
  loadContactAvatars: (emails: string[], mailbox?: string) => Promise<{ avatars: Record<string, string>; permissionRequired: boolean }>;
  latestThreadMessageId?: string;
  latestThreadInternalDate?: string;
}) {
  const [page, setPage] = useState<InboxThreadPagePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [assist, setAssist] = useState<InboxThreadAssistPayload | null>(null);
  const [assistLoading, setAssistLoading] = useState(false);
  const [assistError, setAssistError] = useState("");
  const [composerOpen, setComposerOpen] = useState(false);
  const [composerClosing, setComposerClosing] = useState(false);
  const [composerExpanded, setComposerExpanded] = useState(false);
  const [draft, setDraft] = useState("");
  const [draftDirty, setDraftDirty] = useState(false);
  const [draftEtag, setDraftEtag] = useState("");
  const [sending, setSending] = useState(false);
  const [toolbarPending, setToolbarPending] = useState(false);
  const [displayBodyLoading, setDisplayBodyLoading] = useState<Set<string>>(() => new Set());
  const [displayBodyLoaded, setDisplayBodyLoaded] = useState<Set<string>>(() => new Set());
  const [displayBodyErrors, setDisplayBodyErrors] = useState<Record<string, string>>({});
  const [previewAttachment, setPreviewAttachment] = useState<PreviewAttachmentRef | null>(null);
  const [previewAccess, setPreviewAccess] = useState<ResolvedAttachmentAccess | null>(null);
  const [previewText, setPreviewText] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [previewTextTruncated, setPreviewTextTruncated] = useState(false);
  const [previewTransition, setPreviewTransition] = useState<PreviewTransition>("idle");
  const [previewSwitching, setPreviewSwitching] = useState(false);
  const [attachmentDownloads, setAttachmentDownloads] = useState<Record<string, boolean>>({});
  const previewBodyRef = useRef<HTMLDivElement | null>(null);
  const previewSwitchTokenRef = useRef(0);
  const [, setPreviewCacheRevision] = useState(0);
  const [resolvedAvatars, setResolvedAvatars] = useState<Record<string, string>>({});
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const footerRef = useRef<HTMLElement | null>(null);
  const composerRef = useRef<HTMLDivElement | null>(null);
  const bodyRef = useRef<HTMLTextAreaElement | null>(null);
  const loadInboxEmailBodyRef = useRef(loadInboxEmailBody);
  const loadInboxThreadPageRef = useRef(loadInboxThreadPage);
  const loadInboxMessageDisplayBodyRef = useRef(loadInboxMessageDisplayBody);
  const loadFullDisplayBodyRef = useRef<(item: InboxThreadMessage, force?: boolean) => Promise<void>>(async () => undefined);
  const loadInboxThreadAssistRef = useRef(loadInboxThreadAssist);
  const getInboxThreadDraftRef = useRef(getInboxThreadDraft);
  const saveInboxThreadDraftRef = useRef(saveInboxThreadDraft);
  const loadContactAvatarsRef = useRef(loadContactAvatars);
  const assistRequestKeyRef = useRef("");
  const previewAccessRef = useRef<ResolvedAttachmentAccess | null>(null);
  const previewCacheRef = useRef<Map<string, PreviewCacheEntry>>(new Map());
  const previewSessionRef = useRef(0);
  const currentPreviewKeyRef = useRef("");
  const backgroundWarmSessionRef = useRef(-1);
  const renderedPreviewKeysRef = useRef<Set<string>>(new Set());
  const previewRenderWaitersRef = useRef<Map<string, () => void>>(new Map());
  const attachmentDownloadsRef = useRef<Set<string>>(new Set());
  const pendingThreadScrollTargetRef = useRef("");
  const draftLoadSequenceRef = useRef(0);
  const suppressDraftLoadForRef = useRef("");
  const draftEditSequenceRef = useRef(0);
  const pendingDraftSavesRef = useRef(new Set<Promise<void>>());
  const composerCloseTimerRef = useRef<number | null>(null);

  const clearPreviewAccess = () => {
    previewAccessRef.current = null;
    setPreviewAccess(null);
  };

  const invalidatePreviewCache = () => {
    previewSessionRef.current += 1;
    backgroundWarmSessionRef.current = -1;
    currentPreviewKeyRef.current = "";
    for (const entry of previewCacheRef.current.values()) entry.access?.revoke?.();
    previewCacheRef.current.clear();
    renderedPreviewKeysRef.current.clear();
    for (const resolve of previewRenderWaitersRef.current.values()) resolve();
    previewRenderWaitersRef.current.clear();
    clearPreviewAccess();
    setPreviewCacheRevision((value) => value + 1);
  };

  const notifyPreviewCacheChanged = () => setPreviewCacheRevision((value) => value + 1);

  const queueThreadScroll = (nextPage: InboxThreadPagePayload, fallbackMessageId: string) => {
    pendingThreadScrollTargetRef.current = scrollTargetForPage(nextPage, fallbackMessageId);
  };

  const closeComposer = () => {
    if (!composerOpen) return;
    setComposerOpen(false);
    setComposerClosing(true);
    if (composerCloseTimerRef.current) window.clearTimeout(composerCloseTimerRef.current);
    composerCloseTimerRef.current = window.setTimeout(() => {
      setComposerClosing(false);
      composerCloseTimerRef.current = null;
    }, COMPOSER_TRANSITION_MS);
  };

  const threadId = resolveMessageThreadId(message);
  const messageId = message?.id || "";
  const draftStorageKey = `${mailbox.trim().toLowerCase()}:${threadId}`;
  const context = useMemo(() => (message ? buildMailContext(mailbox, message, page) : null), [mailbox, message, page]);
  const visibleThreadMessages = useMemo(
    () => page?.messages || [],
    [page?.messages],
  );
  const latestMessage = visibleThreadMessages[visibleThreadMessages.length - 1] || page?.messages[page.messages.length - 1];
  const selectedIsDraft = isLocalDraftMessage(message);
  const hasThreadUpdate = hasNewerThreadMessage(page, latestThreadMessageId, latestThreadInternalDate);
  const important = Boolean(latestMessage?.label_ids?.includes("IMPORTANT") || message?.important);
  const starred = Boolean(latestMessage?.label_ids?.includes("STARRED") || message?.starred);
  const trashed = Boolean(latestMessage?.label_ids?.includes("TRASH") || message?.label_ids?.includes("TRASH"));
  const isTodo = Boolean(message && flags.todos.includes(message.id));
  const isSnoozed = Boolean(message && flags.snoozed.includes(message.id));
  const isSent = Boolean(message && (
    message.label_ids?.some((label) => label.toUpperCase() === "SENT")
    || isOutboundMessageForMailbox(message.from || undefined, mailbox)
  ));
  const isDone = Boolean(message && (flags.done.includes(message.id) || isSent));
  const previewableAttachments = useMemo(
    () => visibleThreadMessages.flatMap((item) => item.attachments
      .filter(isPreviewableAttachment)
      .map((attachment) => ({
        attachment,
        messageId: attachment.message_id || item.id,
      }))) || [],
    [visibleThreadMessages],
  );
  const replyPromptTarget = useMemo(
    () => [...visibleThreadMessages].reverse().find((item) => !isOutboundMessageForMailbox(item.from, mailbox))
      || (message && !isOutboundMessageForMailbox(message.from || undefined, mailbox) ? message : null),
    [mailbox, message, visibleThreadMessages],
  );
  const quickReplySuggestions = useMemo(() => {
    const generated = (assist?.quick_replies || []).filter((item) => item.label && item.intent);
    return generated;
  }, [assist?.quick_replies]);
  const showQuickReplies = Boolean(
    context
    && quickReplySuggestions.length
    && replyPromptTarget
  );
  const quickReplyRenderKey = quickReplySuggestions
    .map((item) => `${item.id}:${item.label}:${item.intent}`)
    .join("|");
  const fromAddress = useMemo(() => firstAddress(message?.from || undefined), [message?.from]);
  const toAddresses = useMemo(() => splitAddresses(message?.to).map(senderParts), [message?.to]);
  const fromAvatarUrl = fromAddress ? (resolvedAvatars[fromAddress.email.toLowerCase()] || contactAvatars?.[fromAddress.email.toLowerCase()]) : undefined;
  const recipientAvatarUrls = useMemo(() => {
    const avatars = { ...contactAvatars, ...resolvedAvatars };
    return Object.fromEntries(toAddresses.map((address) => [address.email.toLowerCase(), avatars[address.email.toLowerCase()]]));
  }, [contactAvatars, resolvedAvatars, toAddresses]);
  const displaySubject = page?.subject || message?.subject || "(no subject)";
  const latestSubject = page?.latest_subject || message?.latest_subject || "";
  const subjectWasModified = Boolean(
    latestSubject
    && displaySubject
    && normalizeComparableSubject(latestSubject) !== normalizeComparableSubject(displaySubject)
  );

  useLayoutEffect(() => {
    const targetMessageId = pendingThreadScrollTargetRef.current;
    if (!targetMessageId) return;
    pendingThreadScrollTargetRef.current = "";
    scrollToLatestMessage(scrollRef.current, targetMessageId, "smooth");
  }, [page]);

  useLayoutEffect(() => {
    if (!open || !showQuickReplies) return;
    const scroller = scrollRef.current;
    if (!scroller) return;
    // AI prompts arrive after the thread page. They are part of the detail's
    // destination, so always reveal them even if the user scrolled elsewhere.
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
  }, [assistLoading, open, quickReplyRenderKey, showQuickReplies]);

  useLayoutEffect(() => {
    if (!composerOpen) return;
    const scroller = scrollRef.current;
    const footer = footerRef.current;
    if (!scroller || !footer) return;
    // The composer reduces the context viewport. Keep the latest thread content
    // (including delayed AI prompts) above it throughout the transition.
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
    const ResizeObserverCtor = window.ResizeObserver;
    const observer = ResizeObserverCtor
      ? new ResizeObserver(() => {
        scroller.scrollTop = scroller.scrollHeight;
      })
      : null;
    observer?.observe(footer);
    return () => observer?.disconnect();
  }, [composerExpanded, composerOpen]);

  useEffect(() => {
    loadInboxEmailBodyRef.current = loadInboxEmailBody;
    loadInboxThreadPageRef.current = loadInboxThreadPage;
    loadInboxMessageDisplayBodyRef.current = loadInboxMessageDisplayBody;
    loadInboxThreadAssistRef.current = loadInboxThreadAssist;
    getInboxThreadDraftRef.current = getInboxThreadDraft;
    saveInboxThreadDraftRef.current = saveInboxThreadDraft;
    loadContactAvatarsRef.current = loadContactAvatars;
  }, [getInboxThreadDraft, loadContactAvatars, loadInboxEmailBody, loadInboxMessageDisplayBody, loadInboxThreadAssist, loadInboxThreadPage, saveInboxThreadDraft]);

  useEffect(() => () => {
    previewSessionRef.current += 1;
    for (const entry of previewCacheRef.current.values()) entry.access?.revoke?.();
    previewCacheRef.current.clear();
    previewAccessRef.current = null;
    attachmentDownloadsRef.current.clear();
    if (composerCloseTimerRef.current) window.clearTimeout(composerCloseTimerRef.current);
  }, []);

  useEffect(() => {
    if (!open || !message || !mailbox || !messageId) return;
    let cancelled = false;
    const anchorMessage = message;
    setPage(null);
    setLoading(true);
    setError("");
    setDisplayBodyLoading(new Set());
    setDisplayBodyLoaded(new Set());
    setDisplayBodyErrors({});
    setAssist(null);
    setAssistLoading(false);
    setAssistError("");
    assistRequestKeyRef.current = "";
    if (composerCloseTimerRef.current) window.clearTimeout(composerCloseTimerRef.current);
    composerCloseTimerRef.current = null;
    setComposerOpen(false);
    setComposerClosing(false);
    setComposerExpanded(false);
    setDraft("");
    setDraftDirty(false);
    setDraftEtag("");
    suppressDraftLoadForRef.current = "";
    setPreviewAttachment(null);
    invalidatePreviewCache();
    setPreviewText("");
    setPreviewLoading(false);
    setPreviewError("");
    setPreviewTextTruncated(false);
    const requestAssist = (latestMessageId: string) => {
      if (!threadId || !latestMessageId) {
        setAssistLoading(false);
        return;
      }
      const requestKey = `${mailbox}:${threadId}:${latestMessageId}:${messageId}`;
      if (assistRequestKeyRef.current === requestKey) return;
      assistRequestKeyRef.current = requestKey;
      setAssistError("");
      setAssistLoading(true);
      void loadInboxThreadAssistRef.current(mailbox, threadId, latestMessageId, messageId)
        .then((result) => {
          if (!cancelled && assistRequestKeyRef.current === requestKey) setAssist(result);
        })
        .catch((reason) => {
          if (!cancelled && assistRequestKeyRef.current === requestKey) setAssistError(reason instanceof Error ? reason.message : String(reason));
        })
        .finally(() => {
          if (!cancelled && assistRequestKeyRef.current === requestKey) setAssistLoading(false);
        });
    };
    const loadFullAnchorMessage = (visiblePage: InboxThreadPagePayload) => {
      // 首屏仍保持受限预览，选中邮件若被截断则立即经 body_url 加载完整正文。
      // 仅加载用户打开的锚点邮件，避免打开长线程时并发请求全部历史正文。
      const anchorItem = visiblePage.messages.find((item) => item.id === messageId);
      if (anchorItem?.body_truncated) void loadFullDisplayBodyRef.current(anchorItem, true);
    };
    const load = async () => {
      try {
        if (threadId) {
          const cachedPage = await getCachedThreadPage(mailbox, threadId, latestThreadMessageId || messageId);
          if (cancelled) return;
          if (cachedPage) {
            const visibleCachedPage = withoutGmailDraftThreadMessages(cachedPage);
            queueThreadScroll(visibleCachedPage, messageId);
            setPage(visibleCachedPage);
            requestAssist(visibleCachedPage.latest_message_id || messageId);
            if (
              !pageMayBeMissingAnchorAttachments(visibleCachedPage, anchorMessage)
              && !pageMayBeMissingThreadContext(visibleCachedPage, anchorMessage)
            ) {
              void hydrateCachedThreadPageBodies(mailbox, visibleCachedPage)
                .then((hydratedPage) => {
                  if (cancelled) return;
                  if (hydratedPage !== visibleCachedPage) {
                    queueThreadScroll(hydratedPage, messageId);
                    setPage(hydratedPage);
                  }
                  loadFullAnchorMessage(hydratedPage);
                })
                .catch(() => {
                  if (!cancelled) loadFullAnchorMessage(visibleCachedPage);
                });
              return;
            }
          }
          const nextPage = await loadInboxThreadPageRef.current(mailbox, threadId, { anchorMessageId: messageId, limit: 5, includeDisplayBody: true });
          if (cancelled) return;
          const visiblePage = withoutGmailDraftThreadMessages(nextPage);
          queueThreadScroll(visiblePage, messageId);
          setPage(visiblePage);
          void cacheThreadPage(mailbox, visiblePage);
          loadFullAnchorMessage(visiblePage);
          requestAssist(visiblePage.latest_message_id || messageId);
        } else {
          const cachedBody = await getCachedMessageBody(mailbox, messageId, anchorMessage.internal_date);
          if (cancelled) return;
          if (cachedBody) {
            setPage(singleMessagePageFromBody(mailbox, anchorMessage, cachedBody));
            return;
          }
          const body = await loadInboxEmailBodyRef.current(messageId, mailbox);
          if (cancelled) return;
          const nextPage = singleMessagePageFromBody(mailbox, anchorMessage, { body_text: body, attachments: inboxMessageAttachments(anchorMessage) });
          setPage(nextPage);
          void setCachedMessageBody(mailbox, anchorMessage, { body_text: body, attachments: inboxMessageAttachments(anchorMessage) });
        }
      } catch (reason) {
        if (cancelled) return;
        const threadError = reason instanceof Error ? reason.message : String(reason);
        if (threadId) {
          try {
            const display = await resolveDisplayBodyPayload(await loadInboxMessageDisplayBodyRef.current(mailbox, messageId));
            if (cancelled) return;
            setPage(singleMessagePageFromBody(mailbox, anchorMessage, {
              body_html: display.body_html || "",
              body_text: display.body_text || "",
              body_truncated: Boolean(display.body_truncated),
              attachments: inboxMessageAttachments(anchorMessage),
            }));
            if (!display.body_url) {
              void setCachedMessageBody(mailbox, anchorMessage, {
                body_html: display.body_html || "",
                body_text: display.body_text || "",
                body_truncated: Boolean(display.body_truncated),
                attachments: inboxMessageAttachments(anchorMessage),
              });
            }
            setError("");
            requestAssist(messageId);
          } catch {
            setError(threadError);
          }
        } else {
          setError(threadError);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  // A saved local draft overlays the selected inbox message with a new object. That
  // is not a navigation event, so avoid using the whole message as a dependency:
  // doing so would reload the thread and reset the composer after an AI insertion.
  }, [latestThreadMessageId, mailbox, messageId, open, threadId]);

  useEffect(() => {
    setResolvedAvatars(contactAvatars || {});
  }, [contactAvatars]);

  useEffect(() => {
    if (!open || !mailbox) return;
    const seedValues = [message?.from, message?.to];
    const pageValues = page?.messages.flatMap((item) => [item.from, item.to, item.cc, item.bcc]) || [];
    const emails = [...seedValues, ...pageValues]
      .flatMap((item) => splitAddresses(item))
      .map((item) => senderParts(item).email.toLowerCase())
      .filter(Boolean)
      .filter((email, index, all) => all.indexOf(email) === index)
      .filter((email) => !(contactAvatars?.[email] || resolvedAvatars[email]));
    if (!emails.length) return;
    let cancelled = false;
    void loadContactAvatarsRef.current(emails, mailbox)
      .then(({ avatars }) => {
        if (cancelled || !avatars || !Object.keys(avatars).length) return;
        setResolvedAvatars((current) => ({ ...current, ...avatars }));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [contactAvatars, mailbox, message?.from, message?.to, open, page, resolvedAvatars]);

  useEffect(() => {
    if ((!composerOpen && !selectedIsDraft) || !threadId || !mailbox) return;
    // 用户已明确替换或丢弃当前草稿时，不能再用持久化草稿自动回填编辑栏。
    if (suppressDraftLoadForRef.current === draftStorageKey) return;
    const requestId = ++draftLoadSequenceRef.current;
    let cancelled = false;
    void getInboxThreadDraftRef.current(mailbox, threadId)
      .then((stored) => {
        if (cancelled || requestId !== draftLoadSequenceRef.current) return;
        const storedBody = stored.body || "";
        const fallbackBody = message?.draft_body || "";
        const nextDraft = storedBody || fallbackBody;
        if (nextDraft) {
          setComposerOpen(true);
          setDraft(nextDraft);
          setDraftEtag(stored.etag || "");
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [composerOpen, draftStorageKey, mailbox, message?.draft_body, selectedIsDraft, threadId]);

  useEffect(() => {
    if (!composerOpen || !threadId || !mailbox || !draftDirty) return;
    const editSequence = draftEditSequenceRef.current;
    const timer = window.setTimeout(() => {
      const persistDraft = async () => {
        try {
          const result = await saveInboxThreadDraftRef.current(mailbox, threadId, draft, draftEtag || undefined, {
            id: latestMessage?.id || message?.id || "",
            thread_id: threadId,
            mailbox,
            internal_date: latestMessage?.internal_date || message?.internal_date || "",
            date: message?.date || "",
            from: latestMessage?.from || message?.from || "",
            to: latestMessage?.to || message?.to || "",
            subject: latestMessage?.subject || message?.subject || "",
            label_ids: (latestMessage?.label_ids || message?.label_ids || []).filter((label) => label.toUpperCase() !== "DRAFT"),
            important: Boolean(message?.important || latestMessage?.label_ids?.includes("IMPORTANT")),
            starred: Boolean(message?.starred || latestMessage?.label_ids?.includes("STARRED")),
            has_attachment: Boolean(message?.has_attachment),
            attachment_count: Number(message?.attachment_count || 0),
          });
          if (editSequence !== draftEditSequenceRef.current) return;
          setDraftDirty(false);
          if (result.etag) setDraftEtag(result.etag);
        } catch {
          // The next edit can retry the save; keep the editor usable meanwhile.
        }
      };
      const pendingSave = persistDraft();
      pendingDraftSavesRef.current.add(pendingSave);
      void pendingSave.finally(() => pendingDraftSavesRef.current.delete(pendingSave));
    }, 500);
    return () => window.clearTimeout(timer);
  }, [composerOpen, draft, draftDirty, draftEtag, latestMessage, mailbox, message, threadId]);

  useEffect(() => {
    if (!composerOpen) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (!composerRef.current?.contains(event.target as Node) && !draft.trim() && !sending && !aiBusy) {
        closeComposer();
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [aiBusy, composerOpen, draft, sending]);

  useEffect(() => {
    if (!insertRequest || !message || !threadId) return;
    const { artifact, mode, nonce } = insertRequest;
    if (!matchesDraftArtifact(mailbox, threadId, artifact)) {
      onConsumeInsertRequest(nonce);
      showToast("Open the matching email thread before applying this draft.");
      return;
    }
    onConsumeInsertRequest(nonce);
    if (mode === "replace") {
      // Replace 是用户明确操作：直接覆盖编辑栏，不走 discard 或持久化草稿回填路径。
      suppressDraftLoadForRef.current = draftStorageKey;
      draftLoadSequenceRef.current += 1;
      setComposerOpen(true);
      draftEditSequenceRef.current += 1;
      setDraft(artifact.body);
      setDraftDirty(true);
      requestAnimationFrame(() => bodyRef.current?.focus());
      return;
    }
    // Append 仍保留编辑栏原文，并使已发起的旧草稿读取失效。
    draftLoadSequenceRef.current += 1;
    setComposerOpen(true);
    draftEditSequenceRef.current += 1;
    setDraft((current) => mergeDraftArtifactBody(current, artifact.body, mode));
    setDraftDirty(true);
    requestAnimationFrame(() => bodyRef.current?.focus());
  }, [draft, draftStorageKey, insertRequest, mailbox, message, onConsumeInsertRequest, showToast, threadId]);

  const refreshThread = async () => {
    if (!threadId || !messageId) return;
    setLoading(true);
    setError("");
    try {
      const refreshed = await loadInboxThreadPageRef.current(mailbox, threadId, { anchorMessageId: messageId, limit: 5, includeDisplayBody: true, forceRefresh: true });
      const visiblePage = withoutGmailDraftThreadMessages(refreshed);
      queueThreadScroll(visiblePage, messageId);
      setPage(visiblePage);
      void cacheThreadPage(mailbox, visiblePage);
      if (visiblePage.latest_message_id) {
        setAssistLoading(true);
        const requestKey = `${mailbox}:${threadId}:${visiblePage.latest_message_id}:${messageId}`;
        assistRequestKeyRef.current = requestKey;
        void loadInboxThreadAssistRef.current(mailbox, threadId, visiblePage.latest_message_id, messageId)
          .then((result) => {
            if (assistRequestKeyRef.current === requestKey) setAssist(result);
          })
          .catch((reason) => {
            if (assistRequestKeyRef.current === requestKey) setAssistError(reason instanceof Error ? reason.message : String(reason));
          })
          .finally(() => {
            if (assistRequestKeyRef.current === requestKey) setAssistLoading(false);
          });
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  };

  const loadEarlier = async () => {
    if (!page?.has_earlier || page.next_before_index === null || !threadId) return;
    const scroller = scrollRef.current;
    const previousHeight = scroller?.scrollHeight || 0;
    const older = await loadInboxThreadPageRef.current(mailbox, threadId, {
      anchorMessageId: message?.id,
      beforeIndex: page.next_before_index,
      limit: 5,
      includeDisplayBody: true,
    });
    const visibleOlder = withoutGmailDraftThreadMessages(older);
    void cacheThreadPage(mailbox, visibleOlder);
    setPage((current) => current ? {
      ...visibleOlder,
      messages: [...visibleOlder.messages, ...current.messages.filter((item) => !visibleOlder.messages.some((olderItem) => olderItem.id === item.id))],
    } : visibleOlder);
    requestAnimationFrame(() => {
      if (!scroller) return;
      scroller.scrollTop += scroller.scrollHeight - previousHeight;
    });
  };

  const loadFullDisplayBody = async (item: InboxThreadMessage, force = false) => {
    if (!force && (displayBodyLoading.has(item.id) || displayBodyLoaded.has(item.id))) return;
    setDisplayBodyLoading((current) => new Set(current).add(item.id));
    setDisplayBodyErrors((current) => ({ ...current, [item.id]: "" }));
    try {
      const cached = await getCachedMessageBody(mailbox, item.id, item.internal_date);
      if (cached && !cached.body_truncated && (cached.body_html || (!item.body_html && cached.body_text))) {
        setPage((current) => current ? {
          ...current,
          messages: current.messages.map((message) => message.id === item.id ? {
            ...message,
            body_html: cached.body_html || "",
            body_text: cached.body_text || "",
            body_truncated: false,
          } : message),
        } : current);
        setDisplayBodyLoaded((current) => new Set(current).add(item.id));
        return;
      }
      const display = await resolveDisplayBodyPayload(await loadInboxMessageDisplayBody(mailbox, item.id));
      if (!display.body_url) {
        void setCachedMessageBody(mailbox, threadMessageToInboxMessage(item, mailbox), {
          body_html: display.body_html || "",
          body_text: display.body_text || "",
          body_truncated: Boolean(display.body_truncated),
          attachments: item.attachments || [],
        });
      }
      setPage((current) => current ? {
        ...current,
        messages: current.messages.map((message) => message.id === item.id ? {
          ...message,
          body_html: display.body_html || "",
          body_text: display.body_text || "",
          body_truncated: Boolean(display.body_truncated),
        } : message),
      } : current);
      setDisplayBodyLoaded((current) => new Set(current).add(item.id));
    } catch (reason) {
      setDisplayBodyErrors((current) => ({
        ...current,
        [item.id]: reason instanceof Error ? reason.message : String(reason),
      }));
    } finally {
      setDisplayBodyLoading((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
    }
  };
  loadFullDisplayBodyRef.current = loadFullDisplayBody;

  const prepareAttachmentAccess = async (item: PreviewAttachmentRef, mode: "preview" | "download") => {
    const messageId = String(item.messageId || item.attachment.message_id || "").trim();
    const attachmentId = String(item.attachment.id || "").trim();
    if (!messageId || !attachmentId) {
      throw new Error("This attachment is missing its message or attachment id. Refresh the thread and try again.");
    }
    const result = await prepareInboxAttachmentAccess(mailbox, messageId, attachmentId, mode);
    return resolveAttachmentAccess(result);
  };

  const attachmentDownloadKey = (item: PreviewAttachmentRef) =>
    `${String(item.messageId || item.attachment.message_id || "").trim()}::${String(item.attachment.id || "").trim()}`;

  const isAttachmentDownloading = (item: PreviewAttachmentRef) => Boolean(attachmentDownloads[attachmentDownloadKey(item)]);

  const downloadAttachment = async (item: PreviewAttachmentRef) => {
    const stateKey = attachmentDownloadKey(item);
    if (attachmentDownloadsRef.current.has(stateKey)) return;
    let access: ResolvedAttachmentAccess | null = null;
    attachmentDownloadsRef.current.add(stateKey);
    setAttachmentDownloads((current) => ({ ...current, [stateKey]: true }));
    showToast("Preparing download...");
    try {
      access = await prepareAttachmentAccess(item, "download");
      triggerAttachmentDownload(access, item.attachment.filename);
    } catch (reason) {
      throw new Error(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (access?.kind === "blob") {
        window.setTimeout(() => access?.revoke?.(), 60_000);
      }
      setAttachmentDownloads((current) => {
        const next = { ...current };
        delete next[stateKey];
        return next;
      });
      attachmentDownloadsRef.current.delete(stateKey);
    }
  };

  const applyPreviewEntry = (entry: PreviewCacheEntry) => {
    previewAccessRef.current = entry.access;
    setPreviewAccess(entry.access);
    setPreviewText(entry.text);
    setPreviewTextTruncated(entry.textTruncated);
    setPreviewError(entry.error);
  };

  const ensurePreviewEntry = (item: PreviewAttachmentRef, shouldRender: boolean, session: number) => {
    const key = previewCacheKey(item);
    const cached = previewCacheRef.current.get(key);
    if (cached) {
      if (shouldRender && !cached.shouldRender) {
        cached.shouldRender = true;
        notifyPreviewCacheChanged();
      }
      return cached.promise || Promise.resolve(cached);
    }

    const entry: PreviewCacheEntry = {
      key,
      item,
      status: "loading",
      access: null,
      text: "",
      textTruncated: false,
      error: "",
      shouldRender,
    };
    const promise = (async () => {
      let access: ResolvedAttachmentAccess | null = null;
      try {
        access = await prepareAttachmentAccess(item, "preview");
        const kind = normalizeAttachmentKind(item.attachment);
        if (access.kind === "url" && kind !== "text") access = await materializeAttachmentAccess(access);
        if (session !== previewSessionRef.current) {
          access.revoke?.();
          throw new Error("Preview session ended.");
        }
        entry.access = access;
        if (kind === "text") {
          if ((item.attachment.size || 0) > TEXT_PREVIEW_MAX_BYTES) {
            entry.textTruncated = true;
          } else {
            const response = await fetch(access.url);
            if (!response.ok) throw new Error(`Preview fetch failed with ${response.status}`);
            entry.text = await response.text();
          }
        }
        entry.status = "ready";
      } catch (reason) {
        if (session !== previewSessionRef.current) throw reason;
        access?.revoke?.();
        entry.access = null;
        entry.status = "error";
        entry.error = reason instanceof Error ? reason.message : String(reason);
      } finally {
        entry.promise = undefined;
        if (session === previewSessionRef.current) notifyPreviewCacheChanged();
      }
      return entry;
    })();
    entry.promise = promise;
    previewCacheRef.current.set(key, entry);
    notifyPreviewCacheChanged();
    return promise;
  };

  const markPreviewRendered = (key: string) => {
    renderedPreviewKeysRef.current.add(key);
    previewRenderWaitersRef.current.get(key)?.();
    previewRenderWaitersRef.current.delete(key);
  };

  const waitForPreviewRender = (key: string, session: number) => {
    if (renderedPreviewKeysRef.current.has(key) || session !== previewSessionRef.current) return Promise.resolve();
    return new Promise<void>((resolve) => {
      let timer = 0;
      const finish = () => {
        window.clearTimeout(timer);
        previewRenderWaitersRef.current.delete(key);
        resolve();
      };
      timer = window.setTimeout(finish, 30_000);
      previewRenderWaitersRef.current.set(key, finish);
    });
  };

  const warmRemainingPreviews = (activeKey: string, session: number) => {
    if (backgroundWarmSessionRef.current === session) return;
    backgroundWarmSessionRef.current = session;
    const activeIndex = previewableAttachments.findIndex((item) => previewCacheKey(item) === activeKey);
    const ordered = activeIndex < 0
      ? previewableAttachments
      : [...previewableAttachments.slice(activeIndex + 1), ...previewableAttachments.slice(0, activeIndex)];
    void (async () => {
      let renderedCount = 0;
      let estimatedBytes = 0;
      for (const item of ordered) {
        if (session !== previewSessionRef.current) return;
        const key = previewCacheKey(item);
        if (key === activeKey) continue;
        const estimate = estimateAttachmentPreviewMemory(item.attachment);
        const shouldRender = renderedCount < MAX_BACKGROUND_RENDERED_PREVIEWS
          && estimatedBytes + estimate <= MAX_BACKGROUND_PREVIEW_BYTES;
        const entry = await ensurePreviewEntry(item, shouldRender, session).catch(() => null);
        if (!entry || session !== previewSessionRef.current || entry.status !== "ready") continue;
        if (!shouldRender) continue;
        renderedCount += 1;
        estimatedBytes += estimate;
        const kind = normalizeAttachmentKind(item.attachment);
        if (kind === "pdf" || kind === "image") await waitForPreviewRender(key, session);
        else markPreviewRendered(key);
      }
    })();
  };

  const loadPreviewItem = async (item: PreviewAttachmentRef) => {
    const key = previewCacheKey(item);
    const session = previewSessionRef.current;
    currentPreviewKeyRef.current = key;
    setPreviewAttachment(item);
    setPreviewError("");
    setPreviewText("");
    setPreviewTextTruncated(false);
    clearPreviewAccess();
    const cached = previewCacheRef.current.get(key);
    if (cached?.status === "error") {
      previewCacheRef.current.delete(key);
    } else if (cached?.status === "ready") {
      cached.shouldRender = true;
      applyPreviewEntry(cached);
      setPreviewLoading(false);
      notifyPreviewCacheChanged();
      warmRemainingPreviews(key, session);
      return;
    }
    setPreviewLoading(true);
    try {
      const entry = await ensurePreviewEntry(item, true, session);
      if (session !== previewSessionRef.current || currentPreviewKeyRef.current !== key) return;
      applyPreviewEntry(entry);
      if (entry.status === "ready") warmRemainingPreviews(key, session);
    } catch (reason) {
      if (session === previewSessionRef.current && currentPreviewKeyRef.current === key) {
        clearPreviewAccess();
        setPreviewError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (session === previewSessionRef.current && currentPreviewKeyRef.current === key) setPreviewLoading(false);
    }
  };

  const previewIndex = previewAttachment
    ? previewableAttachments.findIndex((item) => item.attachment.id === previewAttachment.attachment.id && item.messageId === previewAttachment.messageId)
    : -1;
  const activePreviewKey = previewAttachment ? previewCacheKey(previewAttachment) : "";
  const mountedPreviewEntries = Array.from(previewCacheRef.current.values()).filter((entry) => (
    entry.status === "ready"
    && entry.access
    && (entry.shouldRender || entry.key === activePreviewKey)
  ));

  const closePreview = () => {
    previewSwitchTokenRef.current += 1;
    setPreviewAttachment(null);
    setPreviewLoading(false);
    setPreviewSwitching(false);
    setPreviewTransition("idle");
    setPreviewError("");
    setPreviewText("");
    setPreviewTextTruncated(false);
    invalidatePreviewCache();
  };

  const openAttachmentPreview = async (item: PreviewAttachmentRef) => {
    previewSwitchTokenRef.current += 1;
    setPreviewSwitching(false);
    setPreviewTransition("idle");
    await loadPreviewItem(item);
  };

  const stepPreview = async (direction: -1 | 1) => {
    if (previewIndex < 0 || previewSwitching) return;
    const next = previewableAttachments[previewIndex + direction];
    if (!next) return;
    const switchToken = previewSwitchTokenRef.current + 1;
    previewSwitchTokenRef.current = switchToken;
    const transitionDirection = direction < 0 ? "previous" : "next";
    setPreviewSwitching(true);
    setPreviewTransition(`exit-${transitionDirection}`);
    try {
      await waitForPreviewAnimation(PREVIEW_EXIT_MS);
      if (previewSwitchTokenRef.current !== switchToken) return;
      if (previewBodyRef.current) previewBodyRef.current.scrollTop = 0;
      await loadPreviewItem(next);
      if (previewSwitchTokenRef.current !== switchToken) return;
      setPreviewTransition(`enter-${transitionDirection}`);
      await waitForPreviewAnimation(PREVIEW_ENTER_MS);
    } finally {
      if (previewSwitchTokenRef.current === switchToken) {
        setPreviewTransition("idle");
        setPreviewSwitching(false);
      }
    }
  };

  const submitPrompt = async (visiblePrompt: string, expectedArtifact: "draft_reply" | "summary" = "draft_reply", forceNewConversation = false) => {
    if (!context || context.kind !== "gmail_thread") return;
    await submitMailContextPrompt({
      visiblePrompt,
      context,
      expectedArtifact,
      contextTitle: page?.subject || message?.subject || "",
      forceNewConversation,
    });
  };

  const expandOverview = () => {
    void submitPrompt("Summarize this thread", "summary", true);
  };

  const retryOverview = () => {
    if (!context || context.kind !== "gmail_thread") return;
    setAssistError("");
    setAssistLoading(true);
    void loadInboxThreadAssist(mailbox, context.thread_id, context.latest_message_id, context.anchor_message_id)
      .then(setAssist)
      .catch((reason) => setAssistError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setAssistLoading(false));
  };

  const sendReply = async () => {
    if (!threadId || !draft.trim() || !latestMessage) return;
    const to = deriveReplyToAddress(latestMessage, mailbox);
    if (!to) {
      showToast("Reply recipient is unavailable.");
      return;
    }
    setSending(true);
    try {
      const result = await sendInboxThreadReply({ mailbox, threadId, to, body: draft, replyMode: "reply_to_sender", dryRun: false });
      if (!result.ok) throw new Error(result.error || "Failed to send reply");
      if (threadId) await deleteInboxThreadDraft(mailbox, threadId);
      setDraft("");
      setDraftDirty(false);
      showToast("Reply sent.");
      const refreshed = await loadInboxThreadPageRef.current(mailbox, threadId, { anchorMessageId: message?.id, limit: 5, includeDisplayBody: true, forceRefresh: true });
      const visiblePage = withoutGmailDraftThreadMessages(refreshed);
      queueThreadScroll(visiblePage, message?.id || "");
      setPage(visiblePage);
    } catch (reason) {
      showToast(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSending(false);
    }
  };

  const discardDraft = async () => {
    // Discard 是显式清空：阻止当前线程的持久化草稿在关闭后重新打开编辑栏。
    suppressDraftLoadForRef.current = draftStorageKey;
    draftLoadSequenceRef.current += 1;
    draftEditSequenceRef.current += 1;
    setDraft("");
    setDraftDirty(false);
    setDraftEtag("");
    closeComposer();
    setComposerExpanded(false);
    await Promise.allSettled([...pendingDraftSavesRef.current]);
    if (threadId) await deleteInboxThreadDraft(mailbox, threadId);
  };

  const closeThread = () => {
    if (draftDirty && draft.trim()) showToast("Draft saved to Drafts.");
    onClose();
  };

  const runThreadAction = async (operation: InboxThreadStateOperation) => {
    if (!message || toolbarPending) return;
    setToolbarPending(true);
    try {
      await onThreadAction(operation, message);
    } finally {
      setToolbarPending(false);
    }
  };

  if (!message) return null;

  const composerVisible = composerOpen || composerClosing;

  return (
    <>
      <aside className={`mail-detail-drawer ${open ? "is-open" : ""}`} aria-hidden={!open}>
        <header className="mail-detail-header">
          <div className="mail-detail-toolbar">
            <button aria-label="Close thread" data-tooltip="Close thread" onClick={closeThread}><CloseThreadIcon /></button>
            {trashed ? (
              <button className="is-active is-trashed" aria-label="Remove from trash" data-tooltip="Remove from trash" disabled={toolbarPending} onClick={() => void runThreadAction("untrash")}><TrashOffIcon /></button>
            ) : (
              <>
                <button aria-label="Mark unread" data-tooltip="Mark unread" disabled={toolbarPending} onClick={() => void runThreadAction("mark_unread")}><MarkUnreadIcon /></button>
                <button className={starred ? "is-active is-starred" : ""} aria-label={starred ? "Remove stars" : "Add stars"} data-tooltip={starred ? "Remove stars" : "Add stars"} disabled={toolbarPending} onClick={() => void runThreadAction(starred ? "unstar" : "star")}><StarIcon /></button>
                <button className={important ? "is-active is-important" : ""} aria-label={important ? "Mark not important" : "Mark important"} data-tooltip={important ? "Mark not important" : "Mark important"} disabled={toolbarPending} onClick={() => void runThreadAction(important ? "mark_not_important" : "mark_important")}><ImportantIcon /></button>
                <button className={isTodo ? "is-active is-todo" : ""} aria-label={isTodo ? "Click Done to remove" : "Add to Todo"} data-tooltip={isTodo ? "Click Done to remove" : "Add to Todo"} disabled={toolbarPending || isTodo} onClick={() => onTodoMessage(message)}><TodoIcon /></button>
                <button className={isSnoozed ? "is-active is-snoozed" : ""} aria-label={isSnoozed ? "Remove from snoozed" : "Snooze"} data-tooltip={isSnoozed ? "Remove from snoozed" : "Snooze"} disabled={toolbarPending} onClick={() => onSnoozeMessage(message)}><ClockIcon /></button>
                <button aria-label="Move to trash" data-tooltip="Move to trash" disabled={toolbarPending} onClick={() => void runThreadAction("trash")}><TrashIcon /></button>
                <button
                  className={isDone ? "is-active is-done" : ""}
                  aria-label={isSent ? "Sent and done" : isDone ? "Move to inbox" : "Done"}
                  data-tooltip={isSent ? "Sent and done" : isDone ? "Move to inbox" : "Done"}
                  disabled={toolbarPending || isSent}
                  onClick={() => onDoneMessage(message)}
                ><DoneIcon /></button>
              </>
            )}
          </div>
          <div className="mail-detail-summary">
            <h2>{displaySubject}</h2>
            {subjectWasModified ? (
              <p className="mail-detail-subject-note">Latest message subject: {latestSubject}</p>
            ) : null}
            {hasThreadUpdate ? (
              <div className="mail-detail-update-banner" role="status">
                <span>Newer mail is available in this thread.</span>
                <button onClick={() => void refreshThread()}>Update thread</button>
              </div>
            ) : null}
            <div className="mail-detail-overview">
              {assistLoading ? <p className="mail-detail-overview-loading">Generating overview...</p> : assist?.overview ? <MailOverviewAction text={assist.overview} onClick={expandOverview} /> : assistError ? <p>AI is unavailable. <button onClick={retryOverview}>Retry</button></p> : null}
            </div>
            {fromAddress || toAddresses.length ? (
              <div className="mail-detail-participants">
                <HeaderContactRow label="From" address={fromAddress} avatarUrl={fromAvatarUrl} />
                <HeaderRecipientRows label="To" addresses={toAddresses} avatarUrls={recipientAvatarUrls} />
              </div>
            ) : null}
          </div>
        </header>

        <div className="mail-detail-context" ref={scrollRef}>
          {page?.has_earlier ? <button className="mail-detail-load-earlier" onClick={() => void loadEarlier()}>Load earlier messages</button> : null}
          {loading && !page ? <MailDetailLoadingSkeleton /> : null}
          {error ? <div className="mail-detail-error">Thread failed to load. {error}</div> : null}
          {(visibleThreadMessages.length ? visibleThreadMessages : []).map((item) => {
            const waitForFullBody = item.id === messageId
              && item.body_truncated
              && !displayBodyLoaded.has(item.id)
              && !displayBodyErrors[item.id];
            return (
            <article key={item.id} className="mail-thread-message" data-message-id={item.id}>
              <div className="mail-thread-message-head">
                <div className="mail-thread-message-author">
                  {(() => {
                    const sender = senderParts(item.from);
                    const email = sender.email.toLowerCase();
                    return (
                      <>
                        <span className="mail-thread-message-avatar">
                          <ThreadMessageAvatar
                            identity={sender.name || sender.email || item.from}
                            label={sender.name}
                            avatarUrl={resolvedAvatars[email] || contactAvatars?.[email]}
                          />
                        </span>
                        <strong>{sender.name || sender.email || "Unknown sender"}</strong>
                      </>
                    );
                  })()}
                </div>
                <time>{formatAbsoluteDateTime(item.internal_date)}</time>
              </div>
              {waitForFullBody ? <MailThreadBodyLoading /> : item.body_html ? <SafeEmailHtml className="mail-thread-message-body is-html" html={item.body_html} scaleToFit /> : <SafeEmailText className="mail-thread-message-body" text={item.body_text || ""} />}
              {item.body_truncated && !waitForFullBody ? (
                <p className="mail-thread-message-notice">
                  <span>{displayBodyLoaded.has(item.id) ? "This message exceeds the safe display limit." : "This message is too large to display completely."}</span>
                  {!displayBodyLoaded.has(item.id) ? (
                    <button disabled={displayBodyLoading.has(item.id)} onClick={() => void loadFullDisplayBody(item)}>
                      {displayBodyLoading.has(item.id) ? "Loading…" : "Load full message"}
                    </button>
                  ) : null}
                </p>
              ) : null}
              {displayBodyErrors[item.id] ? (
                <p className="mail-thread-message-notice is-error">
                  <span>{displayBodyErrors[item.id]}</span>
                  {waitForFullBody ? <button onClick={() => void loadFullDisplayBody(item)}>Retry</button> : null}
                </p>
              ) : null}
              <AttachmentSection
                attachments={item.attachments}
                expectedAttachmentCount={expectedAttachmentCountForMessage(item, message)}
                onRefresh={() => void refreshThread()}
                onPreview={(attachment) => void openAttachmentPreview({ attachment, messageId: attachment.message_id || item.id })}
                onDownload={(attachment) => void downloadAttachment({ attachment, messageId: attachment.message_id || item.id }).catch((reason) => showToast(reason instanceof Error ? reason.message : String(reason)))}
                isDownloading={(attachment) => isAttachmentDownloading({ attachment, messageId: attachment.message_id || item.id })}
              />
            </article>
            );
          })}
          {showQuickReplies ? (
            <section className="mail-detail-quick-replies" aria-label="Quick reply prompts">
              {quickReplySuggestions.map((item) => (
                <button key={item.id} onClick={() => void submitPrompt(buildQuickReplyPrompt(item))}>
                  <AiSparkleIcon />
                  <span>{item.label}</span>
                </button>
              ))}
            </section>
          ) : null}
        </div>

        <footer ref={footerRef} className={`mail-detail-footer ${composerVisible ? "is-composer-open" : ""} ${composerClosing ? "is-composer-closing" : ""} ${composerExpanded ? "is-expanded" : ""}`}>
          <div className="mail-detail-reply">
            {!composerVisible ? (
              <button className="mail-detail-reply-toggle" onClick={() => {
                setComposerOpen(true);
                requestAnimationFrame(() => bodyRef.current?.focus());
              }}>
                <span>Reply to {deriveReplyToAddress(latestMessage, mailbox) || "thread"}</span>
                <strong>Reply</strong>
              </button>
            ) : (
              <div className="mail-detail-composer" ref={composerRef}>
                <div className="mail-detail-composer-head">
                  <span>To {deriveReplyToAddress(latestMessage, mailbox) || "thread"}</span>
                  <div>
                    <button
                      className="mail-detail-composer-icon-btn"
                      aria-label={composerExpanded ? "Collapse inline" : "Expand inline"}
                      data-tooltip={composerExpanded ? "Collapse inline" : "Expand inline"}
                      onClick={() => setComposerExpanded((value) => !value)}
                    >
                      {composerExpanded ? <CollapseInlineIcon /> : <ExpandInlineIcon />}
                    </button>
                    <button
                      className="mail-detail-composer-icon-btn"
                      aria-label="AI draft"
                      data-tooltip="AI draft"
                      disabled={aiBusy || !context}
                      onClick={() => void submitPrompt("Write a first draft reply to the current thread", "draft_reply", true)}
                    >
                      <AiDraftIcon />
                    </button>
                    <button
                      className="mail-detail-composer-icon-btn"
                      aria-label="Discard draft"
                      data-tooltip="Discard draft"
                      onClick={() => void discardDraft()}
                    >
                      <TrashIcon />
                    </button>
                  </div>
                </div>
                <textarea
                  ref={bodyRef}
                  value={draft}
                  onChange={(event) => {
                    draftEditSequenceRef.current += 1;
                    setDraft(event.target.value);
                    setDraftDirty(true);
                  }}
                  placeholder="Write your reply…"
                />
                <div className="mail-detail-composer-actions">
                  <button className="is-primary" disabled={!draft.trim() || sending} onClick={() => void sendReply()}>{sending ? "Sending…" : "Send"}</button>
                </div>
              </div>
            )}
          </div>
        </footer>
      </aside>

      {previewAttachment ? (
        <div className="attachment-preview-modal" role="dialog" aria-modal="true" aria-label={previewAttachment.attachment.filename}>
          <button className="attachment-preview-backdrop" aria-label="Close attachment preview" onClick={closePreview} />
          <div className="attachment-preview-sheet">
            <header>
              <strong>{previewIndex + 1} / {previewableAttachments.length} - {previewAttachment.attachment.filename}</strong>
              <div>
                <DownloadButton disabled={isAttachmentDownloading(previewAttachment)} onClick={() => void downloadAttachment(previewAttachment).catch((reason) => showToast(reason instanceof Error ? reason.message : String(reason)))} />
                <PreviewToolbarButton label="Close" onClick={closePreview}><CloseIcon /></PreviewToolbarButton>
              </div>
            </header>
            <div ref={previewBodyRef} className={`attachment-preview-body is-${previewTransition}`}>
              {previewLoading ? <p>Loading preview…</p> : null}
              {!previewLoading && previewError ? (
                <div className="attachment-preview-error">
                  <p>{previewError}</p>
                  <div className="attachment-preview-actions">
                    <button onClick={() => void loadPreviewItem(previewAttachment)}>Retry</button>
                    <DownloadButton disabled={isAttachmentDownloading(previewAttachment)} onClick={() => void downloadAttachment(previewAttachment).catch((reason) => showToast(reason instanceof Error ? reason.message : String(reason)))} />
                  </div>
                </div>
              ) : null}
              {mountedPreviewEntries.map((entry) => {
                const access = entry.access!;
                const attachment = entry.item.attachment;
                const kind = normalizeAttachmentKind(attachment);
                const active = entry.key === activePreviewKey;
                return (
                  <div
                    key={entry.key}
                    className={`attachment-preview-pane ${active ? "is-active" : "is-preloading"}`}
                    aria-hidden={!active}
                  >
                    {kind === "pdf" ? (
                      <PdfAttachmentPreview url={access.url} onReady={() => markPreviewRendered(entry.key)} />
                    ) : null}
                    {kind === "image" ? <img src={access.url} alt={active ? attachment.filename : ""} onLoad={() => markPreviewRendered(entry.key)} /> : null}
                    {kind === "audio" ? <audio src={access.url} controls={active} preload="auto" onCanPlay={() => markPreviewRendered(entry.key)} /> : null}
                    {kind === "video" ? <video src={access.url} controls={active} preload="auto" onCanPlay={() => markPreviewRendered(entry.key)} /> : null}
                    {kind === "text" && entry.textTruncated ? (
                      <div className="attachment-preview-external-only">
                        <p>Text preview is limited to 256 KB for safety. Download the file to inspect the full content.</p>
                        {active ? (
                          <div className="attachment-preview-actions">
                            <DownloadButton disabled={isAttachmentDownloading(entry.item)} onClick={() => void downloadAttachment(entry.item).catch((reason) => showToast(reason instanceof Error ? reason.message : String(reason)))} />
                          </div>
                        ) : null}
                      </div>
                    ) : null}
                    {kind === "text" && !entry.textTruncated ? <pre>{entry.text}</pre> : null}
                  </div>
                );
              })}
            </div>
            {previewIndex > 0 ? <PreviewArrow direction="previous" disabled={previewLoading || previewSwitching} onClick={() => void stepPreview(-1)} /> : null}
            {previewIndex >= 0 && previewIndex < previewableAttachments.length - 1 ? <PreviewArrow direction="next" disabled={previewLoading || previewSwitching} onClick={() => void stepPreview(1)} /> : null}
          </div>
        </div>
      ) : null}
    </>
  );
}
