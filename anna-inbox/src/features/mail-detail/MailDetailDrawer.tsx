import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useApp } from "../../app/AppContext";
import type {
  AiMailContextRef,
  AttachmentDownloadPayload,
  ComposeContact,
  ComposeDraft,
  DraftReplyArtifact,
  InboxMessage,
  InboxMessageDisplayBodyPayload,
  InboxThreadAssistPayload,
  InboxThreadMessage,
  InboxThreadPagePayload,
  InboxThreadStateOperation,
  MailAttachmentMeta,
  OutgoingAttachmentMeta,
  SubmitMailPromptRequest,
} from "../../types/mail";
import { RecipientChipInput } from "../../shared/RecipientChipInput";
import { SafeEmailHtml, SafeEmailText } from "../../shared/SafeEmailHtml";
import {
  getCachedMessageBody,
  getCachedThreadPage,
  setCachedMessageBody,
  setCachedThreadPage,
} from "../../shared/browserStorage";
import { mailAvatarFallback } from "../../shared/mailIdentity";
import { OutgoingAttachButton, OutgoingAttachmentList } from "../../shared/OutgoingAttachmentBar";
import {
  isImageOutgoingAttachment,
  putFileToUploadUrl,
  toPersistedOutgoingAttachments,
  totalOutgoingAttachmentBytes,
  validateOutgoingAttachment,
} from "../../shared/outgoingAttachments";
import { PdfAttachmentPreview } from "./PdfAttachmentPreview";
import { RichTextEditor, plainTextToEditorHtml, type RichTextValue } from "./RichTextEditor";
import { useI18n } from "../../i18n/I18nContext";
import {
  buildForwardDraftBody,
  buildForwardSendBodies,
  buildForwardSubject,
  buildQuickReplyPrompt,
  deriveReplyAllRecipients,
  deriveReplyToAddress,
  extractForwardNoteHtml,
  estimateAttachmentPreviewMemory,
  attachmentFallbackMetadata,
  formatAbsoluteDateTime,
  formatAttachmentSize,
  isOutboundMessageForMailbox,
  isPreviewableAttachment,
  materializeAttachmentAccess,
  matchesDraftArtifact,
  mergeDraftArtifactBody,
  normalizeAttachmentKind,
  resolveAttachmentAccess,
  resolveMessageThreadId,
  senderParts,
  shouldFollowLatestThreadMessage,
  stripQuotedReplyForDisplay,
  splitAddresses,
  stripForwardedMessageBlock,
  triggerAttachmentDownload,
  type ResolvedAttachmentAccess,
} from "./mailDetailHelpers";

type ComposerMode = "reply" | "forward";
type ReplyMode = "reply_to_sender" | "reply_all";

type ComposerDraftState = {
  id: string;
  body: string;
  bodyHtml: string;
  dirty: boolean;
  etag: string;
  recipients: string[];
  cc: string[];
  bcc: string[];
  ccInput: string;
  bccInput: string;
  ccOpen: boolean;
  bccOpen: boolean;
  focusField: "cc" | "bcc" | null;
  attachments: OutgoingAttachmentMeta[];
};

type ComposerDraftPatch =
  | Partial<ComposerDraftState>
  | ((current: ComposerDraftState) => ComposerDraftState);

function emptyComposerDraft(): ComposerDraftState {
  return {
    id: "",
    body: "",
    bodyHtml: "",
    dirty: false,
    etag: "",
    recipients: [],
    cc: [],
    bcc: [],
    ccInput: "",
    bccInput: "",
    ccOpen: false,
    bccOpen: false,
    focusField: null,
    attachments: [],
  };
}

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
  const { t } = useI18n();
  return (
    <button
      className="attachment-download-button"
      type="button"
      aria-label={t("detail.download")}
      data-tooltip={disabled ? t("detail.preparingDownload") : t("detail.download")}
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
  const { t } = useI18n();
  const previous = direction === "previous";
  const label = previous ? t("detail.previousAttachment") : t("detail.nextAttachment");
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
  const { t } = useI18n();
  return (
    <div className="mail-detail-loading-skeleton" role="status" aria-label={t("detail.loadingThread")}>
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
  const { locale, t } = useI18n();
  return (
    <div className="mail-thread-message-loading" role="status" aria-label={t("detail.loadingMessage")}>
      <div className="mail-detail-loading-body">
        <span className="mail-detail-loading-line" />
        <span className="mail-detail-loading-line" />
        <span className="mail-detail-loading-line is-short" />
      </div>
      <p>{t("detail.loadingMessage")}</p>
    </div>
  );
}

function AttachmentSection({
  attachments,
  onPreview,
  onDownload,
  isDownloading,
}: {
  attachments: MailAttachmentMeta[];
  onPreview: (attachment: MailAttachmentMeta) => void;
  onDownload: (attachment: MailAttachmentMeta) => void;
  isDownloading: (attachment: MailAttachmentMeta) => boolean;
}) {
  const { t } = useI18n();
  if (!attachments.length) return null;
  const fallbackLabel = (category: string): string => {
    switch (category) {
      case "office": return t("attach.office");
      case "calendar": return t("attach.calendar");
      case "archive": return t("attach.archive");
      default: return t("attach.download");
    }
  };
  return (
    <div className="mail-detail-attachments">
      {attachments.map((attachment) => {
        const previewable = isPreviewableAttachment(attachment);
        const fallback = attachmentFallbackMetadata(attachment);
        return (
          <article key={attachment.id} className={`mail-detail-attachment${previewable ? " is-previewable" : ""}`}>
            {previewable ? (
              <button
                className="attachment-preview-hitarea"
                type="button"
                aria-label={t("detail.previewAttachment", { filename: attachment.filename })}
                onClick={() => onPreview(attachment)}
              />
            ) : null}
            <div>
              <strong>{attachment.filename}</strong>
              <span>
                {[fallbackLabel(fallback.category), attachment.mime_type !== "application/octet-stream" ? attachment.mime_type : "", attachment.size ? formatAttachmentSize(attachment.size) : ""]
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

function displayAddress(parts: { name: string; email: string } | null, unknown = "Unknown") {
  if (!parts) return { title: unknown, subtitle: "" };
  return {
    title: parts.name && parts.name !== parts.email ? parts.name : parts.email || unknown,
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

function HeaderContactRow({
  label,
  address,
  avatarUrl,
}: {
  label: string;
  address: { name: string; email: string } | null;
  avatarUrl?: string;
}) {
  const { t } = useI18n();
  const unknown = t("mail.unknown");
  const display = displayAddress(address, unknown);
  const sender = address?.name || address?.email || unknown;
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
  const { t } = useI18n();
  const unknown = t("mail.unknown");
  if (!addresses.length) return null;
  return (
    <div className="mail-detail-contact-row mail-detail-contact-row--recipients">
      <span className="mail-detail-contact-label">{label}:</span>
      <div className="mail-detail-contact-recipient-list">
        {addresses.map((address, index) => {
          const display = displayAddress(address, unknown);
          const sender = address.name || address.email || unknown;
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

/** 距底部多少像素内视为仍贴底，用于用户上滑后停止自动跟随。 */
const THREAD_BOTTOM_STICK_THRESHOLD_PX = 48;

function isThreadScrollerNearBottom(container: HTMLDivElement, thresholdPx = THREAD_BOTTOM_STICK_THRESHOLD_PX) {
  return container.scrollHeight - container.clientHeight - container.scrollTop <= thresholdPx;
}

function scrollToThreadBottom(container: HTMLDivElement | null, behavior: ScrollBehavior = "smooth") {
  if (!container) return;
  const bottom = Math.max(0, container.scrollHeight - container.clientHeight);
  if (behavior === "smooth") {
    container.scrollTo({ top: bottom, behavior });
    return;
  }
  // 直接写入 scrollTop，兼容嵌入式 WebView 在 layout 尚未稳定时的 scrollTo 时序。
  container.scrollTop = bottom;
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

function MailOverviewAction({ text, onClick, label }: { text: string; onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      className="mail-detail-overview-result"
      data-tooltip={label}
      aria-label={label}
      onClick={onClick}
    >
      <AiSparkleIcon />
      <span>{text}</span>
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
const ReplyModeIcon = () => <ToolbarIcon><path d="M9 14 4 9l5-5" /><path d="M20 20v-7a4 4 0 0 0-4-4H4" /></ToolbarIcon>;
const ReplyAllModeIcon = () => <ToolbarIcon><path d="m8 14-5-5 5-5" /><path d="M20 20v-7a4 4 0 0 0-4-4H3" /><path d="m13 14-5-5 5-5" /><path d="M20 20v-3a4 4 0 0 0-4-4h-3" /></ToolbarIcon>;
const ForwardModeIcon = () => <ToolbarIcon><path d="m15 14 5-5-5-5" /><path d="M4 20v-7a4 4 0 0 1 4-4h12" /></ToolbarIcon>;
const ModeChevronIcon = () => <ToolbarIcon><path d="m6 9 6 6 6-6" /></ToolbarIcon>;

export function MailDetailDrawer({
  open,
  mailbox,
  message,
  flags,
  aiBusy,
  insertRequest,
  onConsumeInsertRequest,
  onClose,
  onRequestClose,
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
  onScheduleReply,
  onScheduleForward,
  onSaveForwardDraft,
  replyDraftRestore,
  onConsumeReplyDraftRestore,
  contactAvatars,
  loadContactAvatars,
  searchComposeContacts,
  latestThreadMessageId = "",
  autoOpenDraftComposer = false,
}: {
  open: boolean;
  mailbox: string;
  message: InboxMessage | null;
  flags: MailUiFlags;
  aiBusy: boolean;
  insertRequest: InsertRequest;
  onConsumeInsertRequest: (nonce: string) => void;
  onClose: () => void;
  onRequestClose: () => void;
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
  loadInboxThreadAssist: (mailbox: string, threadId: string, latestMessageId: string, anchorMessageId?: string, locale?: string) => Promise<InboxThreadAssistPayload>;
  getInboxThreadDraft: (mailbox: string, threadId: string) => Promise<{
    exists: boolean;
    body: string;
    body_html?: string;
    etag?: string;
    attachments?: OutgoingAttachmentMeta[];
  }>;
  saveInboxThreadDraft: (
    mailbox: string,
    threadId: string,
    body: string,
    bodyHtml?: string,
    ifMatch?: string,
    message?: Record<string, unknown>,
    attachments?: Array<Record<string, unknown>>,
  ) => Promise<{ etag?: string }>;
  deleteInboxThreadDraft: (mailbox: string, threadId: string) => Promise<{ ok?: boolean }>;
  prepareInboxAttachmentAccess: (mailbox: string, messageId: string, attachmentId: string, mode: "preview" | "download") => Promise<AttachmentDownloadPayload>;
  submitMailContextPrompt: (request: SubmitMailPromptRequest) => Promise<unknown>;
  onScheduleReply: (args: {
    mailbox: string;
    threadId: string;
    to: string;
    body: string;
    bodyHtml?: string;
    cc?: string[];
    bcc?: string[];
    replyMode?: ReplyMode;
    message: InboxMessage;
    attachments?: OutgoingAttachmentMeta[];
  }) => boolean;
  onScheduleForward: (args: {
    mailbox: string;
    recipients: string[];
    cc?: string[];
    bcc?: string[];
    subject: string;
    body: string;
    body_html?: string;
    message: InboxMessage;
    attachments?: OutgoingAttachmentMeta[];
  }) => boolean;
  onSaveForwardDraft: (args: {
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
    attachments?: OutgoingAttachmentMeta[];
  }) => Promise<ComposeDraft>;
  replyDraftRestore: {
    nonce: string;
    threadId: string;
    body: string;
    bodyHtml?: string;
    cc?: string[];
    bcc?: string[];
    replyMode?: ReplyMode;
    mode?: ComposerMode;
    recipients?: string[];
    composeDraftId?: string;
    composeDraftEtag?: string;
  } | null;
  onConsumeReplyDraftRestore: (nonce: string) => void;
  contactAvatars?: Record<string, string>;
  loadContactAvatars: (emails: string[], mailbox?: string) => Promise<{ avatars: Record<string, string>; permissionRequired: boolean }>;
  searchComposeContacts: (
    mailbox: string,
    query: string,
  ) => Promise<{ contacts: ComposeContact[] }>;
  latestThreadMessageId?: string;
  /** 仅从 Drafts 分类进入详情时为 true：默认展开编辑区并加载已存草稿 */
  autoOpenDraftComposer?: boolean;
}) {
  const { locale, t } = useI18n();
  const [page, setPage] = useState<InboxThreadPagePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [assist, setAssist] = useState<InboxThreadAssistPayload | null>(null);
  const [assistLoading, setAssistLoading] = useState(false);
  const [assistError, setAssistError] = useState("");
  const [composerOpen, setComposerOpen] = useState(false);
  const [composerClosing, setComposerClosing] = useState(false);
  const [composerExpanded, setComposerExpanded] = useState(false);
  const [composerMode, setComposerMode] = useState<ComposerMode>("reply");
  const [replyMode, setReplyMode] = useState<ReplyMode>("reply_to_sender");
  const { actions } = useApp();
  const [modeMenuOpen, setModeMenuOpen] = useState(false);
  const [composerDrafts, setComposerDrafts] = useState<Record<ComposerMode, ComposerDraftState>>(() => ({
    reply: emptyComposerDraft(),
    forward: emptyComposerDraft(),
  }));
  const [sending, setSending] = useState(false);
  const modeMenuRef = useRef<HTMLDivElement | null>(null);
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
  /** 初始滚动已开始，防止当前详情页重复定位。 */
  const initialBottomScrollStartedRef = useRef(false);
  /** 初始平滑滚动已抵达底部；此前不允许布局补偿改写 scrollTop。 */
  const initialBottomScrollDoneRef = useRef(false);
  const preserveScrollOnPageUpdateRef = useRef(false);
  const detailSessionRef = useRef(0);
  const historicalBodyHydrationRef = useRef<Promise<void> | null>(null);
  const footerRef = useRef<HTMLElement | null>(null);
  const composerRef = useRef<HTMLDivElement | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);
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
  const draftLoadKeyRef = useRef("");
  const composerContextHeightRef = useRef<number | null>(null);
  const draftLoadSequenceRef = useRef(0);
  const suppressDraftLoadForRef = useRef("");
  const draftEditSequenceRef = useRef(0);
  const pendingDraftSavesRef = useRef(new Set<Promise<void>>());
  const composerCloseTimerRef = useRef<number | null>(null);
  const closeComposerRef = useRef<() => Promise<void>>(async () => undefined);
  const aiDraftModeRef = useRef<ComposerMode>("reply");

  const updateComposerDraft = (mode: ComposerMode, patch: ComposerDraftPatch) => {
    setComposerDrafts((current) => {
      const previous = current[mode];
      const next = typeof patch === "function" ? patch(previous) : { ...previous, ...patch };
      return { ...current, [mode]: next };
    });
  };

  const updateActiveComposerDraft = (patch: ComposerDraftPatch) => updateComposerDraft(composerMode, patch);
  const activeComposerDraft = composerDrafts[composerMode];
  const draft = activeComposerDraft.body;
  const draftHtml = activeComposerDraft.bodyHtml;
  const draftDirty = activeComposerDraft.dirty;
  const draftEtag = activeComposerDraft.etag;
  const forwardTo = activeComposerDraft.recipients;
  const replyCc = activeComposerDraft.cc;
  const replyBcc = activeComposerDraft.bcc;
  const replyCcInput = activeComposerDraft.ccInput;
  const replyBccInput = activeComposerDraft.bccInput;
  const replyCcOpen = activeComposerDraft.ccOpen;
  const replyBccOpen = activeComposerDraft.bccOpen;
  const replyFocusField = activeComposerDraft.focusField;
  const composerAttachments = activeComposerDraft.attachments;
  const setComposerAttachments = (attachments: OutgoingAttachmentMeta[] | ((current: OutgoingAttachmentMeta[]) => OutgoingAttachmentMeta[])) => {
    updateActiveComposerDraft((current) => ({
      ...current,
      attachments: typeof attachments === "function" ? attachments(current.attachments) : attachments,
      dirty: true,
    }));
  };
  const setDraft = (body: string | ((current: string) => string)) => {
    updateActiveComposerDraft((current) => ({
      ...current,
      body: typeof body === "function" ? body(current.body) : body,
    }));
  };
  const setDraftContent = ({ html, text }: RichTextValue) => {
    updateActiveComposerDraft((current) => ({ ...current, body: text, bodyHtml: html }));
  };
  const setDraftDirty = (dirty: boolean) => updateActiveComposerDraft({ dirty });
  const setDraftEtag = (etag: string) => updateActiveComposerDraft({ etag });
  const setForwardTo = (recipients: string[]) => updateActiveComposerDraft({ recipients, dirty: true });
  const setReplyCc = (cc: string[]) => updateActiveComposerDraft({ cc, dirty: true });
  const setReplyBcc = (bcc: string[]) => updateActiveComposerDraft({ bcc, dirty: true });
  const setReplyCcInput = (ccInput: string) => updateActiveComposerDraft({ ccInput });
  const setReplyBccInput = (bccInput: string) => updateActiveComposerDraft({ bccInput });
  const setReplyCcOpen = (ccOpen: boolean) => updateActiveComposerDraft({ ccOpen });
  const setReplyBccOpen = (bccOpen: boolean) => updateActiveComposerDraft({ bccOpen });
  const setReplyFocusField = (focusField: "cc" | "bcc" | null) => updateActiveComposerDraft({ focusField });

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

  const clearEmptyForwardDraft = () => {
    if (stripForwardedMessageBlock(composerDrafts.forward.body).trim()) return;
    setComposerDrafts((current) => ({ ...current, forward: emptyComposerDraft() }));
  };

  const saveForwardDraftIfNeeded = async () => {
    const forwardDraft = composerDrafts.forward;
    const forwardNote = stripForwardedMessageBlock(forwardDraft.body).trim();
    if ((!forwardNote && !forwardDraft.attachments.length) || !threadId || !message?.id) return;
    return onSaveForwardDraft({
      id: forwardDraft.id || undefined,
      ifMatch: forwardDraft.etag || undefined,
      mailbox,
      recipients: forwardDraft.recipients,
      cc: forwardDraft.cc,
      bcc: forwardDraft.bcc,
      subject: buildForwardSubject(latestMessage?.subject || message.subject || page?.subject),
      body: forwardDraft.body,
      bodyHtml: buildForwardSendBodies(latestMessage, forwardDraft.body, forwardDraft.bodyHtml).body_html,
      sourceThreadId: threadId,
      sourceMessageId: message.id,
      attachments: toPersistedOutgoingAttachments(forwardDraft.attachments),
    });
  };

  const closeComposer = async () => {
    if (!composerOpen) return;
    if (composerMode === "forward") {
      try {
        await saveForwardDraftIfNeeded();
      } catch (reason) {
        showToast(reason instanceof Error ? reason.message : String(reason));
        return;
      }
    }
    clearEmptyForwardDraft();
    setComposerDrafts((current) => ({
      reply: { ...current.reply, ccOpen: false, bccOpen: false, ccInput: "", bccInput: "", focusField: null },
      forward: { ...current.forward, ccOpen: false, bccOpen: false, ccInput: "", bccInput: "", focusField: null },
    }));
    setComposerExpanded(false);
    setModeMenuOpen(false);
    setComposerOpen(false);
    setComposerClosing(true);
    if (composerCloseTimerRef.current) window.clearTimeout(composerCloseTimerRef.current);
    composerCloseTimerRef.current = window.setTimeout(() => {
      setComposerClosing(false);
      composerCloseTimerRef.current = null;
    }, COMPOSER_TRANSITION_MS);
  };
  closeComposerRef.current = closeComposer;

  const threadId = resolveMessageThreadId(message);
  const messageId = message?.id || "";
  const draftStorageKey = `${mailbox.trim().toLowerCase()}:${threadId}`;
  const context = useMemo(() => (message ? buildMailContext(mailbox, message, page) : null), [mailbox, message, page]);
  const visibleThreadMessages = useMemo(
    () => page?.messages || [],
    [page?.messages],
  );
  const latestMessage = visibleThreadMessages[visibleThreadMessages.length - 1] || page?.messages[page.messages.length - 1];
  const replyToAddress = deriveReplyToAddress(latestMessage, mailbox);
  const replyAllRecipients = deriveReplyAllRecipients(latestMessage, mailbox);
  const canReplyAll = replyAllRecipients.cc.length > 0;
  const effectiveReplyToAddress = replyMode === "reply_all"
    ? replyAllRecipients.to
    : replyToAddress;
  const effectiveReplyCc = replyMode === "reply_all"
    ? [...replyAllRecipients.cc, ...replyCc].filter((email, index, values) =>
      values.findIndex((candidate) => candidate.toLowerCase() === email.toLowerCase()) === index,
    )
    : replyCc;
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
    // 仅在需要回复时展示 draft 快捷提示；无需回复时由后端清空 quick_replies
    if (assist?.needs_reply === false) return [];
    const generated = (assist?.quick_replies || []).filter((item) => item.label && item.intent);
    const localizedLabels: Record<string, string> = {
      "ready_to_start": t("detail.quickReply.readyToStart"),
      "reviewing_with_team": t("detail.quickReply.reviewingWithTeam"),
      "provide_status_update": t("detail.quickReply.provideStatusUpdate"),
      "request_more_time": t("detail.quickReply.requestMoreTime"),
      "ready to start": t("detail.quickReply.readyToStart"),
      "reviewing with team": t("detail.quickReply.reviewingWithTeam"),
      "provide status update": t("detail.quickReply.provideStatusUpdate"),
      "request more time": t("detail.quickReply.requestMoreTime"),
    };
    return generated.map((item) => ({
      ...item,
      label: localizedLabels[String(item.id || "").toLowerCase()] || localizedLabels[String(item.label || "").toLowerCase()] || item.label,
    }));
  }, [assist?.needs_reply, assist?.quick_replies, t]);
  const noReplyReason = String(assist?.no_reply_reason || "").trim();
  const showNoReplyNotice = Boolean(
    context
    && assist
    && !assistLoading
    && assist.needs_reply === false
  );
  const showQuickReplies = Boolean(
    context
    && assist?.needs_reply !== false
    && quickReplySuggestions.length
    && replyPromptTarget
  );
  const quickReplyRenderKey = quickReplySuggestions
    .map((item) => `${item.id}:${item.label}:${item.intent}`)
    .join("|");
  const assistFooterKey = showNoReplyNotice
    ? `no-reply:${noReplyReason}`
    : quickReplyRenderKey;
  const fromAddress = useMemo(() => firstAddress(message?.from || undefined), [message?.from]);
  const toAddresses = useMemo(() => splitAddresses(message?.to).map(senderParts), [message?.to]);
  const fromAvatarUrl = fromAddress ? (resolvedAvatars[fromAddress.email.toLowerCase()] || contactAvatars?.[fromAddress.email.toLowerCase()]) : undefined;
  const recipientAvatarUrls = useMemo(() => {
    const avatars = { ...contactAvatars, ...resolvedAvatars };
    return Object.fromEntries(toAddresses.map((address) => [address.email.toLowerCase(), avatars[address.email.toLowerCase()]]));
  }, [contactAvatars, resolvedAvatars, toAddresses]);
  const displaySubject = page?.subject || message?.subject || t("mail.noSubject");
  const latestSubject = page?.latest_subject || message?.latest_subject || "";
  const subjectWasModified = Boolean(
    latestSubject
    && displaySubject
    && normalizeComparableSubject(latestSubject) !== normalizeComparableSubject(displaySubject)
  );

  useEffect(() => {
    if (open) return;
    detailSessionRef.current += 1;
    const scroller = scrollRef.current;
    if (scroller) {
      scroller.scrollTo({ top: scroller.scrollTop, behavior: "auto" });
    }
  }, [open]);

  useLayoutEffect(() => {
    if (!open || loading || !visibleThreadMessages.length || preserveScrollOnPageUpdateRef.current) return;
    if (initialBottomScrollStartedRef.current) return;
    const scroller = scrollRef.current;
    if (!scroller) return;
    const session = detailSessionRef.current;

    let cancelled = false;
    let scrollCompletionFrame = 0;
    scrollToThreadBottom(scroller, "smooth");
    initialBottomScrollStartedRef.current = true;

    const completeWhenPositioned = () => {
      if (cancelled || session !== detailSessionRef.current || !open) return;
      if (isThreadScrollerNearBottom(scroller, 2)) {
        initialBottomScrollDoneRef.current = true;
        return;
      }
      scrollCompletionFrame = window.requestAnimationFrame(completeWhenPositioned);
    };
    scrollCompletionFrame = window.requestAnimationFrame(completeWhenPositioned);

    return () => {
      cancelled = true;
      window.cancelAnimationFrame(scrollCompletionFrame);
    };
  }, [loading, open, visibleThreadMessages.length]);

  // quick reply / no-reply 条出现在线程下方：仅当用户仍在底部附近时补一次 smooth，不打断阅读。
  useEffect(() => {
    if (!open || (!showQuickReplies && !showNoReplyNotice)) return;
    if (!initialBottomScrollDoneRef.current || preserveScrollOnPageUpdateRef.current) return;
    const scroller = scrollRef.current;
    if (!scroller || !isThreadScrollerNearBottom(scroller)) return;
    scrollToThreadBottom(scroller, "smooth");
  }, [assistFooterKey, open, showNoReplyNotice, showQuickReplies]);

  // footer 展开/收缩会改变 context 高度；在动画帧内持续补偿 scrollTop，让正文随 footer 上下移
  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller || !open) {
      composerContextHeightRef.current = null;
      return;
    }
    let previousHeight = scroller.clientHeight;
    composerContextHeightRef.current = previousHeight;
    let timer = 0;
    const observer = new ResizeObserver(() => {
      window.clearTimeout(timer);
      // setTimeout 完全跳出 RO 投递周期，比 rAF 更能避免 loop 通知。
      timer = window.setTimeout(() => {
        const currentHeight = scroller.clientHeight;
        // 初始平滑滚动尚未完成时，任何 footer/header 尺寸变化都只能更新基线，
        // 不能按旧 scrollTop 补偿，否则会把滚动动画拉回顶部。
        if (!initialBottomScrollDoneRef.current) {
          previousHeight = currentHeight;
          composerContextHeightRef.current = currentHeight;
          return;
        }
        const heightDelta = previousHeight - currentHeight;
        const wasAtBottom = isThreadScrollerNearBottom(scroller, 4);
        if (wasAtBottom) {
          scroller.scrollTop = Math.max(0, scroller.scrollHeight - currentHeight);
        } else if (heightDelta) {
          scroller.scrollTop += heightDelta;
        }
        previousHeight = currentHeight;
        composerContextHeightRef.current = currentHeight;
      }, 0);
    });
    observer.observe(scroller);
    return () => {
      window.clearTimeout(timer);
      observer.disconnect();
    };
  }, [messageId, open, threadId]);

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

  // 导航邮件/线程时跳过一次 soft refresh，避免与完整加载双发请求
  const skipSoftThreadRefreshRef = useRef(true);
  useEffect(() => {
    skipSoftThreadRefreshRef.current = true;
  }, [autoOpenDraftComposer, mailbox, messageId, open, threadId]);

  useEffect(() => {
    if (!open || !message || !mailbox || !messageId) return;
    const session = ++detailSessionRef.current;
    let cancelled = false;
    const anchorMessage = message;
    preserveScrollOnPageUpdateRef.current = false;
    initialBottomScrollStartedRef.current = false;
    initialBottomScrollDoneRef.current = false;
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
    // 默认收起编辑区；仅 Drafts 分类进入时默认展开
    setComposerOpen(Boolean(autoOpenDraftComposer));
    setComposerClosing(false);
    setComposerExpanded(false);
    setComposerMode("reply");
    setModeMenuOpen(false);
    setComposerDrafts({ reply: emptyComposerDraft(), forward: emptyComposerDraft() });
    draftLoadKeyRef.current = "";
    composerContextHeightRef.current = null;
    suppressDraftLoadForRef.current = "";
    // 仅切换邮件时清附件预览；同线程后台同步刷新不得打断预览
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
      const requestKey = `${mailbox}:${threadId}:${latestMessageId}:${messageId}:${locale}`;
      if (assistRequestKeyRef.current === requestKey) return;
      assistRequestKeyRef.current = requestKey;
      setAssistError("");
      setAssistLoading(true);
      void loadInboxThreadAssistRef.current(mailbox, threadId, latestMessageId, messageId, locale)
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
      // 选中邮件若被截断则立即经 body_url 加载完整正文，其余截断消息由下方
      // “历史正文补全”逻辑逐封串行补齐。THREAD_REF 初始只有 thread id 时，
      // 回退到线程最新邮件。仍只并发加载一封，避免打开长线程时并发请求全部历史正文。
      const anchorItem = visiblePage.messages.find((item) => item.id === messageId)
        || visiblePage.messages.find((item) => item.id === visiblePage.latest_message_id)
        || visiblePage.messages.at(-1);
      if (anchorItem?.body_truncated) void loadFullDisplayBodyRef.current(anchorItem, true);
    };
    const load = async () => {
      try {
        if (threadId) {
          const cachedPagePromise = getCachedThreadPage(mailbox, threadId, latestThreadMessageId || messageId)
            .catch(() => null);
          const applyThreadPage = (nextPage: InboxThreadPagePayload) => {
            const visiblePage = withoutGmailDraftThreadMessages(nextPage);
            setPage(visiblePage);
            void cacheThreadPage(mailbox, visiblePage);
            loadFullAnchorMessage(visiblePage);
            requestAssist(visiblePage.latest_message_id || messageId);
          };
          const refreshNetworkPage = async (previousPage?: InboxThreadPagePayload) => {
            const scroller = scrollRef.current;
            const nextPage = await loadInboxThreadPageRef.current(mailbox, threadId, {
              anchorMessageId: messageId,
              limit: 5,
              includeDisplayBody: true,
            });
            if (cancelled || session !== detailSessionRef.current) return;
            const visiblePage = withoutGmailDraftThreadMessages(nextPage);
            const shouldFollowRefresh = shouldFollowLatestThreadMessage(
              previousPage?.latest_message_id || "",
              visiblePage.latest_message_id || "",
              Boolean(scroller && isThreadScrollerNearBottom(scroller)),
              preserveScrollOnPageUpdateRef.current,
            );
            applyThreadPage(nextPage);
            if (shouldFollowRefresh && scroller) {
              window.requestAnimationFrame(() => {
                if (cancelled || session !== detailSessionRef.current || !open) return;
                scrollToThreadBottom(scroller, "smooth");
                initialBottomScrollDoneRef.current = true;
              });
            }
          };
          const cachedPage = await cachedPagePromise;
          if (cancelled || session !== detailSessionRef.current) return;
          if (cachedPage) {
            const visibleCachedPage = withoutGmailDraftThreadMessages(cachedPage);
            applyThreadPage(visibleCachedPage);
            setLoading(false);
            void refreshNetworkPage(visibleCachedPage).catch(() => undefined);
            return;
          }
          await refreshNetworkPage();
        } else {
          const cachedBody = await getCachedMessageBody(mailbox, messageId, anchorMessage.internal_date);
          if (cancelled || session !== detailSessionRef.current) return;
          if (cachedBody) {
            setPage(singleMessagePageFromBody(mailbox, anchorMessage, cachedBody));
            return;
          }
          const body = await loadInboxEmailBodyRef.current(messageId, mailbox);
          if (cancelled || session !== detailSessionRef.current) return;
          const nextPage = singleMessagePageFromBody(mailbox, anchorMessage, { body_text: body, attachments: inboxMessageAttachments(anchorMessage) });
          setPage(nextPage);
          void setCachedMessageBody(mailbox, anchorMessage, { body_text: body, attachments: inboxMessageAttachments(anchorMessage) });
        }
      } catch (reason) {
        if (cancelled || session !== detailSessionRef.current) return;
        const threadError = reason instanceof Error ? reason.message : String(reason);
        if (threadId) {
          try {
            const display = await resolveDisplayBodyPayload(await loadInboxMessageDisplayBodyRef.current(mailbox, messageId));
            if (cancelled || session !== detailSessionRef.current) return;
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
        if (!cancelled && session === detailSessionRef.current) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (session === detailSessionRef.current) detailSessionRef.current += 1;
    };
  // A saved local draft overlays the selected inbox message with a new object. That
  // is not a navigation event, so avoid using the whole message as a dependency:
  // doing so would reload the thread and reset the composer after an AI insertion.
  // latestThreadMessageId 变更走下方 soft refresh，避免清附件预览。
  }, [autoOpenDraftComposer, mailbox, messageId, open, threadId]);

  // 同线程最新消息变化（同步/新邮件）：静默刷新线程页，不关附件预览、不重置编辑区
  useEffect(() => {
    if (!open || !mailbox || !messageId || !threadId || !latestThreadMessageId) return;
    if (skipSoftThreadRefreshRef.current) {
      skipSoftThreadRefreshRef.current = false;
      return;
    }
    const session = detailSessionRef.current;
    let cancelled = false;
    const softRefresh = async () => {
      try {
        const scroller = scrollRef.current;
        const nextPage = await loadInboxThreadPageRef.current(mailbox, threadId, {
          anchorMessageId: messageId,
          limit: 5,
          includeDisplayBody: true,
        });
        if (cancelled || session !== detailSessionRef.current || !open) return;
        const visiblePage = withoutGmailDraftThreadMessages(nextPage);
        const shouldFollowRefresh = shouldFollowLatestThreadMessage(
          page?.latest_message_id || "",
          visiblePage.latest_message_id || "",
          Boolean(scroller && isThreadScrollerNearBottom(scroller)),
          preserveScrollOnPageUpdateRef.current,
        );
        setPage(visiblePage);
        void cacheThreadPage(mailbox, visiblePage);
        if (shouldFollowRefresh && scroller) {
          window.requestAnimationFrame(() => {
            if (cancelled || session !== detailSessionRef.current || !open) return;
            scrollToThreadBottom(scroller, "smooth");
            initialBottomScrollDoneRef.current = true;
          });
        }
        const requestKey = `${mailbox}:${threadId}:${visiblePage.latest_message_id || latestThreadMessageId}:${messageId}:${locale}`;
        if (assistRequestKeyRef.current !== requestKey) {
          assistRequestKeyRef.current = requestKey;
          setAssistError("");
          setAssistLoading(true);
          void loadInboxThreadAssistRef.current(
            mailbox,
            threadId,
            visiblePage.latest_message_id || latestThreadMessageId,
            messageId,
            locale,
          )
            .then((result) => {
              if (!cancelled && assistRequestKeyRef.current === requestKey) setAssist(result);
            })
            .catch((reason) => {
              if (!cancelled && assistRequestKeyRef.current === requestKey) {
                setAssistError(reason instanceof Error ? reason.message : String(reason));
              }
            })
            .finally(() => {
              if (!cancelled && assistRequestKeyRef.current === requestKey) setAssistLoading(false);
            });
        }
      } catch {
        // soft refresh 失败保留当前页，不打断附件预览
      }
    };
    void softRefresh();
    return () => {
      cancelled = true;
    };
  }, [latestThreadMessageId, locale, mailbox, messageId, open, threadId]);

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
    // 默认不加载/展开；仅 Drafts 进入时自动展开，或用户手动打开编辑区后回填草稿。
    if ((!composerOpen && !autoOpenDraftComposer) || !threadId || !mailbox) return;
    const loadKey = `${draftStorageKey}:${autoOpenDraftComposer ? "auto" : "manual"}`;
    if (autoOpenDraftComposer && draftLoadKeyRef.current === loadKey) return;
    if (autoOpenDraftComposer) draftLoadKeyRef.current = loadKey;
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
        const storedAttachments = (stored.attachments || []).map((item) => ({
          ...item,
          status: "ready" as const,
        }));
        if (nextDraft || storedAttachments.length) {
          if (autoOpenDraftComposer) setComposerOpen(true);
          updateComposerDraft("reply", {
            body: nextDraft,
            bodyHtml: stored.body_html || plainTextToEditorHtml(nextDraft),
            etag: stored.etag || "",
            attachments: storedAttachments,
          });
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [autoOpenDraftComposer, composerOpen, draftStorageKey, mailbox, message?.draft_body, threadId]);

  useEffect(() => {
    if (!open || !replyDraftRestore || replyDraftRestore.threadId !== threadId) return;
    draftLoadSequenceRef.current += 1;
    draftEditSequenceRef.current += 1;
    setComposerClosing(false);
    setComposerOpen(true);
    const restoreMode = replyDraftRestore.mode || "reply";
    setComposerMode(restoreMode);
    setReplyMode(replyDraftRestore.replyMode || "reply_to_sender");
    updateComposerDraft(restoreMode, (current) => ({
      ...current,
      body: replyDraftRestore.body,
      bodyHtml: restoreMode === "forward"
        ? (extractForwardNoteHtml(replyDraftRestore.bodyHtml) || plainTextToEditorHtml(stripForwardedMessageBlock(replyDraftRestore.body)))
        : (replyDraftRestore.bodyHtml || plainTextToEditorHtml(replyDraftRestore.body)),
      id: restoreMode === "forward" ? (replyDraftRestore.composeDraftId || "") : current.id,
      etag: restoreMode === "forward" ? (replyDraftRestore.composeDraftEtag || "") : current.etag,
      dirty: true,
      recipients: restoreMode === "forward" ? (replyDraftRestore.recipients || []) : current.recipients,
      cc: replyDraftRestore.cc || [],
      bcc: replyDraftRestore.bcc || [],
      ccOpen: Boolean(replyDraftRestore.cc?.length),
      bccOpen: Boolean(replyDraftRestore.bcc?.length),
    }));
    onConsumeReplyDraftRestore(replyDraftRestore.nonce);
  }, [onConsumeReplyDraftRestore, open, replyDraftRestore, threadId]);

  useEffect(() => {
    if (!modeMenuOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (modeMenuRef.current?.contains(event.target as Node)) return;
      setModeMenuOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [modeMenuOpen]);

  useEffect(() => {
    if (!composerOpen || !threadId || !mailbox || !draftDirty) return;
    const editSequence = draftEditSequenceRef.current;
    const timer = window.setTimeout(() => {
      const persistDraft = async () => {
        try {
          if (composerMode === "forward") {
            const saved = await saveForwardDraftIfNeeded();
            if (!saved || editSequence !== draftEditSequenceRef.current) return;
            updateComposerDraft("forward", (current) => ({
              ...current,
              id: saved.id,
              etag: saved.etag || current.etag,
              dirty: false,
            }));
            return;
          }
          const result = await saveInboxThreadDraftRef.current(
            mailbox,
            threadId,
            draft,
            draftHtml,
            draftEtag || undefined,
            {
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
            },
            toPersistedOutgoingAttachments(composerAttachments),
          );
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
  }, [composerAttachments, composerMode, composerOpen, draft, draftDirty, draftEtag, draftHtml, latestMessage, mailbox, message, threadId]);

  useEffect(() => {
    if (!composerOpen) return;
    const collapseEmptyRecipientFields = () => {
      const ccEmpty = replyCcOpen && !replyCc.length && !replyCcInput.trim();
      const bccEmpty = replyBccOpen && !replyBcc.length && !replyBccInput.trim();
      if (ccEmpty) {
        setReplyCcOpen(false);
        setReplyCcInput("");
      }
      if (bccEmpty) {
        setReplyBccOpen(false);
        setReplyBccInput("");
      }
      if (ccEmpty || bccEmpty) setReplyFocusField(null);
    };
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Element | null;
      if (!composerRef.current?.contains(target)) return;
      if (
        target?.closest('[data-recipient-role="cc"], [data-recipient-role="bcc"]')
        || target?.closest(".compose-contact-menu")
        || target?.closest(".compose-cc-bcc-toggle")
      ) return;
      collapseEmptyRecipientFields();
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [composerOpen, replyBcc, replyBccInput, replyBccOpen, replyCc, replyCcInput, replyCcOpen]);

  // 编辑区展开时，点击编辑区外内容则收起
  useEffect(() => {
    if (!composerOpen) return;
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Element | null;
      if (!target) return;
      if (footerRef.current?.contains(target)) return;
      if (target.closest(".compose-contact-menu")) return;
      // 附件预览层内操作不收起编辑区
      if (target.closest(".attachment-preview-modal")) return;
      void closeComposerRef.current();
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [composerOpen]);

  useEffect(() => {
    if (!insertRequest || !message || !threadId) return;
    const { artifact, mode, nonce } = insertRequest;
    if (!matchesDraftArtifact(mailbox, threadId, artifact)) {
      onConsumeInsertRequest(nonce);
      showToast(t("toast.openThreadFirst"));
      return;
    }
    onConsumeInsertRequest(nonce);
    const targetMode = artifact.composer_mode || aiDraftModeRef.current;
    setComposerMode(targetMode);
    setModeMenuOpen(false);
    if (mode === "replace") {
      // Replace 是用户明确操作：直接覆盖编辑栏，不走 discard 或持久化草稿回填路径。
      if (targetMode === "reply") suppressDraftLoadForRef.current = draftStorageKey;
      draftLoadSequenceRef.current += 1;
      setComposerOpen(true);
      draftEditSequenceRef.current += 1;
      updateComposerDraft(targetMode, (current) => ({
        ...current,
        body: targetMode === "forward"
          ? buildForwardDraftBody(latestMessage || undefined, artifact.body)
          : artifact.body,
        bodyHtml: plainTextToEditorHtml(artifact.body),
        dirty: true,
        recipients: targetMode === "forward" && artifact.recipients?.length
          ? artifact.recipients
          : current.recipients,
      }));
      requestAnimationFrame(() => bodyRef.current?.focus());
      return;
    }
    // Append 仍保留编辑栏原文，并使已发起的旧草稿读取失效。
    draftLoadSequenceRef.current += 1;
    setComposerOpen(true);
    draftEditSequenceRef.current += 1;
    updateComposerDraft(targetMode, (current) => ({
      ...current,
        body: targetMode === "forward"
        ? buildForwardDraftBody(
            latestMessage || undefined,
            mergeDraftArtifactBody(stripForwardedMessageBlock(current.body), artifact.body, mode),
          )
          : mergeDraftArtifactBody(current.body, artifact.body, mode),
        bodyHtml: plainTextToEditorHtml(targetMode === "forward"
          ? mergeDraftArtifactBody(stripForwardedMessageBlock(current.body), artifact.body, mode)
          : mergeDraftArtifactBody(current.body, artifact.body, mode)),
        dirty: true,
        recipients: targetMode === "forward" && artifact.recipients?.length
          ? artifact.recipients
          : current.recipients,
      }));
    requestAnimationFrame(() => bodyRef.current?.focus());
  }, [composerDrafts.reply.body, draftStorageKey, insertRequest, mailbox, message, onConsumeInsertRequest, showToast, threadId]);

  const loadEarlier = async () => {
    if (!page?.has_earlier || page.next_before_index === null || !threadId) return;
    const scroller = scrollRef.current;
    const previousHeight = scroller?.scrollHeight || 0;
    // 历史消息插入当前视口上方后，后续 page/iframe 更新不能把用户带回线程底部。
    preserveScrollOnPageUpdateRef.current = true;
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

  useEffect(() => {
    if (!open || !page || historicalBodyHydrationRef.current) return;
    // 截断消息统一走全文补全：正文就绪前只展示骨架，不展示被截断的文本预览，
    // 避免原始 URL/编码串组成的“乱码”闪屏以及正文高度变化造成的滚动条抖动。
    const pendingMessage = page.messages.find((item) => (
      item.body_truncated
      && !displayBodyLoading.has(item.id)
      && !displayBodyLoaded.has(item.id)
      && !displayBodyErrors[item.id]
    ));
    if (!pendingMessage) return;

    // 历史消息可能只有截断预览或缓存摘要。逐封串行补全文，
    // 避免展开长线程时并发请求 Gmail。
    const task = loadFullDisplayBody(pendingMessage).finally(() => {
      historicalBodyHydrationRef.current = null;
    });
    historicalBodyHydrationRef.current = task;
  }, [displayBodyErrors, displayBodyLoaded, displayBodyLoading, loadFullDisplayBody, open, page]);

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
    showToast(t("toast.preparingDownload"));
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
      draftComposerMode: composerMode,
    });
  };

  const requestAiDraft = () => {
    aiDraftModeRef.current = composerMode;
    void submitPrompt(
      composerMode === "forward"
        ? t("detail.prompt.forwardNote")
        : t("detail.prompt.draftReply"),
      "draft_reply",
      true,
    );
  };

  const expandOverview = () => {
    void submitPrompt(t("detail.prompt.summarizeThread"), "summary", true);
  };

  const retryOverview = () => {
    if (!context || context.kind !== "gmail_thread") return;
    setAssistError("");
    setAssistLoading(true);
    void loadInboxThreadAssist(mailbox, context.thread_id, context.latest_message_id, context.anchor_message_id, locale)
      .then(setAssist)
      .catch((reason) => setAssistError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setAssistLoading(false));
  };

  const openComposer = (mode: ComposerMode = "reply") => {
    setComposerClosing(false);
    setComposerOpen(true);
    setModeMenuOpen(false);
    if (mode === "forward") {
      setComposerMode("forward");
      // 转发：用户说明区为空 + 自动附加引用块（不含附件）
      updateComposerDraft("forward", (current) => ({
        ...current,
        body: current.body || buildForwardDraftBody(latestMessage || undefined, ""),
        dirty: false,
      }));
      requestAnimationFrame(() => bodyRef.current?.focus());
      return;
    }
    setComposerMode("reply");
    setReplyMode("reply_to_sender");
    requestAnimationFrame(() => bodyRef.current?.focus());
  };

  const switchComposerMode = (mode: ComposerMode) => {
    setModeMenuOpen(false);
    if (mode === composerMode) return;
    if (mode === "forward") {
      setComposerMode("forward");
      updateComposerDraft("forward", (current) => ({
        ...current,
        body: current.body || buildForwardDraftBody(latestMessage || undefined, ""),
        dirty: false,
      }));
      return;
    }
    clearEmptyForwardDraft();
    setComposerMode("reply");
    setReplyMode("reply_to_sender");
  };

  const stageComposerFiles = async (files: FileList) => {
    const list = Array.from(files);
    if (!list.length) return;
    let runningTotal = totalOutgoingAttachmentBytes(composerAttachments.filter((item) => item.status !== "error"));
    for (const file of list) {
      const validation = validateOutgoingAttachment(file, runningTotal);
      if (!validation.ok) {
        showToast(validation.error || `Attachment rejected: ${file.name}`);
        continue;
      }
      const tempId = crypto.randomUUID().replace(/-/g, "");
      const localPreview = isImageOutgoingAttachment({ filename: file.name, mime_type: file.type })
        ? URL.createObjectURL(file)
        : "";
      setComposerAttachments((current) => [
        ...current,
        {
          id: tempId,
          filename: file.name,
          mime_type: file.type || "application/octet-stream",
          size: file.size,
          storage_key: "",
          preview_url: localPreview || undefined,
          status: "uploading",
          progress: 0,
        },
      ]);
      try {
        const begun = await actions.beginStageOutgoingAttachment(mailbox, {
          filename: file.name,
          mime_type: file.type || "application/octet-stream",
          size: file.size,
          existing_total_bytes: runningTotal,
          draft_scope: composerMode === "forward" ? "compose" : "thread",
          draft_key: composerMode === "forward" ? activeComposerDraft.id || "" : threadId,
        });
        if (!begun.ok || !begun.upload_url || !begun.attachment_id || !begun.storage_key) {
          throw new Error(begun.error || "Failed to stage attachment");
        }
         await putFileToUploadUrl(begun.upload_url, file, begun.upload_headers || {}, (ratio) => {
          setComposerAttachments((current) =>
            current.map((item) => (item.id === tempId ? { ...item, progress: ratio } : item)),
          );
         });
         await actions.completeStageOutgoingAttachment(mailbox, begun.storage_key, file.size, file.type || "application/octet-stream");
        runningTotal += file.size;
        setComposerAttachments((current) =>
          current.map((item) =>
            item.id === tempId
              ? {
                  ...item,
                  id: begun.attachment_id || tempId,
                  filename: begun.filename || file.name,
                  mime_type: begun.mime_type || file.type || "application/octet-stream",
                  size: begun.size || file.size,
                  storage_key: begun.storage_key || "",
                  status: "ready",
                  progress: 1,
                }
              : item,
          ),
        );
      } catch (reason) {
        const messageText = reason instanceof Error ? reason.message : String(reason);
        setComposerAttachments((current) =>
          current.map((item) =>
            item.id === tempId ? { ...item, status: "error", error: messageText, progress: 0 } : item,
          ),
        );
        showToast(messageText);
      }
    }
  };

  const removeComposerAttachment = async (id: string) => {
    const target = composerAttachments.find((item) => item.id === id);
    setComposerAttachments((current) => current.filter((item) => item.id !== id));
    if (target?.preview_url?.startsWith("blob:")) URL.revokeObjectURL(target.preview_url);
    if (target?.storage_key) {
      try {
        await actions.deleteStagedOutgoingAttachment(mailbox, target.storage_key);
      } catch {
        // ignore cleanup failure
      }
    }
  };

  const sendReply = async () => {
    if (!message || !threadId || !draft.trim() || !latestMessage) return;
    if (composerAttachments.some((item) => item.status === "uploading")) {
      showToast(t("toast.waitAttachments"));
      return;
    }
    const readyAttachments = toPersistedOutgoingAttachments(composerAttachments);
    if (composerMode === "forward") {
      if (!forwardTo.length) {
        showToast(t("toast.addForwardRecipient"));
        return;
      }
      setSending(true);
      try {
        // 发送时用原信 HTML 组装 multipart，保留排版；编辑栏仍是纯文本预览
        let source = latestMessage;
        if (source.body_truncated || (!source.body_html && source.id)) {
          try {
            const display = await resolveDisplayBodyPayload(
              await loadInboxMessageDisplayBody(mailbox, source.id),
            );
            source = {
              ...source,
              body_html: display.body_html || source.body_html,
              body_text: display.body_text || source.body_text,
              body_truncated: Boolean(display.body_truncated),
            };
          } catch {
            // 拉全文失败时仍按当前正文转发
          }
        }
        const sendBodies = buildForwardSendBodies(source, draft, draftHtml);
        if (onScheduleForward({
          mailbox,
          recipients: forwardTo,
          cc: replyCc,
          bcc: replyBcc,
          subject: buildForwardSubject(source.subject || message.subject || page?.subject),
          body: sendBodies.body,
          body_html: sendBodies.body_html,
          message,
          attachments: readyAttachments,
        })) return;
        setSending(false);
      } catch (reason) {
        showToast(reason instanceof Error ? reason.message : String(reason));
        setSending(false);
      }
      return;
    }
    const to = effectiveReplyToAddress;
    if (!to) {
      showToast(t("toast.replyRecipientUnavailable"));
      return;
    }
    setSending(true);
    try {
      const result = await saveInboxThreadDraft(
        mailbox,
        threadId,
        draft,
        draftHtml,
        draftEtag || undefined,
        {
          id: latestMessage.id || message.id,
          thread_id: threadId,
          mailbox,
          internal_date: latestMessage.internal_date || message.internal_date || "",
          date: message.date || "",
          from: latestMessage.from || message.from || "",
          to: latestMessage.to || message.to || "",
          subject: latestMessage.subject || message.subject || "",
          label_ids: (latestMessage.label_ids || message.label_ids || []).filter((label) => label.toUpperCase() !== "DRAFT"),
          important: Boolean(message.important || latestMessage.label_ids?.includes("IMPORTANT")),
          starred: Boolean(message.starred || latestMessage.label_ids?.includes("STARRED")),
          has_attachment: Boolean(message.has_attachment),
          attachment_count: Number(message.attachment_count || 0),
        },
        readyAttachments,
      );
      if (result.etag) setDraftEtag(result.etag);
      setDraftDirty(false);
      if (onScheduleReply({
        mailbox,
        threadId,
        to,
        body: draft,
        bodyHtml: draftHtml || undefined,
        cc: effectiveReplyCc,
        bcc: replyBcc,
        replyMode,
        message,
        attachments: readyAttachments,
      })) return;
      setSending(false);
    } catch (reason) {
      showToast(reason instanceof Error ? reason.message : String(reason));
      setSending(false);
    }
  };

  const discardDraft = async () => {
    const discardMode = composerMode;
    // Discard 是显式清空：阻止当前线程的持久化回复草稿在关闭后重新打开编辑栏。
    if (discardMode === "reply") {
      suppressDraftLoadForRef.current = draftStorageKey;
      draftLoadSequenceRef.current += 1;
      draftEditSequenceRef.current += 1;
    }
    updateComposerDraft(discardMode, emptyComposerDraft());
    setComposerMode("reply");
    setModeMenuOpen(false);
    await closeComposer();
    setComposerExpanded(false);
    await Promise.allSettled([...pendingDraftSavesRef.current]);
    if (discardMode === "reply" && threadId) await deleteInboxThreadDraft(mailbox, threadId);
  };

  const closeThread = async () => {
    onRequestClose();
    const forwardDraft = composerDrafts.forward;
    const forwardNote = stripForwardedMessageBlock(forwardDraft.body).trim();
    if (forwardNote) {
      try {
        await onSaveForwardDraft({
          id: forwardDraft.id || undefined,
          ifMatch: forwardDraft.etag || undefined,
          mailbox,
          recipients: forwardDraft.recipients,
          cc: forwardDraft.cc,
          bcc: forwardDraft.bcc,
          subject: buildForwardSubject(latestMessage?.subject || message?.subject || page?.subject),
          body: forwardDraft.body,
          bodyHtml: buildForwardSendBodies(latestMessage, forwardDraft.body, forwardDraft.bodyHtml).body_html,
          sourceThreadId: threadId,
          sourceMessageId: message?.id || "",
        });
      } catch (reason) {
        showToast(reason instanceof Error ? reason.message : String(reason));
        return;
      }
    } else {
      clearEmptyForwardDraft();
    }
    if (composerMode === "reply" && draftDirty && draft.trim()) showToast(t("detail.draftSaved"));
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

  const forwardNote = stripForwardedMessageBlock(draft);
  const forwardQuote = composerMode === "forward"
    ? buildForwardDraftBody(latestMessage || undefined, "").trimStart()
    : "";
  const updateForwardNote = ({ html, text }: RichTextValue) => {
    draftEditSequenceRef.current += 1;
    setDraft(buildForwardDraftBody(latestMessage || undefined, text));
    updateActiveComposerDraft({ bodyHtml: html });
    setDraftDirty(true);
  };

  if (!message) return null;

  const composerVisible = composerOpen || composerClosing;

  return (
    <>
      <aside className={`mail-detail-drawer ${open ? "is-open" : ""}`} aria-hidden={!open}>
        <header className="mail-detail-header">
          <div className="mail-detail-toolbar">
            <button aria-label={t("detail.closeThread")} data-tooltip={t("detail.closeThread")} onClick={() => void closeThread()}><CloseThreadIcon /></button>
            {trashed ? (
              <button className="is-active is-trashed" aria-label={t("detail.removeFromTrash")} data-tooltip={t("detail.removeFromTrash")} disabled={toolbarPending} onClick={() => void runThreadAction("untrash")}><TrashOffIcon /></button>
            ) : (
              <>
                <button aria-label={t("detail.markUnread")} data-tooltip={t("detail.markUnread")} disabled={toolbarPending} onClick={() => void runThreadAction("mark_unread")}><MarkUnreadIcon /></button>
                <button className={starred ? "is-active is-starred" : ""} aria-label={starred ? t("detail.star.remove") : t("detail.star.add")} data-tooltip={starred ? t("detail.star.remove") : t("detail.star.add")} disabled={toolbarPending} onClick={() => void runThreadAction(starred ? "unstar" : "star")}><StarIcon /></button>
                <button className={important ? "is-active is-important" : ""} aria-label={important ? t("detail.important.remove") : t("detail.important.add")} data-tooltip={important ? t("detail.important.remove") : t("detail.important.add")} disabled={toolbarPending} onClick={() => void runThreadAction(important ? "mark_not_important" : "mark_important")}><ImportantIcon /></button>
                <button className={isTodo ? "is-active is-todo" : ""} aria-label={isTodo ? t("detail.todo.remove") : t("detail.todo.add")} data-tooltip={isTodo ? t("detail.todo.remove") : t("detail.todo.add")} disabled={toolbarPending || isTodo} onClick={() => onTodoMessage(message)}><TodoIcon /></button>
                <button className={isSnoozed ? "is-active is-snoozed" : ""} aria-label={isSnoozed ? t("detail.snooze.remove") : t("detail.snooze.add")} data-tooltip={isSnoozed ? t("detail.snooze.remove") : t("detail.snooze.add")} disabled={toolbarPending} onClick={() => onSnoozeMessage(message)}><ClockIcon /></button>
                <button aria-label={t("detail.moveToTrash")} data-tooltip={t("detail.moveToTrash")} disabled={toolbarPending} onClick={() => void runThreadAction("trash")}><TrashIcon /></button>
                <button
                  className={isDone ? "is-active is-done" : ""}
                  aria-label={isSent ? t("detail.done.sent") : isDone ? t("detail.done.moveToInbox") : t("detail.done")}
                  data-tooltip={isSent ? t("detail.done.sent") : isDone ? t("detail.done.moveToInbox") : t("detail.done")}
                  disabled={toolbarPending || isSent}
                  onClick={() => onDoneMessage(message)}
                ><DoneIcon /></button>
              </>
            )}
          </div>
          <div className="mail-detail-summary">
            <h2>{displaySubject}</h2>
            {subjectWasModified ? (
              <p className="mail-detail-subject-note">{t("detail.latestSubject", { subject: latestSubject })}</p>
            ) : null}
            <div className="mail-detail-overview">
              {assistLoading ? <p className="mail-detail-overview-loading">{t("detail.generatingOverview")}</p> : assist?.overview ? <MailOverviewAction text={assist.overview} label={t("detail.expandDiscuss")} onClick={expandOverview} /> : assistError ? <p>{t("detail.aiUnavailable")} <button onClick={retryOverview}>{t("detail.retry")}</button></p> : null}
            </div>
            {fromAddress || toAddresses.length ? (
              <div className="mail-detail-participants">
                <HeaderContactRow label={t("detail.from")} address={fromAddress} avatarUrl={fromAvatarUrl} />
                <HeaderRecipientRows label={t("detail.to")} addresses={toAddresses} avatarUrls={recipientAvatarUrls} />
              </div>
            ) : null}
          </div>
        </header>

        <div className="mail-detail-context" ref={scrollRef}>
          <div className="mail-detail-thread">
          {page?.has_earlier ? <button className="mail-detail-load-earlier" onClick={() => void loadEarlier()}>{t("detail.loadEarlier")}</button> : null}
          {loading && !page ? <MailDetailLoadingSkeleton /> : null}
          {error ? <div className="mail-detail-error">{t("detail.threadLoadFailed")} {error}</div> : null}
          {(visibleThreadMessages.length ? visibleThreadMessages : []).map((item) => {
            const hasDisplayBody = Boolean(item.body_html?.trim() || item.body_text?.trim());
            // 截断消息在全文加载完成前一律展示加载骨架，避免先出现截断文本预览
            // （大段原始链接/编码串）再被完整正文替换造成的乱码闪屏与滚动条抖动。
            const waitForFullBody = item.body_truncated
              && !displayBodyLoaded.has(item.id)
              && !displayBodyErrors[item.id];
            return (
            <article
              key={item.id}
              className="mail-thread-message"
              data-message-id={item.id}
            >
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
                        <strong>{sender.name || sender.email || t("mail.unknownSender")}</strong>
                      </>
                    );
                  })()}
                </div>
                <time>{formatAbsoluteDateTime(item.internal_date, locale)}</time>
              </div>
              {waitForFullBody ? <MailThreadBodyLoading /> : item.body_html ? <SafeEmailHtml className="mail-thread-message-body is-html" html={item.body_html} scaleToFit /> : hasDisplayBody ? <SafeEmailText className="mail-thread-message-body" text={stripQuotedReplyForDisplay(item.body_text || "")} /> : null}
              {displayBodyErrors[item.id] ? (
                <p className="mail-thread-message-notice is-error">
                  <span>{displayBodyErrors[item.id]}</span>
                  {item.body_truncated && !displayBodyLoaded.has(item.id) ? <button onClick={() => void loadFullDisplayBody(item, true)}>Retry</button> : null}
                </p>
              ) : null}
              <AttachmentSection
                attachments={item.attachments}
                onPreview={(attachment) => void openAttachmentPreview({ attachment, messageId: attachment.message_id || item.id })}
                onDownload={(attachment) => void downloadAttachment({ attachment, messageId: attachment.message_id || item.id }).catch((reason) => showToast(reason instanceof Error ? reason.message : String(reason)))}
                isDownloading={(attachment) => isAttachmentDownloading({ attachment, messageId: attachment.message_id || item.id })}
              />
            </article>
            );
          })}
          {showQuickReplies ? (
            <section className="mail-detail-quick-replies" aria-label={t("detail.quickReplies")}>
              {quickReplySuggestions.map((item) => (
                <button key={item.id} onClick={() => void submitPrompt(buildQuickReplyPrompt(item))}>
                  <AiSparkleIcon />
                  <span>{item.label}</span>
                </button>
              ))}
            </section>
          ) : null}
          {showNoReplyNotice ? (
            <section className="mail-detail-no-reply-notice" aria-label={t("detail.noReplyNeeded")}>
              <p>
                <strong>{t("detail.noReplyNeeded")}</strong>
                {noReplyReason ? ` ${noReplyReason}` : ""}
              </p>
            </section>
          ) : null}
          </div>
        </div>

        <footer ref={footerRef} className={`mail-detail-footer ${composerVisible ? "is-composer-open" : ""} ${composerClosing ? "is-composer-closing" : ""} ${composerExpanded ? "is-expanded" : ""}`}>
          <div className="mail-detail-reply">
            {!composerVisible ? (
              <div className="mail-detail-reply-bar">
                <button
                  type="button"
                  className="mail-detail-reply-action"
                  aria-label={t("detail.reply")}
                  data-tooltip={t("detail.reply")}
                  onClick={() => openComposer("reply")}
                >
                  <ReplyModeIcon />
                </button>
                {canReplyAll ? <button
                  type="button"
                  className="mail-detail-reply-action mail-detail-reply-all-action"
                  aria-label={t("detail.replyAll")}
                  data-tooltip={t("detail.replyAll")}
                  onClick={() => {
                    const recipients = deriveReplyAllRecipients(latestMessage, mailbox);
                    if (!recipients.to) {
                      showToast(t("toast.replyAllRecipientsUnavailable"));
                      return;
                    }
                    setComposerClosing(false);
                    setComposerOpen(true);
                    setComposerMode("reply");
                    setReplyMode("reply_all");
                    updateComposerDraft("reply", (current) => ({
                      ...current,
                      cc: current.cc.length ? current.cc : recipients.cc,
                      ccOpen: Boolean(current.cc.length || recipients.cc.length),
                    }));
                    requestAnimationFrame(() => bodyRef.current?.focus());
                  }}
                >
                  <ReplyAllModeIcon />
                </button> : null}
                <button
                  type="button"
                  className="mail-detail-reply-action"
                  aria-label={t("detail.forward")}
                  data-tooltip={t("detail.forward")}
                  onClick={() => openComposer("forward")}
                >
                  <ForwardModeIcon />
                </button>
              </div>
            ) : (
              <div className="mail-detail-composer" ref={composerRef}>
                <div className="mail-detail-composer-head">
                  <div className="mail-detail-composer-to-row">
                    <div className="mail-detail-mode-switch" ref={modeMenuRef}>
                      <button
                        type="button"
                        className="mail-detail-mode-btn"
                        aria-label={composerMode === "forward" ? t("detail.forwardMode") : replyMode === "reply_all" ? t("detail.replyAllMode") : t("detail.replyMode")}
                        aria-expanded={modeMenuOpen}
                        onClick={() => setModeMenuOpen((value) => !value)}
                      >
                        {composerMode === "forward" ? <ForwardModeIcon /> : <ReplyModeIcon />}
                        <ModeChevronIcon />
                      </button>
                      {modeMenuOpen ? (
                        <div className="mail-detail-mode-menu" role="menu">
                          <button
                            type="button"
                            role="menuitem"
                            className={composerMode === "reply" && replyMode === "reply_to_sender" ? "is-active" : ""}
                            onClick={() => switchComposerMode("reply")}
                          >
                            <ReplyModeIcon />
                            <span>{t("detail.reply")}</span>
                          </button>
                          {canReplyAll ? <button
                            type="button"
                            role="menuitem"
                            className={composerMode === "reply" && replyMode === "reply_all" ? "is-active" : ""}
                            onClick={() => {
                              const recipients = deriveReplyAllRecipients(latestMessage, mailbox);
                              if (!recipients.to) {
                                showToast(t("toast.replyAllRecipientsUnavailable"));
                                return;
                              }
                              setComposerMode("reply");
                              setReplyMode("reply_all");
                              setModeMenuOpen(false);
                              updateComposerDraft("reply", (current) => ({
                                ...current,
                                cc: current.cc.length ? current.cc : recipients.cc,
                                ccOpen: Boolean(current.cc.length || recipients.cc.length),
                              }));
                            }}
                          >
                            <ReplyAllModeIcon />
                            <span>{t("detail.replyAll")}</span>
                          </button> : null}
                          <button
                            type="button"
                            role="menuitem"
                            className={composerMode === "forward" ? "is-active" : ""}
                            onClick={() => switchComposerMode("forward")}
                          >
                            <ForwardModeIcon />
                            <span>{t("detail.forward")}</span>
                          </button>
                        </div>
                      ) : null}
                    </div>
                    {composerMode === "forward" ? (
                      <div className="mail-detail-forward-to">
                        <RecipientChipInput
                          label={t("detail.to")}
                          emails={forwardTo}
                          onChange={setForwardTo}
                          mailbox={mailbox}
                          searchContacts={searchComposeContacts}
                          fieldRole="to"
                          placeholder={t("detail.forwardToPlaceholder")}
                        />
                      </div>
                    ) : (
                      <div className="mail-detail-forward-to">
                        {!loading && effectiveReplyToAddress ? (
                          <RecipientChipInput
                            label={t("detail.to")}
                            emails={[effectiveReplyToAddress]}
                            onChange={() => undefined}
                            mailbox={mailbox}
                            searchContacts={searchComposeContacts}
                            fieldRole="to"
                            placeholder=""
                            readOnly
                          />
                        ) : null}
                      </div>
                    )}
                  </div>
                  <div>
                    {!replyCcOpen || !replyBccOpen ? (
                      <div className="compose-cc-bcc-toggle">
                        {!replyCcOpen ? (
                          <button
                            type="button"
                            onMouseDown={(event) => event.preventDefault()}
                            onClick={() => {
                              setReplyCcOpen(true);
                              setReplyFocusField("cc");
                            }}
                          >
                            Cc
                          </button>
                        ) : null}
                        {!replyBccOpen ? (
                          <button
                            type="button"
                            onMouseDown={(event) => event.preventDefault()}
                            onClick={() => {
                              setReplyBccOpen(true);
                              setReplyFocusField("bcc");
                            }}
                          >
                            Bcc
                          </button>
                        ) : null}
                      </div>
                    ) : null}
                    <button
                      className="mail-detail-composer-icon-btn"
                      aria-label={composerExpanded ? t("detail.collapse") : t("detail.expand")}
                      data-tooltip={composerExpanded ? t("detail.collapse") : t("detail.expand")}
                      onClick={() => setComposerExpanded((value) => !value)}
                    >
                      {composerExpanded ? <CollapseInlineIcon /> : <ExpandInlineIcon />}
                    </button>
                    <OutgoingAttachButton disabled={sending} onPick={(files) => void stageComposerFiles(files)} />
                    <button
                      className="mail-detail-composer-icon-btn"
                      aria-label={composerMode === "forward" ? t("detail.aiForwardNote") : t("detail.aiDraft")}
                      data-tooltip={composerMode === "forward" ? t("detail.aiForwardNote") : t("detail.aiDraft")}
                      disabled={aiBusy || !context}
                      onClick={requestAiDraft}
                    >
                      <AiDraftIcon />
                    </button>
                    <button
                      className="mail-detail-composer-icon-btn"
                      aria-label={t("detail.discardDraft")}
                      data-tooltip={t("detail.discardDraft")}
                      onClick={() => void discardDraft()}
                    >
                      <TrashIcon />
                    </button>
                  </div>
                </div>
                {replyCcOpen ? (
                  <RecipientChipInput
                    label="Cc"
                    emails={replyCc}
                    onChange={setReplyCc}
                    onInputValueChange={setReplyCcInput}
                    mailbox={mailbox}
                    searchContacts={searchComposeContacts}
                    fieldRole="cc"
                    autoFocus={replyFocusField === "cc"}
                    onEmptyBlur={() => {
                      setReplyCc([]);
                      setReplyCcOpen(false);
                      setReplyFocusField(null);
                    }}
                  />
                ) : null}
                {replyBccOpen ? (
                  <RecipientChipInput
                    label="Bcc"
                    emails={replyBcc}
                    onChange={setReplyBcc}
                    onInputValueChange={setReplyBccInput}
                    mailbox={mailbox}
                    searchContacts={searchComposeContacts}
                    fieldRole="bcc"
                    autoFocus={replyFocusField === "bcc"}
                    onEmptyBlur={() => {
                      setReplyBcc([]);
                      setReplyBccOpen(false);
                      setReplyFocusField(null);
                    }}
                  />
                ) : null}
                <div className={`mail-detail-composer-body${composerMode === "forward" && forwardQuote ? " is-forward" : ""}`}>
                  {composerMode === "forward" ? (
                    <>
                      <RichTextEditor
                        ref={bodyRef}
                        value={draftHtml || plainTextToEditorHtml(forwardNote)}
                        onChange={updateForwardNote}
                        placeholder={t("detail.forwardMessagePlaceholder")}
                      />
                      {forwardQuote ? (
                        <div className="mail-detail-forward-quote" aria-label={t("detail.forwardedContent")}>
                          <pre>{forwardQuote}</pre>
                        </div>
                      ) : null}
                    </>
                  ) : (
                    <RichTextEditor
                      ref={bodyRef}
                      value={draftHtml || plainTextToEditorHtml(draft)}
                      onChange={(value) => {
                        draftEditSequenceRef.current += 1;
                        setDraftContent(value);
                        setDraftDirty(true);
                      }}
                      placeholder={t("detail.replyPlaceholder")}
                    />
                  )}
                </div>
                <OutgoingAttachmentList items={composerAttachments} onRemove={(id) => void removeComposerAttachment(id)} />
                <div className="mail-detail-composer-actions">
                  <button
                    className="is-primary"
                    disabled={
                      sending
                      || !draft.trim()
                      || (composerMode === "forward" && !forwardTo.length)
                      || composerAttachments.some((item) => item.status === "uploading")
                    }
                    onClick={() => void sendReply()}
                  >
                    {sending ? "Sending…" : "Send"}
                  </button>
                </div>
              </div>
            )}
          </div>
        </footer>
      </aside>

      {previewAttachment ? (
        <div className="attachment-preview-modal" role="dialog" aria-modal="true" aria-label={previewAttachment.attachment.filename}>
          <button className="attachment-preview-backdrop" aria-label={t("detail.closePreview")} onClick={closePreview} />
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
