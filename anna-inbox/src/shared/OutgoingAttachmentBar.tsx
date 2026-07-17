import { useRef, type ReactNode } from "react";
import type { OutgoingAttachmentMeta } from "../types/mail";
import {
  formatOutgoingAttachmentSize,
  isImageOutgoingAttachment,
} from "./outgoingAttachments";

const PaperclipIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
  </svg>
);

export function OutgoingAttachmentList({
  items,
  onRemove,
}: {
  items: OutgoingAttachmentMeta[];
  onRemove: (id: string) => void;
}) {
  if (!items.length) return null;
  return (
    <div className="outgoing-attachment-list" aria-label="Attachments">
      {items.map((item) => {
        const isImage = isImageOutgoingAttachment(item) && item.preview_url && item.status === "ready";
        if (isImage) {
          return (
            <div key={item.id} className="outgoing-attachment-image-card">
              <div className="outgoing-attachment-image-toolbar">
                <button type="button" className="outgoing-attachment-more" aria-label="Attachment options" disabled>
                  ···
                </button>
                <button type="button" className="outgoing-attachment-remove-btn" onClick={() => onRemove(item.id)}>
                  Remove
                </button>
              </div>
              <img src={item.preview_url} alt={item.filename} />
            </div>
          );
        }
        return (
          <div key={item.id} className={`outgoing-attachment-chip ${item.status === "error" ? "is-error" : ""}`}>
            <div className="outgoing-attachment-chip-main">
              <strong title={item.filename}>{item.filename}</strong>
              <span>
                {item.status === "uploading"
                  ? "Uploading…"
                  : item.status === "error"
                    ? item.error || "Failed"
                    : formatOutgoingAttachmentSize(item.size)}
              </span>
            </div>
            {item.status === "uploading" ? (
              <div className="outgoing-attachment-progress" aria-hidden="true">
                <div style={{ width: `${Math.round((item.progress || 0) * 100)}%` }} />
              </div>
            ) : null}
            <button type="button" className="outgoing-attachment-chip-remove" aria-label={`Remove ${item.filename}`} onClick={() => onRemove(item.id)}>
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
}

export function OutgoingAttachButton({
  disabled,
  onPick,
  children,
}: {
  disabled?: boolean;
  onPick: (files: FileList) => void;
  children?: ReactNode;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  return (
    <>
      <button
        type="button"
        className="mail-detail-composer-icon-btn"
        aria-label="Attach files"
        data-tooltip="Attach files"
        disabled={disabled}
        onClick={() => inputRef.current?.click()}
      >
        {children || <PaperclipIcon />}
      </button>
      <input
        ref={inputRef}
        type="file"
        multiple
        hidden
        onChange={(event) => {
          const files = event.target.files;
          if (files?.length) onPick(files);
          event.target.value = "";
        }}
      />
    </>
  );
}
