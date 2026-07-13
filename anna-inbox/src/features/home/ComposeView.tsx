import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useApp } from "../../app/AppContext";
import type { ComposeContact, ComposeDraft, ComposeDraftArtifact } from "../../types/mail";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const ToolbarIcon = ({ children }: { children: ReactNode }) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>;
const CloseIcon = () => <ToolbarIcon><path d="m8 6 6 6-6 6M14 6l6 6-6 6" /></ToolbarIcon>;
const TrashIcon = () => <ToolbarIcon><path d="M5 7h14M9 7V4h6v3M8 7l.8 13h6.4L16 7" /></ToolbarIcon>;
const AiDraftIcon = () => <ToolbarIcon><path d="M14.5 4.5 19.5 9.5" /><path d="M5 15.5 15.5 5a2.1 2.1 0 0 1 3 3L8 18.5 4 20l1-4.5Z" /><path d="M19 16v4" /><path d="M17 18h4" /></ToolbarIcon>;
const SaveDraftIcon = () => <ToolbarIcon><path d="M5 4h14v16H5z" /><path d="M8 4v6h8V4M8 20v-6h8v6" /></ToolbarIcon>;

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
  onOpenAiDraft: (draft: Pick<ComposeDraft, "recipients" | "subject" | "body">) => void;
}) {
  const { actions } = useApp();
  const [draftId, setDraftId] = useState(initialDraft?.id || "");
  const [etag, setEtag] = useState(initialDraft?.etag || "");
  const [recipients, setRecipients] = useState<string[]>(initialDraft?.recipients || []);
  const [recipientInput, setRecipientInput] = useState("");
  const [subject, setSubject] = useState(initialDraft?.subject || "");
  const [body, setBody] = useState(initialDraft?.body || "");
  const [contacts, setContacts] = useState<ComposeContact[]>([]);
  const [contactOpen, setContactOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [exitWithoutSavingConfirmation, setExitWithoutSavingConfirmation] = useState(false);
  const recipientInputRef = useRef<HTMLInputElement | null>(null);

  const canSend = recipients.length > 0 && subject.trim().length > 0 && body.trim().length > 0;
  const isEmpty = !recipients.length && !subject.trim() && !body.trim();
  const candidateEmail = recipientInput.trim().toLowerCase();
  const canAddRawEmail = EMAIL_RE.test(candidateEmail) && !recipients.includes(candidateEmail);

  useEffect(() => {
    if (!open) return;
    setDraftId(initialDraft?.id || "");
    setEtag(initialDraft?.etag || "");
    setRecipients(initialDraft?.recipients || []);
    setRecipientInput("");
    setSubject(initialDraft?.subject || "");
    setBody(initialDraft?.body || "");
    setContacts([]);
    setContactOpen(false);
    setSaving(false);
    setError("");
    setExitWithoutSavingConfirmation(false);
  }, [initialDraft, open]);

  useEffect(() => {
    if (!insertRequest || !open) return;
    setBody(insertRequest.artifact.body);
    onConsumeInsertRequest?.(insertRequest.nonce);
  }, [insertRequest, onConsumeInsertRequest, open]);

  useEffect(() => {
    if (!recipientInput.trim()) {
      setContacts([]);
      return;
    }
    const timer = window.setTimeout(() => {
      void actions.searchComposeContacts(mailbox, recipientInput).then((result) => {
        setContacts(result.contacts);
        setContactOpen(true);
      }).catch(() => setContacts([]));
    }, 220);
    return () => window.clearTimeout(timer);
  }, [actions, mailbox, recipientInput]);

  const addRecipient = (email: string) => {
    const normalized = email.trim().toLowerCase();
    if (!EMAIL_RE.test(normalized) || recipients.includes(normalized)) return;
    setRecipients((current) => [...current, normalized]);
    setRecipientInput("");
    setContacts([]);
    setContactOpen(false);
    recipientInputRef.current?.focus();
  };

  const draftInput = useMemo(() => ({
    id: draftId || undefined,
    recipients,
    subject,
    body,
  }), [body, draftId, recipients, subject]);

  const save = async () => {
    if (isEmpty) return null;
    setSaving(true);
    setError("");
    try {
      const saved = await actions.saveComposeDraft(mailbox, draftInput, etag || undefined);
      setDraftId(saved.id);
      setEtag(saved.etag || "");
      return saved;
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message);
      return null;
    } finally {
      setSaving(false);
    }
  };

  const close = async () => {
    if (!recipients.length && (subject.trim() || body.trim())) {
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
      actions.showToast("Add a recipient, subject, or message before saving a draft.");
      return;
    }
    const saved = await save();
    if (!saved) return;
    actions.showToast("Draft saved.", { actionLabel: "View", onAction: onViewDrafts });
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

  return (
    <>
      <aside className={`compose-view mail-detail-drawer ${open ? "is-open" : ""}`} aria-hidden={!open} role="dialog" aria-modal="true" aria-label="Compose email">
      <header className="mail-detail-header compose-header">
        <div className="mail-detail-toolbar"><button type="button" onClick={() => void close()} aria-label="Close compose" data-tooltip="Close compose"><CloseIcon /></button><button type="button" onClick={() => void discard()} aria-label="Discard draft" data-tooltip="Discard draft" disabled={saving}><TrashIcon /></button></div>
        <div className="mail-detail-summary"><h2>{draftId ? "Edit draft" : "New message"}</h2><p className="compose-header-copy">Compose an email from {mailbox}</p></div>
      </header>
      <label className="compose-field compose-to"><span>To</span><div>
        {recipients.map((email) => <span className="compose-chip" key={email}>{email}<button type="button" aria-label={`Remove ${email}`} onClick={() => setRecipients((items) => items.filter((item) => item !== email))}>×</button></span>)}
        <input ref={recipientInputRef} aria-label="Recipients" value={recipientInput} onFocus={() => setContactOpen(true)} onBlur={() => window.setTimeout(() => setContactOpen(false), 120)} onChange={(event) => setRecipientInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && canAddRawEmail) { event.preventDefault(); addRecipient(candidateEmail); } }} placeholder={recipients.length ? undefined : "Name or email"} />
        {contactOpen && (contacts.length > 0 || canAddRawEmail) ? <div className="compose-contact-menu">{contacts.map((contact) => <button type="button" key={contact.email} onMouseDown={(event) => event.preventDefault()} onClick={() => addRecipient(contact.email)}>{contact.avatar_url ? <img src={contact.avatar_url} alt="" /> : null}<span>{contact.name || contact.email}<small>{contact.name ? contact.email : ""}</small></span></button>)}{canAddRawEmail ? <button type="button" onMouseDown={(event) => event.preventDefault()} onClick={() => addRecipient(candidateEmail)}>Add <strong>{candidateEmail}</strong></button> : null}</div> : null}
      </div></label>
      <label className="compose-field"><span>Subject</span><input value={subject} onChange={(event) => setSubject(event.target.value)} placeholder="Add a subject" /></label>
      <label className="compose-body"><span>Content</span><textarea value={body} onChange={(event) => setBody(event.target.value)} placeholder="Write your message or key points…" /></label>
      {error ? <p className="compose-error" role="alert">{error}</p> : null}
      <footer className="mail-detail-footer compose-footer"><span /><div className="mail-detail-composer-actions compose-toolbar-actions"><button type="button" className="mail-detail-composer-icon-btn" aria-label="AI draft" data-tooltip="Ask Anna to draft or improve this message" disabled={saving} onClick={() => onOpenAiDraft({ recipients, subject, body })}><AiDraftIcon /></button><button type="button" className="mail-detail-composer-icon-btn" aria-label="Save to drafts" data-tooltip="Save to drafts" disabled={saving} onClick={() => void saveToDrafts()}><SaveDraftIcon /></button><button type="button" className="is-primary compose-send-btn" onClick={() => void send()} disabled={saving || !canSend}>{saving ? "Saving…" : "Send"}</button></div></footer>
      </aside>
      {exitWithoutSavingConfirmation ? <div className="confirm-overlay" role="presentation" onClick={() => setExitWithoutSavingConfirmation(false)}>
        <section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="compose-exit-without-saving-title" aria-describedby="compose-exit-without-saving-description" onClick={(event) => event.stopPropagation()}>
          <h3 id="compose-exit-without-saving-title">Cannot save draft</h3>
          <p id="compose-exit-without-saving-description">Add a recipient to save this draft. Exit without saving?</p>
          <div className="confirm-actions"><button type="button" className="soft-btn" onClick={() => setExitWithoutSavingConfirmation(false)}>Keep editing</button><button type="button" className="primary-btn danger-btn" onClick={() => { setExitWithoutSavingConfirmation(false); onClose(); }}>Exit without saving</button></div>
        </section>
      </div> : null}
    </>
  );
}
