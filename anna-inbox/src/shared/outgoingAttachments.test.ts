import { describe, expect, it } from "vitest";
import {
  attachmentExtension,
  isSafeOutgoingFilename,
  validateOutgoingAttachment,
} from "./outgoingAttachments";

describe("outgoingAttachments", () => {
  it("extracts only a final filename extension", () => {
    expect(attachmentExtension("report.final.PDF")).toBe("pdf");
    expect(attachmentExtension("README")).toBe("");
    expect(attachmentExtension("archive.tar.gz")).toBe("gz");
  });

  it("rejects paths, control characters, and empty names", () => {
    expect(isSafeOutgoingFilename("folder\\secret.txt")).toBe(false);
    expect(isSafeOutgoingFilename("../secret.txt")).toBe(false);
    expect(isSafeOutgoingFilename("bad\u0000.txt")).toBe(false);
    expect(isSafeOutgoingFilename(" ")).toBe(false);
  });

  it("blocks executable files and enforces per-file and total limits", () => {
    expect(validateOutgoingAttachment({ name: "run.exe", size: 10 })).toEqual({ ok: false, error: "Blocked file type: run.exe" });
    expect(validateOutgoingAttachment({ name: "large.bin", size: 26 * 1024 * 1024 })).toEqual({ ok: false, error: "An attachment cannot exceed 25 MB." });
    expect(validateOutgoingAttachment({ name: "second.bin", size: 1 }, 25 * 1024 * 1024)).toEqual({ ok: false, error: "Total attachments must stay within 25 MB." });
    expect(validateOutgoingAttachment({ name: "notes.txt", size: 10 })).toEqual({ ok: true });
  });
});
