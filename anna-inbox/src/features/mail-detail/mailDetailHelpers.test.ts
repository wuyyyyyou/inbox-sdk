import { describe, expect, it, vi } from "vitest";
import {
  buildForwardDraftBody,
  buildForwardSendBodies,
  buildForwardSubject,
  buildQuickReplyPrompt,
  deriveReplyAllRecipients,
  estimateAttachmentPreviewMemory,
  attachmentFallbackMetadata,
  isOutboundMessageForMailbox,
  isPreviewableAttachment,
  materializeAttachmentAccess,
  matchesDraftArtifact,
  mergeDraftArtifactBody,
  normalizeAttachmentKind,
  resolveMessageThreadId,
  parseSnoozeInput,
  resolveAttachmentAccess,
  senderParts,
  shouldFollowLatestThreadMessage,
  splitAddresses,
  stripForwardedMessageBlock,
  stripQuotedReplyForDisplay,
  triggerAttachmentDownload,
} from "./mailDetailHelpers";

describe("mailDetailHelpers", () => {
  it("splits address lists without breaking display names", () => {
    expect(splitAddresses('"Anna Team" <team@anna.ai>, Bob <bob@example.com>')).toEqual([
      '"Anna Team" <team@anna.ai>',
      "Bob <bob@example.com>",
    ]);
  });

  it("parses sender name and email", () => {
    expect(senderParts("Alice Example <alice@example.com>")).toEqual({
      name: "Alice Example",
      email: "alice@example.com",
    });
  });

  it("derives Reply All recipients without including the active mailbox", () => {
    expect(deriveReplyAllRecipients({
      id: "m1",
      thread_id: "t1",
      internal_date: "1",
      from: "Alice <ALICE@example.com>",
      to: "Inbox <inbox@example.com>, Bob <bob@example.com>",
      cc: "Carol <carol@example.com>, alice@example.com",
      subject: "Hello",
      label_ids: [],
      attachments: [],
    }, "INBOX@example.com")).toEqual({
      to: "ALICE@example.com",
      cc: ["bob@example.com", "carol@example.com"],
    });
  });

  it("parses relative snooze input", () => {
    const now = new Date("2026-07-02T08:00:00.000Z");
    expect(parseSnoozeInput("4 hours", now)?.toISOString()).toBe("2026-07-02T12:00:00.000Z");
  });

  it("matches draft artifacts against mailbox and thread", () => {
    expect(matchesDraftArtifact("Inbox@Example.com", "thread-1", {
      type: "draft_reply",
      mailbox: "inbox@example.com",
      thread_id: "thread-1",
      body: "Draft",
      source_prompt: "Prompt",
    })).toBe(true);
  });

  it("falls back to the message id when a thread id is unavailable", () => {
    expect(resolveMessageThreadId({ id: "message-1" })).toBe("message-1");
    expect(resolveMessageThreadId({ id: "message-1", thread_id: "thread-1" })).toBe("thread-1");
  });

  it("follows a newer cached-thread refresh only when the reader is still at the bottom", () => {
    expect(shouldFollowLatestThreadMessage("old", "new", true, false)).toBe(true);
    expect(shouldFollowLatestThreadMessage("old", "new", false, false)).toBe(false);
    expect(shouldFollowLatestThreadMessage("old", "new", true, true)).toBe(false);
    expect(shouldFollowLatestThreadMessage("new", "new", true, false)).toBe(false);
  });

  it("appends generated drafts with an explicit paragraph break", () => {
    expect(mergeDraftArtifactBody("Existing draft\n", "Generated draft", "append"))
      .toBe("Existing draft\n\nGenerated draft");
    expect(mergeDraftArtifactBody("Existing draft", "Generated draft", "replace"))
      .toBe("Generated draft");
  });

  it("builds the visible quick-reply prompt", () => {
    expect(buildQuickReplyPrompt({ id: "confirm", label: "Sounds good", intent: "Confirm the plan." })).toContain("Sounds good");
  });

  it("detects outbound messages from the active mailbox", () => {
    expect(isOutboundMessageForMailbox("Kate Zhou <kate@anna.partners>", "KATE@anna.partners")).toBe(true);
    expect(isOutboundMessageForMailbox("World of AI <hello@example.com>", "kate@anna.partners")).toBe(false);
  });

  it("treats pdf filename as previewable when mime is generic", () => {
    expect(normalizeAttachmentKind({
      filename: "proposal.pdf",
      mime_type: "application/octet-stream",
    })).toBe("pdf");
    expect(isPreviewableAttachment({
      id: "a1",
      message_id: "m1",
      filename: "proposal.pdf",
      mime_type: "application/octet-stream",
      size: 1,
      source: "gmail",
      downloadable: true,
    })).toBe(true);
  });

  it("supports preview for text, audio, and video attachments", () => {
    expect(normalizeAttachmentKind({ filename: "notes.txt", mime_type: "text/plain" })).toBe("text");
    expect(normalizeAttachmentKind({ filename: "recording.mp3", mime_type: "audio/mpeg" })).toBe("audio");
    expect(normalizeAttachmentKind({ filename: "demo.mp4", mime_type: "video/mp4" })).toBe("video");
  });

  it("does not preview unsupported attachment types", () => {
    expect(normalizeAttachmentKind({ filename: "archive.zip", mime_type: "application/zip" })).toBe("download");
    expect(normalizeAttachmentKind({ filename: "archive.pdf", mime_type: "application/zip" })).toBe("download");
    expect(isPreviewableAttachment({
      id: "a2",
      message_id: "m1",
      filename: "archive.zip",
      mime_type: "application/zip",
      size: 1,
      source: "gmail",
      downloadable: true,
    })).toBe(false);
  });

  it("classifies office, calendar, and archive downloads with fallback metadata", () => {
    expect(attachmentFallbackMetadata({ filename: "plan.docx", mime_type: "application/octet-stream" })).toEqual({
      category: "office", label: "Office document", previewable: false,
    });
    expect(attachmentFallbackMetadata({ filename: "invite.ics", mime_type: "text/calendar" })).toEqual({
      category: "calendar", label: "Calendar file", previewable: false,
    });
    expect(attachmentFallbackMetadata({ filename: "backup.tar.gz", mime_type: "application/gzip" })).toEqual({
      category: "archive", label: "Archive", previewable: false,
    });
  });

  it("detects common media and textual extensions when MIME metadata is generic", () => {
    expect(normalizeAttachmentKind({ filename: "camera.heic", mime_type: "application/octet-stream" })).toBe("image");
    expect(normalizeAttachmentKind({ filename: "meeting.m4a", mime_type: "application/octet-stream" })).toBe("audio");
    expect(normalizeAttachmentKind({ filename: "calendar.ics", mime_type: "application/octet-stream" })).toBe("text");
  });

  it("estimates bounded preview memory for background rendering", () => {
    const megabyte = 1024 * 1024;
    expect(estimateAttachmentPreviewMemory({ filename: "small.pdf", mime_type: "application/pdf", size: 1000 })).toBe(12 * megabyte);
    expect(estimateAttachmentPreviewMemory({ filename: "large.pdf", mime_type: "application/pdf", size: 20 * megabyte })).toBe(48 * megabyte);
    expect(estimateAttachmentPreviewMemory({ filename: "photo.png", mime_type: "image/png", size: 2 * megabyte })).toBe(8 * megabyte);
  });

  it("resolves inline attachment access into a revocable blob url", () => {
    const previousWindow = (globalThis as { window?: unknown }).window;
    (globalThis as { window?: { atob: (value: string) => string } }).window = {
      atob: (value: string) => {
        expect(value).toBe("dGlueQ==");
        return "tiny";
      },
    };
    const createObjectURL = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:test");
    const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    try {
      const access = resolveAttachmentAccess({
        ok: true,
        delivery: "inline",
        mode: "preview",
        filename: "tiny.txt",
        mime_type: "text/plain",
        content_b64: "dGlueQ==",
      });
      expect(access.kind).toBe("blob");
      expect(access.url).toBe("blob:test");
      expect(access.externalPreview).toBe(false);
      access.revoke?.();
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:test");
    } finally {
      (globalThis as { window?: unknown }).window = previousWindow;
      createObjectURL.mockRestore();
      revokeObjectURL.mockRestore();
    }
  });

  it("falls back to download url for preview when preview url is missing", () => {
    const access = resolveAttachmentAccess({
      ok: true,
      delivery: "url",
      mode: "preview",
      filename: "report.pdf",
      mime_type: "application/pdf",
      download_url: "https://files.example.test/report.pdf",
    });
    expect(access.kind).toBe("url");
    expect(access.url).toContain("report.pdf");
    expect(access.externalPreview).toBe(true);
  });

  it("prefers the dedicated preview url over the download url", () => {
    const access = resolveAttachmentAccess({
      ok: true,
      delivery: "url",
      mode: "preview",
      filename: "report.pdf",
      mime_type: "application/pdf",
      preview_url: "http://127.0.0.1:8123/preview/token",
      download_url: "http://127.0.0.1:8123/download/token/report.pdf",
    });
    expect(access.url).toBe("http://127.0.0.1:8123/preview/token");
    expect(access.sourceUrl).toBe("http://127.0.0.1:8123/preview/token");
    expect(access.externalPreview).toBe(false);
  });

  it("materializes url access into a blob url", async () => {
    const previousFetch = globalThis.fetch;
    const createObjectURL = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:materialized");
    const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      blob: async () => new Blob(["pdf"], { type: "application/pdf" }),
    })) as unknown as typeof fetch;
    try {
      const access = await materializeAttachmentAccess({
        kind: "url",
        url: "http://127.0.0.1/file.pdf",
        sourceUrl: "http://127.0.0.1/file.pdf",
        filename: "file.pdf",
        mimeType: "application/pdf",
        externalPreview: false,
      });
      expect(access.kind).toBe("blob");
      expect(access.url).toBe("blob:materialized");
      expect(access.sourceUrl).toBe("http://127.0.0.1/file.pdf");
      access.revoke?.();
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:materialized");
    } finally {
      globalThis.fetch = previousFetch;
      createObjectURL.mockRestore();
      revokeObjectURL.mockRestore();
    }
  });

  it("retries transient attachment responses once before succeeding", async () => {
    const previousFetch = globalThis.fetch;
    const createObjectURL = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:retry");
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 503 })
      .mockResolvedValueOnce({ ok: true, blob: async () => new Blob(["pdf"], { type: "application/pdf" }) });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    try {
      const access = await materializeAttachmentAccess({
        kind: "url",
        url: "https://files.example.test/retry.pdf",
        filename: "retry.pdf",
        mimeType: "application/pdf",
        externalPreview: false,
      });
      expect(access.url).toBe("blob:retry");
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      globalThis.fetch = previousFetch;
      createObjectURL.mockRestore();
    }
  });

  it("retries once on an ERR_CONNECTION_CLOSED network failure", async () => {
    const previousFetch = globalThis.fetch;
    const createObjectURL = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:conn-retry");
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new Error("net::ERR_CONNECTION_CLOSED"))
      .mockResolvedValueOnce({ ok: true, blob: async () => new Blob(["pdf"], { type: "application/pdf" }) });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    try {
      const access = await materializeAttachmentAccess({
        kind: "url",
        url: "https://files.example.test/conn.pdf",
        filename: "conn.pdf",
        mimeType: "application/pdf",
        externalPreview: false,
      });
      expect(access.url).toBe("blob:conn-retry");
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      globalThis.fetch = previousFetch;
      createObjectURL.mockRestore();
    }
  });

  it("gives up after two failed transient attempts", async () => {
    const previousFetch = globalThis.fetch;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 502 })
      .mockResolvedValueOnce({ ok: false, status: 502 });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    try {
      await expect(materializeAttachmentAccess({
        kind: "url",
        url: "https://files.example.test/always-fails.pdf",
        filename: "always-fails.pdf",
        mimeType: "application/pdf",
        externalPreview: false,
      })).rejects.toThrow("Attachment fetch failed with 502");
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      globalThis.fetch = previousFetch;
    }
  });

  it("preserves caller cancellation instead of retrying", async () => {
    const previousFetch = globalThis.fetch;
    const controller = new AbortController();
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      controller.abort("cancelled by caller");
      (init?.signal as AbortSignal).throwIfAborted();
      return { ok: true, blob: async () => new Blob(["never"]) };
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    try {
      await expect(materializeAttachmentAccess({
        kind: "url",
        url: "https://files.example.test/cancel.pdf",
        filename: "cancel.pdf",
        mimeType: "application/pdf",
        externalPreview: false,
      }, controller.signal)).rejects.toBe("cancelled by caller");
      expect(fetchMock).toHaveBeenCalledOnce();
    } finally {
      globalThis.fetch = previousFetch;
    }
  });

  it("throws when attachment access explicitly fails", () => {
    expect(() => resolveAttachmentAccess({
      ok: false,
      error: "Attachment expired",
    })).toThrow("Attachment expired");
  });

  it("materializes url access before downloading and releases the blob url later", async () => {
    const click = vi.fn();
    const remove = vi.fn();
    const appendChild = vi.fn();
    const link = { href: "", download: "", rel: "", style: { display: "" }, click, remove };
    const ownerDocument = {
      createElement: vi.fn(() => link),
      body: { appendChild },
    } as unknown as Document;
    const previousFetch = globalThis.fetch;
    const createObjectURL = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:download");
    const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const setTimeout = vi.spyOn(globalThis, "setTimeout").mockImplementation(((callback: TimerHandler) => {
      if (typeof callback === "function") callback();
      return 1 as unknown as number;
    }) as typeof window.setTimeout);
    globalThis.fetch = vi.fn(async () => ({ ok: true, blob: async () => new Blob(["file"]) })) as unknown as typeof fetch;
    try {
      await triggerAttachmentDownload({
      kind: "url",
      url: "https://files.example.test/file.pdf",
      filename: "file.pdf",
      mimeType: "application/pdf",
      externalPreview: false,
      }, undefined, ownerDocument);
      expect(link.href).toBe("blob:download");
      expect(link.download).toBe("file.pdf");
      expect(link.style.display).toBe("none");
      expect(appendChild).toHaveBeenCalledWith(link);
      expect(click).toHaveBeenCalledOnce();
      expect(remove).toHaveBeenCalledOnce();
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:download");
    } finally {
      globalThis.fetch = previousFetch;
      createObjectURL.mockRestore();
      revokeObjectURL.mockRestore();
      setTimeout.mockRestore();
    }
  });

  it("throws when a url download response is not ok", async () => {
    const previousFetch = globalThis.fetch;
    const fetchMock = vi.fn(async () => ({ ok: false, status: 403 }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    try {
      await expect(triggerAttachmentDownload({
        kind: "url",
        url: "https://files.example.test/file.pdf",
        filename: "file.pdf",
        mimeType: "application/pdf",
        externalPreview: false,
      }, undefined, {
        createElement: vi.fn(),
      } as unknown as Document)).rejects.toThrow("Attachment fetch failed with 403");
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      globalThis.fetch = previousFetch;
    }
  });

  it("downloads blob access from the current document", async () => {
    const click = vi.fn();
    const remove = vi.fn();
    const appendChild = vi.fn();
    const ownerDocument = {
      body: { appendChild },
      createElement: (tagName: string) => {
        expect(tagName).toBe("a");
        return {
          href: "",
          download: "",
          rel: "",
          style: { display: "" },
          click,
          remove,
        };
      },
    } as unknown as Document;
    await triggerAttachmentDownload({
      kind: "blob",
      url: "blob:file",
      filename: "file.pdf",
      mimeType: "application/pdf",
      externalPreview: false,
    }, undefined, ownerDocument);
    expect(appendChild).toHaveBeenCalledOnce();
    expect(click).toHaveBeenCalledOnce();
    expect(remove).toHaveBeenCalledOnce();
  });

  it("builds forward subjects without duplicating prefixes", () => {
    expect(buildForwardSubject("Hello")).toMatch(/^(Fwd: |转发：)Hello$/);
    expect(buildForwardSubject("Fwd: Hello")).toBe("Fwd: Hello");
    expect(buildForwardSubject("转发：你好")).toBe("转发：你好");
  });

  it("builds and strips forwarded message blocks", () => {
    const body = buildForwardDraftBody({
      from: "Alice <a@example.com>",
      to: "bob@example.com",
      subject: "Hello",
      internal_date: String(Date.UTC(2026, 0, 2, 8, 30)),
      body_text: "Original body",
      body_html: "",
    }, "Please see below.");
    expect(body).toContain("Please see below.");
    expect(body).toMatch(/Forwarded message|转发的邮件/);
    expect(body).toContain("Original body");
    expect(stripForwardedMessageBlock(body)).toBe("Please see below.");
  });

  it("removes quoted reply content from stale plain-text bodies", () => {
    const cleaned = stripQuotedReplyForDisplay(
      "I will wait for your approval.\n\nOn Thu, 28 May 2026, 7:04 am Kate <kate@example.com> wrote:\n>>>>>>>> Best regards\n>>>>>>>> Older content",
    );
    expect(cleaned).toBe("I will wait for your approval.");
    expect(stripQuotedReplyForDisplay("> A Markdown quote remains.\nCurrent note.")).toContain("> A Markdown quote remains.");
  });

  it("preserves original html when building forward send bodies", () => {
    const result = buildForwardSendBodies({
      from: "Alice <a@example.com>",
      to: "bob@example.com",
      subject: "Hello",
      internal_date: String(Date.UTC(2026, 0, 2, 8, 30)),
      body_text: "Original body",
      body_html: "<p><strong>Original</strong> <em>body</em></p>",
    }, "Please see below.");
    expect(result.body).toContain("Please see below.");
    expect(result.body_html).toContain("<strong>Original</strong>");
    expect(result.body_html).toContain("Please see below.");
    expect(result.body_html).toMatch(/Forwarded message|转发的邮件/);
  });
});
