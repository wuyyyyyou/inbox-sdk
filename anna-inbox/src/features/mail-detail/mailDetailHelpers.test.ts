import { describe, expect, it, vi } from "vitest";
import {
  buildQuickReplyPrompt,
  estimateAttachmentPreviewMemory,
  hasNewerThreadMessage,
  isOutboundMessageForMailbox,
  isPreviewableAttachment,
  materializeAttachmentAccess,
  matchesDraftArtifact,
  normalizeAttachmentKind,
  parseSnoozeInput,
  resolveAttachmentAccess,
  senderParts,
  splitAddresses,
  triggerAttachmentDownload,
} from "./mailDetailHelpers";

describe("mailDetailHelpers", () => {
  it("only reports a thread update when the known message is actually newer", () => {
    const page = {
      mailbox: "owner@example.com",
      thread_id: "thread-1",
      subject: "Subject",
      latest_message_id: "latest",
      returned_count: 1,
      has_earlier: false,
      next_before_index: null,
      messages: [{
        id: "latest",
        thread_id: "thread-1",
        internal_date: "200",
        from: "sender@example.com",
        to: "owner@example.com",
        subject: "Subject",
        label_ids: [],
        attachments: [],
      }],
    };
    expect(hasNewerThreadMessage(page, "old-draft-anchor", "100")).toBe(false);
    expect(hasNewerThreadMessage(page, "new-feed-message", "300")).toBe(true);
  });

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

  it("throws when attachment access explicitly fails", () => {
    expect(() => resolveAttachmentAccess({
      ok: false,
      error: "Attachment expired",
    })).toThrow("Attachment expired");
  });

  it("downloads url access from the current document without opening a window", () => {
    const click = vi.fn();
    const remove = vi.fn();
    const appendChild = vi.fn();
    const link = { href: "", download: "", rel: "", style: { display: "" }, click, remove };
    const ownerDocument = {
      createElement: vi.fn(() => link),
      body: { appendChild },
    } as unknown as Document;
    triggerAttachmentDownload({
      kind: "url",
      url: "https://files.example.test/file.pdf",
      filename: "file.pdf",
      mimeType: "application/pdf",
      externalPreview: false,
    }, undefined, ownerDocument);
    expect(link.href).toBe("https://files.example.test/file.pdf");
    expect(link.download).toBe("file.pdf");
    expect(link.style.display).toBe("none");
    expect(appendChild).toHaveBeenCalledWith(link);
    expect(click).toHaveBeenCalledOnce();
    expect(remove).toHaveBeenCalledOnce();
  });

  it("downloads blob access from the current document", () => {
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
    triggerAttachmentDownload({
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
});
