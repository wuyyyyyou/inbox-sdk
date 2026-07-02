import { describe, expect, it } from "vitest";
import { hasMailboxScanError, isDraftMessage, isImportantMessage, isStarredMessage, messageParticipant, senderParts } from "./HomeView";

describe("senderParts", () => {
  it("accepts null sender values from Gmail Trash", () => {
    expect(senderParts(null)).toEqual({ name: "Unknown sender", email: "" });
  });

  it("parses display names and addresses", () => {
    expect(senderParts("Jane Doe <jane@example.com>")).toEqual({ name: "Jane Doe", email: "jane@example.com" });
  });
});

describe("messageParticipant", () => {
  it("shows me and recipients for sent messages in All mail", () => {
    const participant = messageParticipant({
      id: "sent-1", from: "Owner <owner@example.com>", to: '"Doe, Jane" <jane@example.com>, Team <team@example.com>', label_ids: ["SENT"],
    }, "all", "owner@example.com");
    expect(participant.name).toBe("me, Doe, Jane, Team");
    expect(participant.email).toBe("jane@example.com");
  });

  it("uses a stable fallback for messages without a From header", () => {
    expect(messageParticipant({ id: "broken", from: null, to: "owner@example.com" }, "all", "owner@example.com").name).toBe("No sender");
  });

  it("labels recipient-less drafts without showing Unknown sender", () => {
    expect(messageParticipant({ id: "draft", from: null, to: null, label_ids: ["DRAFT"] }, "all", "owner@example.com").name).toBe("me");
  });

  it("shows drafts as authored by me instead of listing recipients", () => {
    const participant = messageParticipant({
      id: "draft-with-recipient", from: "Owner <owner@example.com>", to: "Helena <helena@example.com>", label_ids: ["DRAFT"],
    }, "drafts", "owner@example.com");
    expect(participant.name).toBe("me, Helena");
    expect(participant.title).toBe("Helena <helena@example.com>");
  });

  it("uses the same me-plus-recipient format outside All mail for sent items", () => {
    const participant = messageParticipant({
      id: "sent-folder-1", from: "Owner <owner@example.com>", to: "Mail <mail@example.com>", label_ids: ["SENT"],
    }, "inbox", "owner@example.com");
    expect(participant.name).toBe("me, Mail");
    expect(participant.email).toBe("mail@example.com");
  });
});

describe("cached message label fallbacks", () => {
  it("recognizes important and starred labels without DTO booleans", () => {
    const cachedMessage = { id: "cached", label_ids: ["INBOX", "IMPORTANT", "STARRED"] };
    expect(isImportantMessage(cachedMessage)).toBe(true);
    expect(isStarredMessage(cachedMessage)).toBe(true);
    expect(isDraftMessage({ id: "draft", label_ids: ["draft"] })).toBe(true);
  });
});

describe("hasMailboxScanError", () => {
  it("treats explicit scan errors as failed state", () => {
    expect(hasMailboxScanError(undefined, undefined, "scan crashed")).toBe(true);
  });

  it("treats mailbox status failure markers as failed state", () => {
    expect(hasMailboxScanError("failed")).toBe(true);
    expect(hasMailboxScanError("error")).toBe(true);
    expect(hasMailboxScanError("done")).toBe(false);
  });
});
