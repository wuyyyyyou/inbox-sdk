import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useApp } from "../../app/AppContext";
import { OutgoingAttachButton, OutgoingAttachmentList } from "../../shared/OutgoingAttachmentBar";
import {
  isBlockedOutgoingFilename,
  isImageOutgoingAttachment,
  OUTGOING_ATTACHMENT_TOTAL_MAX_BYTES,
  putFileToUploadUrl,
  toPersistedOutgoingAttachments,
  totalOutgoingAttachmentBytes,
} from "../../shared/outgoingAttachments";
import { RecipientChipInput } from "../../shared/RecipientChipInput";
import { RichTextEditor, plainTextToEditorHtml } from "../mail-detail/RichTextEditor";
import type {
  ComposeDraft,
  ComposeDraftArtifact,
  OutgoingAttachmentMeta,
} from "../../types/mail";

const ToolbarIcon = ({ children }: { children: ReactNode }) => (
  <svg
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
const CloseIcon = () => (
  <ToolbarIcon>
    <path d="m8 6 6 6-6 6M14 6l6 6-6 6" />
  </ToolbarIcon>
);
const TrashIcon = () => (
  <ToolbarIcon>
    <path d="M5 7h14M9 7V4h6v3M8 7l.8 13h6.4L16 7" />
  </ToolbarIcon>
);
const AiDraftIcon = () => (
  <ToolbarIcon>
    <path d="M14.5 4.5 19.5 9.5" />
    <path d="M5 15.5 15.5 5a2.1 2.1 0 0 1 3 3L8 18.5 4 20l1-4.5Z" />
    <path d="M19 16v4" />
    <path d="M17 18h4" />
  </ToolbarIcon>
);
const SaveDraftIcon = () => (
  <ToolbarIcon>
    <path d="M5 4h14v16H5z" />
    <path d="M8 4v6h8V4M8 20v-6h8v6" />
  </ToolbarIcon>
);

export function ComposeView({
  mailbox,
  initialDraft,
  open,
  onClose,
  onViewDrafts,
  onScheduleSend,
  insertRequest,
  onConsumeInsertRequest,
  onOpenAiDraft,
}: {
  mailbox: string;
  initialDraft?: ComposeDraft | null;
  open: boolean;
  onClose: () => void;
  onViewDrafts: () => void;
  onScheduleSend: (draft: ComposeDraft) => void;
  insertRequest?: { nonce: string; artifact: ComposeDraftArtifact } | null;
  onConsumeInsertRequest?: (nonce: string) => void;
  onOpenAiDraft: (
    draft: Pick<ComposeDraft, "recipients" | "cc" | "bcc" | "subject" | "body">,
  ) => void;
}) {
  const { actions } = useApp();
  const [draftId, setDraftId] = useState(initialDraft?.id || "");
  const [etag, setEtag] = useState(initialDraft?.etag || "");
  const [recipients, setRecipients] = useState<string[]>(
    initialDraft?.recipients || [],
  );
  const [cc, setCc] = useState<string[]>(initialDraft?.cc || []);
  const [bcc, setBcc] = useState<string[]>(initialDraft?.bcc || []);
  // 各自独立：点 Cc 开 Cc 行，再点 Bcc 再开 Bcc 行
  const [ccOpen, setCcOpen] = useState(Boolean(initialDraft?.cc?.length));
  const [bccOpen, setBccOpen] = useState(Boolean(initialDraft?.bcc?.length));
  const [focusField, setFocusField] = useState<"cc" | "bcc" | null>(null);
  const [subject, setSubject] = useState(initialDraft?.subject || "");
  const [body, setBody] = useState(initialDraft?.body || "");
  const [bodyHtml, setBodyHtml] = useState(initialDraft?.body_html || plainTextToEditorHtml(initialDraft?.body || ""));
  const [attachments, setAttachments] = useState<OutgoingAttachmentMeta[]>(
    () => (initialDraft?.attachments || []).map((item) => ({ ...item, status: "ready" as const })),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [exitWithoutSavingConfirmation, setExitWithoutSavingConfirmation] =
    useState(false);

  const canSend =
    recipients.length > 0 &&
    subject.trim().length > 0 &&
    body.trim().length > 0 &&
    !attachments.some((item) => item.status === "uploading");
  const isEmpty =
    !recipients.length &&
    !cc.length &&
    !bcc.length &&
    !subject.trim() &&
    !body.trim() &&
    !attachments.length;

  useEffect(() => {
    if (!open) return;
    setDraftId(initialDraft?.id || "");
    setEtag(initialDraft?.etag || "");
    setRecipients(initialDraft?.recipients || []);
    setCc(initialDraft?.cc || []);
    setBcc(initialDraft?.bcc || []);
    setCcOpen(Boolean(initialDraft?.cc?.length));
    setBccOpen(Boolean(initialDraft?.bcc?.length));
    setFocusField(null);
    setSubject(initialDraft?.subject || "");
    setBody(initialDraft?.body || "");
    setBodyHtml(initialDraft?.body_html || plainTextToEditorHtml(initialDraft?.body || ""));
    setAttachments((initialDraft?.attachments || []).map((item) => ({ ...item, status: "ready" as const })));
    setSaving(false);
    setError("");
    setExitWithoutSavingConfirmation(false);
  }, [initialDraft, open]);

  // 恢复草稿图片预览 URL（local stage 或 APS Files）
  useEffect(() => {
    if (!open || !mailbox) return;
    let cancelled = false;
    const restore = async () => {
      const next = await Promise.all(
        attachments.map(async (item) => {
          if (!isImageOutgoingAttachment(item) || item.preview_url || !item.storage_key) return item;
          try {
            const access = await actions.prepareStagedOutgoingAttachmentAccess(
              mailbox,
              item.storage_key,
              item.filename,
              item.mime_type,
            );
            const url = access.preview_url || access.download_url || "";
            return url ? { ...item, preview_url: url, status: "ready" as const } : item;
          } catch {
            return item;
          }
        }),
      );
      if (!cancelled) setAttachments(next);
    };
    void restore();
    return () => {
      cancelled = true;
    };
    // 仅在打开/草稿切换时恢复预览
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mailbox, initialDraft?.id]);

  useEffect(() => {
    if (!insertRequest || !open) return;
    setBody(insertRequest.artifact.body);
    setBodyHtml(plainTextToEditorHtml(insertRequest.artifact.body));
    onConsumeInsertRequest?.(insertRequest.nonce);
  }, [insertRequest, onConsumeInsertRequest, open]);

  const draftInput = useMemo(
    () => ({
      id: draftId || undefined,
      recipients,
      cc,
      bcc,
      subject,
      body,
      body_html: bodyHtml,
      attachments: toPersistedOutgoingAttachments(attachments),
    }),
    [attachments, bcc, body, bodyHtml, cc, draftId, recipients, subject],
  );

  const stageFiles = async (files: FileList) => {
    const list = Array.from(files);
    if (!list.length) return;
    setError("");
    let runningTotal = totalOutgoingAttachmentBytes(attachments.filter((item) => item.status !== "error"));
    for (const file of list) {
      if (isBlockedOutgoingFilename(file.name)) {
        setError(`Blocked file type: ${file.name}`);
        continue;
      }
      if (runningTotal + file.size > OUTGOING_ATTACHMENT_TOTAL_MAX_BYTES) {
        setError("Total attachments must stay within 25 MB.");
        break;
      }
      const tempId = crypto.randomUUID().replace(/-/g, "");
      const localPreview = isImageOutgoingAttachment({ filename: file.name, mime_type: file.type })
        ? URL.createObjectURL(file)
        : "";
      setAttachments((current) => [
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
          draft_scope: "compose",
          draft_key: draftId || "",
        });
        if (!begun.ok || !begun.upload_url || !begun.attachment_id || !begun.storage_key) {
          throw new Error(begun.error || "Failed to stage attachment");
        }
         await putFileToUploadUrl(begun.upload_url, file, begun.upload_headers || {}, (ratio) => {
          setAttachments((current) =>
            current.map((item) => (item.id === tempId ? { ...item, progress: ratio } : item)),
          );
         });
         await actions.completeStageOutgoingAttachment(mailbox, begun.storage_key, file.size, file.type || "application/octet-stream");
        runningTotal += file.size;
        setAttachments((current) =>
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
        const message = reason instanceof Error ? reason.message : String(reason);
        setAttachments((current) =>
          current.map((item) =>
            item.id === tempId ? { ...item, status: "error", error: message, progress: 0 } : item,
          ),
        );
        setError(message);
      }
    }
  };

  const removeAttachment = async (id: string) => {
    const target = attachments.find((item) => item.id === id);
    setAttachments((current) => current.filter((item) => item.id !== id));
    if (target?.preview_url?.startsWith("blob:")) URL.revokeObjectURL(target.preview_url);
    if (target?.storage_key) {
      try {
        await actions.deleteStagedOutgoingAttachment(mailbox, target.storage_key);
      } catch {
        // ignore cleanup failure
      }
    }
  };

  const save = async () => {
    if (isEmpty) return null;
    setSaving(true);
    setError("");
    try {
      const saved = await actions.saveComposeDraft(
        mailbox,
        draftInput,
        etag || undefined,
      );
      setDraftId(saved.id);
      setEtag(saved.etag || "");
      return saved;
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      if (draftId && message.includes("changed elsewhere")) {
        try {
          const latestPayload = await actions.listComposeDrafts(mailbox);
          const latest = latestPayload.drafts.find((item) => item.id === draftId);
          if (latest) {
            const saved = await actions.saveComposeDraft(
              mailbox,
              draftInput,
              latest.etag || undefined,
            );
            setDraftId(saved.id);
            setEtag(saved.etag || "");
            return saved;
          }
        } catch (retryReason) {
          setError(retryReason instanceof Error ? retryReason.message : String(retryReason));
          return null;
        }
      }
      setError(message);
      return null;
    } finally {
      setSaving(false);
    }
  };

  const close = async () => {
    if (!recipients.length && (subject.trim() || body.trim() || cc.length || bcc.length || attachments.length)) {
      setExitWithoutSavingConfirmation(true);
      return;
    }
    if (!isEmpty) {
      await save();
    }
    onClose();
  };

  const saveToDrafts = async () => {
    if (isEmpty) {
      actions.showToast(
        "Add a recipient, subject, or body before saving a draft.",
      );
      return;
    }
    const saved = await save();
    if (!saved) return;
    actions.showToast("Draft saved.", {
      actionLabel: "View",
      onAction: onViewDrafts,
    });
    onClose();
  };

  const discard = async () => {
    if (draftId) await actions.deleteComposeDraft(mailbox, draftId);
    onClose();
  };

  const send = async () => {
    if (!canSend) {
      setError("Add a valid recipient, subject, and content before sending.");
      return;
    }
    const saved = await save();
    if (saved) onScheduleSend(saved);
  };

  const ccBccButtons = (
    <div className="compose-cc-bcc-toggle">
      {!ccOpen ? (
        <button
          type="button"
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => {
            setCcOpen(true);
            setFocusField("cc");
          }}
        >
          Cc
        </button>
      ) : null}
      {!bccOpen ? (
        <button
          type="button"
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => {
            setBccOpen(true);
            setFocusField("bcc");
          }}
        >
          Bcc
        </button>
      ) : null}
    </div>
  );

  return (
    <>
      <aside
        className={`compose-view mail-detail-drawer ${open ? "is-open" : ""}`}
        aria-hidden={!open}
        role="dialog"
        aria-modal="true"
        aria-label="Compose email"
      >
        <header className="mail-detail-header compose-header">
          <div className="mail-detail-toolbar">
            <button
              type="button"
              onClick={() => void close()}
              aria-label="Close compose"
              data-tooltip="Close compose"
            >
              <CloseIcon />
            </button>
            <button
              type="button"
              onClick={() => void discard()}
              aria-label="Discard draft"
              data-tooltip="Discard draft"
              disabled={saving}
            >
              <TrashIcon />
            </button>
          </div>
          <div className="mail-detail-summary">
            <h2>{draftId ? "Edit draft" : "New email"}</h2>
            <p className="compose-header-copy">
              Compose an email from {mailbox}
            </p>
          </div>
        </header>
        <RecipientChipInput
          label="To"
          emails={recipients}
          onChange={setRecipients}
          mailbox={mailbox}
          searchContacts={actions.searchComposeContacts}
          placeholder="Name or email"
          fieldRole="to"
          trailing={!ccOpen || !bccOpen ? ccBccButtons : null}
        />
        {ccOpen ? (
          <RecipientChipInput
            label="Cc"
            emails={cc}
            onChange={setCc}
            mailbox={mailbox}
            searchContacts={actions.searchComposeContacts}
            fieldRole="cc"
            autoFocus={focusField === "cc"}
            onEmptyBlur={() => {
              setCc([]);
              setCcOpen(false);
              setFocusField(null);
            }}
          />
        ) : null}
        {bccOpen ? (
          <RecipientChipInput
            label="Bcc"
            emails={bcc}
            onChange={setBcc}
            mailbox={mailbox}
            searchContacts={actions.searchComposeContacts}
            fieldRole="bcc"
            autoFocus={focusField === "bcc"}
            onEmptyBlur={() => {
              setBcc([]);
              setBccOpen(false);
              setFocusField(null);
            }}
          />
        ) : null}
        <label className="compose-field">
          <span>Subject</span>
          <input
            value={subject}
            onChange={(event) => setSubject(event.target.value)}
            placeholder="Add a subject"
          />
        </label>
        <div className="compose-body">
          <span>Content</span>
          <RichTextEditor
            value={bodyHtml}
            onChange={(value) => {
              setBody(value.text);
              setBodyHtml(value.html);
            }}
            placeholder="Write your message or key points…"
          />
        </div>
        <OutgoingAttachmentList items={attachments} onRemove={(id) => void removeAttachment(id)} />
        {error ? (
          <p className="compose-error" role="alert">
            {error}
          </p>
        ) : null}
        <footer className="mail-detail-footer compose-footer">
          <span />
          <div className="mail-detail-composer-actions compose-toolbar-actions">
            <OutgoingAttachButton disabled={saving} onPick={(files) => void stageFiles(files)} />
            <button
              type="button"
              className="mail-detail-composer-icon-btn"
              aria-label="AI draft"
              data-tooltip="AI draft"
              disabled={saving}
              onClick={() =>
                onOpenAiDraft({ recipients, cc, bcc, subject, body })
              }
            >
              <AiDraftIcon />
            </button>
            <button
              type="button"
              className="mail-detail-composer-icon-btn"
              aria-label="Save to drafts"
              data-tooltip="Save to drafts"
              disabled={saving}
              onClick={() => void saveToDrafts()}
            >
              <SaveDraftIcon />
            </button>
            <button
              type="button"
              className="is-primary compose-send-btn"
              onClick={() => void send()}
              disabled={saving || !canSend}
            >
              {saving ? "Saving…" : "Send"}
            </button>
          </div>
        </footer>
      </aside>
      {exitWithoutSavingConfirmation ? (
        <div
          className="confirm-overlay"
          role="presentation"
          onClick={() => setExitWithoutSavingConfirmation(false)}
        >
          <section
            className="confirm-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="compose-exit-without-saving-title"
            aria-describedby="compose-exit-without-saving-description"
            onClick={(event) => event.stopPropagation()}
          >
            <h3 id="compose-exit-without-saving-title">Cannot save draft</h3>
            <p id="compose-exit-without-saving-description">
              Add a recipient to save this draft. Exit without saving?
            </p>
            <div className="confirm-actions">
              <button
                type="button"
                className="soft-btn"
                onClick={() => setExitWithoutSavingConfirmation(false)}
              >
                Keep editing
              </button>
              <button
                type="button"
                className="primary-btn danger-btn"
                onClick={() => {
                  setExitWithoutSavingConfirmation(false);
                  onClose();
                }}
              >
                Exit without saving
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </>
  );
}
