import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type {
  AiMailContextRef,
  AttachmentDownloadPayload,
  DraftReplyArtifact,
  InboxMessage,
  InboxThreadAssistPayload,
  InboxThreadMessage,
  InboxThreadPagePayload,
  InboxThreadStateOperation,
  MailAttachmentMeta,
  SubmitMailPromptRequest,
} from "../../types/mail";
import { SafeEmailHtml, SafeEmailText } from "../../shared/SafeEmailHtml";
import {
  buildQuickReplyPrompt,
  deriveReplyToAddress,
  formatAbsoluteDateTime,
  formatAttachmentSize,
  isOutboundMessageForMailbox,
  isPreviewableAttachment,
  matchesDraftArtifact,
  senderParts,
  splitAddresses,
} from "./mailDetailHelpers";

type MailUiFlags = {
  todos: string[];
  snoozed: string[];
  done: string[];
};

type InsertRequest = {
  nonce: string;
  artifact: DraftReplyArtifact;
} | null;

function messageAvatar(sender: string) {
  const parts = senderParts(sender);
  const label = parts.name || parts.email || "?";
  const initial = (label.match(/[A-Za-z0-9]/)?.[0] || label.slice(0, 1) || "?").toUpperCase();
  const tone = (initial.charCodeAt(0) || 65) % 5;
  return { initial, tone };
}

function ThreadMessageAvatar({ sender, avatarUrl }: { sender: string; avatarUrl?: string }) {
  const [failed, setFailed] = useState(false);
  const avatar = messageAvatar(sender);
  return avatarUrl && !failed
    ? <img className="sender-avatar is-photo" src={avatarUrl} alt="" referrerPolicy="no-referrer" onError={() => setFailed(true)} />
    : <span className={`sender-avatar tone-${avatar.tone}`} aria-hidden="true">{avatar.initial}</span>;
}

function AttachmentSection({
  attachments,
  onPreview,
  onDownload,
}: {
  attachments: MailAttachmentMeta[];
  onPreview: (attachment: MailAttachmentMeta) => void;
  onDownload: (attachment: MailAttachmentMeta) => void;
}) {
  if (!attachments.length) return null;
  return (
    <div className="mail-detail-attachments">
      {attachments.map((attachment) => (
        <article key={attachment.id} className="mail-detail-attachment">
          <div>
            <strong>{attachment.filename}</strong>
            <span>{attachment.mime_type}{attachment.size ? ` · ${formatAttachmentSize(attachment.size)}` : ""}</span>
          </div>
          <div>
            {isPreviewableAttachment(attachment) ? <button onClick={() => onPreview(attachment)}>Preview</button> : null}
            <button onClick={() => onDownload(attachment)}>Download</button>
          </div>
        </article>
      ))}
    </div>
  );
}

function firstAddress(value: string | undefined, mailbox?: string) {
  const candidates = splitAddresses(value).map(senderParts);
  if (!candidates.length) return null;
  const normalizedMailbox = mailbox?.trim().toLowerCase();
  return candidates.find((item) => item.email.toLowerCase() !== normalizedMailbox) || candidates[0];
}

function displayAddress(parts: { name: string; email: string } | null) {
  if (!parts) return { title: "Unknown", subtitle: "" };
  return {
    title: parts.name && parts.name !== parts.email ? parts.name : parts.email || "Unknown",
    subtitle: parts.name && parts.email && parts.name !== parts.email ? parts.email : "",
  };
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
        <ThreadMessageAvatar sender={sender} avatarUrl={avatarUrl} />
      </span>
      <span className="mail-detail-contact-text">
        <strong>{display.title}</strong>
        {display.subtitle ? <span>{display.subtitle}</span> : null}
      </span>
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

function scrollToLatestMessage(container: HTMLDivElement | null, latestMessageId: string) {
  if (!container || !latestMessageId) return;
  const target = Array.from(container.querySelectorAll<HTMLElement>("[data-message-id]"))
    .find((element) => element.dataset.messageId === latestMessageId);
  if (target) {
    target.scrollIntoView({ block: "start", behavior: "auto" });
  }
}

function AiSparkleIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d="M12 3c.6 4.5 2.8 6.7 7.2 7.2-4.4.5-6.6 2.7-7.2 7.2-.6-4.5-2.8-6.7-7.2-7.2C9.2 9.7 11.4 7.5 12 3Z" />
      <path d="M19 3v4M17 5h4" />
    </svg>
  );
}

function AnimatedMailOverview({ text }: { text: string }) {
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
    <p className="mail-detail-overview-result">
      <AiSparkleIcon />
      <span>{visibleText}</span>
    </p>
  );
}

function ToolbarIcon({ children }: { children: ReactNode }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>;
}

const CloseThreadIcon = () => <ToolbarIcon><path d="m8 6 6 6-6 6M14 6l6 6-6 6" /></ToolbarIcon>;
const MarkUnreadIcon = () => <ToolbarIcon><rect x="3" y="5" width="18" height="14" rx="4" /><path d="m4 8 8 6 5-3.8" /><circle cx="18.5" cy="6" r="2.2" fill="currentColor" stroke="white" /></ToolbarIcon>;
const StarIcon = () => <ToolbarIcon><path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3Z" /></ToolbarIcon>;
const ImportantIcon = () => <ToolbarIcon><path d="M5 6h10l4 6-4 6H5l4-6-4-6Z" /><path d="M11 9v4M11 16h.01" /></ToolbarIcon>;
const TodoIcon = () => <ToolbarIcon><rect x="4" y="4" width="16" height="16" rx="4" /><path d="m8 12 2.5 2.5L16 9" /></ToolbarIcon>;
const ClockIcon = () => <ToolbarIcon><circle cx="12" cy="12" r="8" /><path d="M12 7v5l3 2" /></ToolbarIcon>;
const TrashIcon = () => <ToolbarIcon><path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13" /></ToolbarIcon>;
const TrashOffIcon = () => <ToolbarIcon><path d="M9 7V4h6v3M7.5 7H19M7 10l1 10h8l.6-6M4 4l16 16" /></ToolbarIcon>;
const DoneIcon = () => <ToolbarIcon><path d="m4 12 5 5L20 6" /></ToolbarIcon>;

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
  }) => Promise<InboxThreadPagePayload>;
  loadInboxMessageDisplayBody: (mailbox: string, messageId: string) => Promise<{ body_text?: string; body_html?: string; body_truncated?: boolean }>;
  loadInboxThreadAssist: (mailbox: string, threadId: string, latestMessageId: string, anchorMessageId?: string) => Promise<InboxThreadAssistPayload>;
  getInboxThreadDraft: (mailbox: string, threadId: string) => Promise<{ exists: boolean; body: string; etag?: string }>;
  saveInboxThreadDraft: (mailbox: string, threadId: string, body: string, ifMatch?: string) => Promise<{ etag?: string }>;
  deleteInboxThreadDraft: (mailbox: string, threadId: string) => Promise<{ ok?: boolean }>;
  prepareInboxAttachmentAccess: (mailbox: string, messageId: string, attachmentId: string, mode: "preview" | "download") => Promise<AttachmentDownloadPayload>;
  submitMailContextPrompt: (request: SubmitMailPromptRequest) => Promise<unknown>;
  sendInboxThreadReply: (args: { mailbox: string; threadId: string; to: string; body: string; replyMode?: string; dryRun?: boolean }) => Promise<{ ok?: boolean; error?: string }>;
  contactAvatars?: Record<string, string>;
  loadContactAvatars: (emails: string[], mailbox?: string) => Promise<{ avatars: Record<string, string>; permissionRequired: boolean }>;
  latestThreadMessageId?: string;
}) {
  const [page, setPage] = useState<InboxThreadPagePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [assist, setAssist] = useState<InboxThreadAssistPayload | null>(null);
  const [assistLoading, setAssistLoading] = useState(false);
  const [assistError, setAssistError] = useState("");
  const [composerOpen, setComposerOpen] = useState(false);
  const [composerExpanded, setComposerExpanded] = useState(false);
  const [draft, setDraft] = useState("");
  const [draftDirty, setDraftDirty] = useState(false);
  const [draftEtag, setDraftEtag] = useState("");
  const [sending, setSending] = useState(false);
  const [toolbarPending, setToolbarPending] = useState(false);
  const [displayBodyLoading, setDisplayBodyLoading] = useState<Set<string>>(() => new Set());
  const [displayBodyLoaded, setDisplayBodyLoaded] = useState<Set<string>>(() => new Set());
  const [displayBodyErrors, setDisplayBodyErrors] = useState<Record<string, string>>({});
  const [previewAttachment, setPreviewAttachment] = useState<MailAttachmentMeta | null>(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [previewText, setPreviewText] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [resolvedAvatars, setResolvedAvatars] = useState<Record<string, string>>({});
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const composerRef = useRef<HTMLDivElement | null>(null);
  const bodyRef = useRef<HTMLTextAreaElement | null>(null);
  const loadInboxEmailBodyRef = useRef(loadInboxEmailBody);
  const loadInboxThreadPageRef = useRef(loadInboxThreadPage);
  const loadInboxMessageDisplayBodyRef = useRef(loadInboxMessageDisplayBody);
  const loadInboxThreadAssistRef = useRef(loadInboxThreadAssist);
  const getInboxThreadDraftRef = useRef(getInboxThreadDraft);
  const saveInboxThreadDraftRef = useRef(saveInboxThreadDraft);
  const loadContactAvatarsRef = useRef(loadContactAvatars);
  const assistRequestKeyRef = useRef("");

  const threadId = message?.thread_id || "";
  const messageId = message?.id || "";
  const context = useMemo(() => (message ? buildMailContext(mailbox, message, page) : null), [mailbox, message, page]);
  const latestMessage = page?.messages[page.messages.length - 1];
  const hasThreadUpdate = Boolean(page?.latest_message_id && latestThreadMessageId && latestThreadMessageId !== page.latest_message_id);
  const important = Boolean(latestMessage?.label_ids?.includes("IMPORTANT") || message?.important);
  const starred = Boolean(latestMessage?.label_ids?.includes("STARRED") || message?.starred);
  const trashed = Boolean(latestMessage?.label_ids?.includes("TRASH") || message?.label_ids?.includes("TRASH"));
  const isTodo = Boolean(message && flags.todos.includes(message.id));
  const isSnoozed = Boolean(message && flags.snoozed.includes(message.id));
  const isDone = Boolean(message && flags.done.includes(message.id));
  const previewableAttachments = useMemo(
    () => page?.messages.flatMap((item) => item.attachments.filter(isPreviewableAttachment).map((attachment) => ({ attachment, messageId: item.id }))) || [],
    [page],
  );
  const showQuickReplies = Boolean(
    assist?.quick_replies?.length
    && latestMessage
    && !isOutboundMessageForMailbox(latestMessage.from, mailbox),
  );
  const fromAddress = useMemo(() => firstAddress(message?.from || undefined), [message?.from]);
  const toAddress = useMemo(() => firstAddress(message?.to || undefined, mailbox), [mailbox, message?.to]);
  const fromAvatarUrl = fromAddress ? (resolvedAvatars[fromAddress.email.toLowerCase()] || contactAvatars?.[fromAddress.email.toLowerCase()]) : undefined;
  const toAvatarUrl = toAddress ? (resolvedAvatars[toAddress.email.toLowerCase()] || contactAvatars?.[toAddress.email.toLowerCase()]) : undefined;

  useEffect(() => {
    loadInboxEmailBodyRef.current = loadInboxEmailBody;
    loadInboxThreadPageRef.current = loadInboxThreadPage;
    loadInboxMessageDisplayBodyRef.current = loadInboxMessageDisplayBody;
    loadInboxThreadAssistRef.current = loadInboxThreadAssist;
    getInboxThreadDraftRef.current = getInboxThreadDraft;
    saveInboxThreadDraftRef.current = saveInboxThreadDraft;
    loadContactAvatarsRef.current = loadContactAvatars;
  }, [getInboxThreadDraft, loadContactAvatars, loadInboxEmailBody, loadInboxMessageDisplayBody, loadInboxThreadAssist, loadInboxThreadPage, saveInboxThreadDraft]);

  useEffect(() => {
    if (!open || !message || !mailbox || !messageId) return;
    let cancelled = false;
    const anchorMessage = message;
    setPage(null);
    setLoading(true);
    setError("");
    setAssist(null);
    setAssistLoading(Boolean(threadId));
    setAssistError("");
    setComposerOpen(false);
    setComposerExpanded(false);
    setDraft("");
    setDraftDirty(false);
    setDraftEtag("");
    setPreviewAttachment(null);
    setPreviewUrl("");
    setPreviewText("");
    setPreviewLoading(false);
    const requestAssist = (latestMessageId: string) => {
      if (!threadId) return;
      const requestKey = `${mailbox}:${threadId}:${latestMessageId}:${messageId}`;
      if (assistRequestKeyRef.current === requestKey) return;
      assistRequestKeyRef.current = requestKey;
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
    const load = async () => {
      try {
        if (threadId) {
          const nextPage = await loadInboxThreadPageRef.current(mailbox, threadId, { anchorMessageId: messageId, limit: 5, includeDisplayBody: true });
          if (cancelled) return;
          setPage(nextPage);
          requestAnimationFrame(() => scrollToLatestMessage(scrollRef.current, nextPage.latest_message_id));
          setAssistLoading(true);
          requestAssist(nextPage.latest_message_id || messageId);
        } else {
          const body = await loadInboxEmailBodyRef.current(messageId, mailbox);
          if (cancelled) return;
          setPage({
            mailbox,
            thread_id: anchorMessage.thread_id || messageId,
            subject: anchorMessage.subject || "(no subject)",
            latest_message_id: messageId,
            returned_count: 1,
            has_earlier: false,
            next_before_index: null,
            messages: [{
              id: messageId,
              thread_id: anchorMessage.thread_id || messageId,
              internal_date: anchorMessage.internal_date || "",
              from: anchorMessage.from || "",
              to: anchorMessage.to || "",
              subject: anchorMessage.subject || "(no subject)",
              label_ids: anchorMessage.label_ids || [],
              body_text: body,
              attachments: [],
            }],
          });
        }
      } catch (reason) {
        if (cancelled) return;
        const threadError = reason instanceof Error ? reason.message : String(reason);
        if (threadId) {
          try {
            const display = await loadInboxMessageDisplayBodyRef.current(mailbox, messageId);
            if (cancelled) return;
            setPage({
              mailbox,
              thread_id: threadId,
              subject: anchorMessage.subject || "(no subject)",
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
                body_html: display.body_html || "",
                body_text: display.body_text || "",
                body_truncated: Boolean(display.body_truncated),
                attachments: [],
              }],
            });
            setError("");
            setAssistLoading(true);
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
  }, [mailbox, messageId, open, threadId]);

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
    if (!composerOpen || !threadId || !mailbox) return;
    let cancelled = false;
    void getInboxThreadDraftRef.current(mailbox, threadId)
      .then((stored) => {
        if (cancelled) return;
        if (stored.body) {
          setDraft(stored.body);
          setDraftEtag(stored.etag || "");
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [composerOpen, mailbox, threadId]);

  useEffect(() => {
    if (!composerOpen || !threadId || !mailbox || !draftDirty) return;
    const timer = window.setTimeout(() => {
      void saveInboxThreadDraftRef.current(mailbox, threadId, draft, draftEtag || undefined)
        .then((result) => {
          setDraftDirty(false);
          if (result.etag) setDraftEtag(result.etag);
        })
        .catch(() => undefined);
    }, 500);
    return () => window.clearTimeout(timer);
  }, [composerOpen, draft, draftDirty, draftEtag, mailbox, threadId]);

  useEffect(() => {
    if (!composerOpen) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (!composerRef.current?.contains(event.target as Node) && !draft.trim() && !sending && !aiBusy) {
        setComposerOpen(false);
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [aiBusy, composerOpen, draft, sending]);

  useEffect(() => {
    if (!insertRequest || !message || !threadId) return;
    const { artifact, nonce } = insertRequest;
    if (!matchesDraftArtifact(mailbox, threadId, artifact)) return;
    onConsumeInsertRequest(nonce);
    if (draft.trim() && !window.confirm("Replace the current draft reply?")) return;
    setComposerOpen(true);
    setDraft(artifact.body);
    setDraftDirty(true);
    requestAnimationFrame(() => bodyRef.current?.focus());
  }, [draft, insertRequest, mailbox, message, onConsumeInsertRequest, threadId]);

  const refreshThread = async () => {
    if (!threadId || !messageId) return;
    setLoading(true);
    setError("");
    try {
      const refreshed = await loadInboxThreadPageRef.current(mailbox, threadId, { anchorMessageId: messageId, limit: 5, includeDisplayBody: true });
      setPage(refreshed);
      if (refreshed.latest_message_id) {
        setAssistLoading(true);
        const requestKey = `${mailbox}:${threadId}:${refreshed.latest_message_id}:${messageId}`;
        assistRequestKeyRef.current = requestKey;
        void loadInboxThreadAssistRef.current(mailbox, threadId, refreshed.latest_message_id, messageId)
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
      requestAnimationFrame(() => scrollToLatestMessage(scrollRef.current, refreshed.latest_message_id));
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
    setPage((current) => current ? {
      ...older,
      messages: [...older.messages, ...current.messages.filter((item) => !older.messages.some((olderItem) => olderItem.id === item.id))],
    } : older);
    requestAnimationFrame(() => {
      if (!scroller) return;
      scroller.scrollTop += scroller.scrollHeight - previousHeight;
    });
  };

  const loadFullDisplayBody = async (item: InboxThreadMessage) => {
    if (displayBodyLoading.has(item.id) || displayBodyLoaded.has(item.id)) return;
    setDisplayBodyLoading((current) => new Set(current).add(item.id));
    setDisplayBodyErrors((current) => ({ ...current, [item.id]: "" }));
    try {
      const display = await loadInboxMessageDisplayBody(mailbox, item.id);
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

  const openAttachment = async (attachment: MailAttachmentMeta, mode: "preview" | "download") => {
    if (!page) return;
    const owner = page.messages.find((item) => item.attachments.some((candidate) => candidate.id === attachment.id));
    if (!owner) return;
    const result = await prepareInboxAttachmentAccess(mailbox, owner.id, attachment.id, mode);
    const url = String((mode === "preview" ? result.preview_url : result.download_url) || result.download_url || "");
    if (!url) {
      showToast(String(result.error || "Attachment URL is unavailable."));
      return;
    }
    if (mode === "download") {
      window.open(url, "_blank", "noopener,noreferrer");
      return;
    }
    setPreviewAttachment(attachment);
    setPreviewLoading(true);
    setPreviewUrl(url);
    if ((attachment.mime_type || "").startsWith("text/") || attachment.mime_type === "application/json") {
      try {
        const response = await fetch(url);
        setPreviewText(await response.text());
      } catch {
        setPreviewText("");
      }
    } else {
      setPreviewText("");
    }
    setPreviewLoading(false);
  };

  const submitPrompt = async (visiblePrompt: string, expectedArtifact: "draft_reply" = "draft_reply") => {
    if (!context) return;
    await submitMailContextPrompt({ visiblePrompt, context, expectedArtifact });
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
      const refreshed = await loadInboxThreadPageRef.current(mailbox, threadId, { anchorMessageId: message?.id, limit: 5, includeDisplayBody: true });
      setPage(refreshed);
      requestAnimationFrame(() => scrollToLatestMessage(scrollRef.current, refreshed.latest_message_id));
    } catch (reason) {
      showToast(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSending(false);
    }
  };

  const discardDraft = async () => {
    if (draft.trim() && !window.confirm("Discard this draft reply?")) return;
    setDraft("");
    setDraftDirty(false);
    setComposerOpen(false);
    if (threadId) await deleteInboxThreadDraft(mailbox, threadId);
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

  if (!open || !message) return null;

  return (
    <>
      <aside className={`mail-detail-drawer ${open ? "is-open" : ""}`} aria-hidden={!open}>
        <header className="mail-detail-header">
          <div className="mail-detail-toolbar">
            <button aria-label="Close thread" data-tooltip="Close thread" onClick={onClose}><CloseThreadIcon /></button>
            <button aria-label="Mark unread" data-tooltip="Mark unread" disabled={toolbarPending} onClick={() => void runThreadAction("mark_unread")}><MarkUnreadIcon /></button>
            <button className={starred ? "is-active is-starred" : ""} aria-label={starred ? "Remove stars" : "Add stars"} data-tooltip={starred ? "Remove stars" : "Add stars"} disabled={toolbarPending} onClick={() => void runThreadAction(starred ? "unstar" : "star")}><StarIcon /></button>
            <button className={important ? "is-active is-important" : ""} aria-label={important ? "Mark not important" : "Mark important"} data-tooltip={important ? "Mark not important" : "Mark important"} disabled={toolbarPending} onClick={() => void runThreadAction(important ? "mark_not_important" : "mark_important")}><ImportantIcon /></button>
            <button className={isTodo ? "is-active is-todo" : ""} aria-label={isTodo ? "Click Done to remove" : "Add to Todo"} data-tooltip={isTodo ? "Click Done to remove" : "Add to Todo"} disabled={toolbarPending || isTodo} onClick={() => onTodoMessage(message)}><TodoIcon /></button>
            <button className={isSnoozed ? "is-active is-snoozed" : ""} aria-label={isSnoozed ? "Remove from snoozed" : "Snooze"} data-tooltip={isSnoozed ? "Remove from snoozed" : "Snooze"} disabled={toolbarPending} onClick={() => onSnoozeMessage(message)}><ClockIcon /></button>
            <button className={trashed ? "is-active is-trashed" : ""} aria-label={trashed ? "Remove from trash" : "Move to trash"} data-tooltip={trashed ? "Remove from trash" : "Move to trash"} disabled={toolbarPending} onClick={() => void runThreadAction(trashed ? "untrash" : "trash")}>{trashed ? <TrashOffIcon /> : <TrashIcon />}</button>
            <button className={isDone ? "is-active is-done" : ""} aria-label={isDone ? "Move to inbox" : "Done"} data-tooltip={isDone ? "Move to inbox" : "Done"} disabled={toolbarPending} onClick={() => onDoneMessage(message)}><DoneIcon /></button>
          </div>
          <div className="mail-detail-summary">
            <h2>{page?.subject || message.subject || "(no subject)"}</h2>
            {hasThreadUpdate ? (
              <div className="mail-detail-update-banner" role="status">
                <span>Newer mail is available in this thread.</span>
                <button onClick={() => void refreshThread()}>Update thread</button>
              </div>
            ) : null}
            <div className="mail-detail-overview">
              {assistLoading ? <p className="mail-detail-overview-loading">Loading AI overview…</p> : assist?.overview ? <AnimatedMailOverview text={assist.overview} /> : assistError ? <p>AI is unavailable. <button onClick={() => context && void loadInboxThreadAssist(mailbox, context.thread_id, context.latest_message_id, context.anchor_message_id).then(setAssist).catch((reason) => setAssistError(reason instanceof Error ? reason.message : String(reason)))}>Retry</button></p> : null}
            </div>
            {fromAddress || toAddress ? (
              <div className="mail-detail-participants">
                <HeaderContactRow label="From" address={fromAddress} avatarUrl={fromAvatarUrl} />
                <HeaderContactRow label="To" address={toAddress} avatarUrl={toAvatarUrl} />
              </div>
            ) : null}
          </div>
        </header>

        <div className="mail-detail-context" ref={scrollRef}>
          {page?.has_earlier ? <button className="mail-detail-load-earlier" onClick={() => void loadEarlier()}>Load earlier messages</button> : null}
          {loading ? <div className="mail-detail-loading">Loading thread…</div> : null}
          {error ? <div className="mail-detail-error">Thread failed to load. {error}</div> : null}
          {(page?.messages.length ? page.messages : []).map((item) => (
            <article key={item.id} className="mail-thread-message" data-message-id={item.id}>
              <div className="mail-thread-message-head">
                <div className="mail-thread-message-author">
                  {(() => {
                    const sender = senderParts(item.from);
                    const email = sender.email.toLowerCase();
                    return (
                      <>
                        <span className="mail-thread-message-avatar">
                          <ThreadMessageAvatar sender={item.from} avatarUrl={resolvedAvatars[email] || contactAvatars?.[email]} />
                        </span>
                        <strong>{sender.name || sender.email || "Unknown sender"}</strong>
                      </>
                    );
                  })()}
                </div>
                <time>{formatAbsoluteDateTime(item.internal_date)}</time>
              </div>
              {item.body_html ? <SafeEmailHtml className="mail-thread-message-body is-html" html={item.body_html} scaleToFit /> : <SafeEmailText className="mail-thread-message-body" text={item.body_text || ""} />}
              {item.body_truncated ? (
                <p className="mail-thread-message-notice">
                  <span>{displayBodyLoaded.has(item.id) ? "This message exceeds the safe display limit." : "This message is too large to display completely."}</span>
                  {!displayBodyLoaded.has(item.id) ? (
                    <button disabled={displayBodyLoading.has(item.id)} onClick={() => void loadFullDisplayBody(item)}>
                      {displayBodyLoading.has(item.id) ? "Loading…" : "Load full message"}
                    </button>
                  ) : null}
                </p>
              ) : null}
              {displayBodyErrors[item.id] ? <p className="mail-thread-message-notice is-error">{displayBodyErrors[item.id]}</p> : null}
              <AttachmentSection attachments={item.attachments} onPreview={(attachment) => void openAttachment(attachment, "preview")} onDownload={(attachment) => void openAttachment(attachment, "download")} />
            </article>
          ))}
          {showQuickReplies ? (
            <section className="mail-detail-quick-replies" aria-label="Quick reply prompts">
              {assist!.quick_replies.map((item) => (
                <button key={item.id} onClick={() => void submitPrompt(buildQuickReplyPrompt(item))}>
                  <AiSparkleIcon />
                  <span>{item.label}</span>
                </button>
              ))}
            </section>
          ) : null}
        </div>

        <footer className={`mail-detail-footer ${composerOpen ? "is-composer-open" : ""} ${composerExpanded ? "is-expanded" : ""}`}>
          <div className="mail-detail-reply">
            {!composerOpen ? (
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
                    <button onClick={() => setComposerExpanded((value) => !value)}>{composerExpanded ? "Collapse" : "Expand inline"}</button>
                    <button onClick={() => void submitPrompt("Write a first draft reply to the current thread")}>AI draft</button>
                    <button onClick={() => void discardDraft()}>Discard draft</button>
                  </div>
                </div>
                <textarea
                  ref={bodyRef}
                  value={draft}
                  onChange={(event) => {
                    setDraft(event.target.value);
                    setDraftDirty(true);
                  }}
                  placeholder="Write your reply…"
                />
                <div className="mail-detail-composer-actions">
                  <button onClick={() => setDraft((current) => current ? `- ${current}` : "- ")}>Bulleted list</button>
                  <button onClick={() => setDraft((current) => current ? `1. ${current}` : "1. ")}>Numbered list</button>
                  <button onClick={() => setDraft((current) => `${current}${current ? "\n" : ""}https://`)}>Insert link</button>
                  <button className="is-primary" disabled={!draft.trim() || sending} onClick={() => void sendReply()}>{sending ? "Sending…" : "Send"}</button>
                </div>
              </div>
            )}
          </div>
        </footer>
      </aside>

      {previewAttachment ? (
        <div className="attachment-preview-modal" role="dialog" aria-modal="true" aria-label={previewAttachment.filename}>
          <button className="attachment-preview-backdrop" aria-label="Close attachment preview" onClick={() => {
            setPreviewAttachment(null);
            setPreviewUrl("");
            setPreviewText("");
          }} />
          <div className="attachment-preview-sheet">
            <header>
              <strong>{previewableAttachments.findIndex((item) => item.attachment.id === previewAttachment.id) + 1} / {previewableAttachments.length} - {previewAttachment.filename}</strong>
              <div>
                <button onClick={() => previewAttachment && void openAttachment(previewAttachment, "download")}>Download</button>
                <button onClick={() => void Promise.all(previewableAttachments.map((item) => openAttachment(item.attachment, "download")))}>Download all</button>
                <button onClick={() => {
                  setPreviewAttachment(null);
                  setPreviewUrl("");
                  setPreviewText("");
                }}>Close</button>
              </div>
            </header>
            <div className="attachment-preview-body">
              {previewLoading ? <p>Loading preview…</p> : previewAttachment.mime_type === "application/pdf" ? <iframe src={previewUrl} title={previewAttachment.filename} /> : previewAttachment.mime_type.startsWith("image/") ? <img src={previewUrl} alt={previewAttachment.filename} /> : previewAttachment.mime_type.startsWith("audio/") ? <audio src={previewUrl} controls /> : previewAttachment.mime_type.startsWith("video/") ? <video src={previewUrl} controls /> : <pre>{previewText || previewUrl}</pre>}
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
