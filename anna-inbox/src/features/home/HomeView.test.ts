import { describe, expect, it } from "vitest";
import { accountDisplayName, hasMailboxScanError, isDoneMessage, isDraftMessage, isImportantMessage, isSentMessage, isStarredMessage, mergeDraftOverlayMessages, messageParticipant, resolveSourceMessages, senderParts, shouldShowImportantIcon } from "./HomeView";

describe("senderParts", () => {
  it("accepts null sender values from Gmail Trash", () => {
    expect(senderParts(null)).toEqual({ name: "Unknown sender", email: "" });
  });

  it("parses display names and addresses", () => {
    expect(senderParts("Jane Doe <jane@example.com>")).toEqual({ name: "Jane Doe", email: "jane@example.com" });
  });
});

describe("accountDisplayName", () => {
  it("prefers Google profile names for account labels", () => {
    expect(accountDisplayName({ email: "kateq@anna.partners", display_name: "KateQ Zhou" })).toBe("KateQ Zhou");
  });

  it("falls back to the mailbox local-part when no profile name is available", () => {
    expect(accountDisplayName({ email: "kateq@anna.partners" })).toBe("kateq");
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
    expect(messageParticipant({ id: "draft", from: null, to: null, draft_local: true }, "all", "owner@example.com").name).toBe("me");
  });

  it("shows drafts as authored by me instead of listing recipients", () => {
    const participant = messageParticipant({
      id: "draft-with-recipient", from: "Owner <owner@example.com>", to: "Helena <helena@example.com>", draft_local: true,
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
    expect(isDraftMessage({ id: "gmail-draft", label_ids: ["draft"] })).toBe(false);
    expect(isDraftMessage({ id: "local-draft", draft_local: true })).toBe(true);
  });

  it("uses the original sender as the reply recipient for inbound local drafts", () => {
    const participant = messageParticipant({
      id: "draft-inbound", from: "Sahra <sahra@example.com>", to: "owner@example.com", draft_local: true,
    }, "drafts", "owner@example.com");
    expect(participant.name).toBe("me, Sahra");
    expect(participant.email).toBe("sahra@example.com");
  });

  it("keeps the important icon visible on outgoing draft rows", () => {
    expect(shouldShowImportantIcon(true, true, true)).toBe(true);
    expect(shouldShowImportantIcon(true, true, false)).toBe(false);
  });
});

describe("resolveSourceMessages", () => {
  it("keeps a drafted starred message in both category projections", () => {
    const base = [{ id: "message-1", thread_id: "thread-1", label_ids: ["INBOX", "STARRED"], subject: "Hello" }];
    const drafts = [{ id: "message-1", thread_id: "thread-1", label_ids: ["INBOX", "STARRED", "DRAFT"], draft_local: true, draft_body: "Reply" }];
    const merged = mergeDraftOverlayMessages(base, drafts);
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: {} };

    expect(resolveSourceMessages("starred", [], merged, flags).map((message) => message.id)).toEqual(["message-1"]);
    expect(resolveSourceMessages("drafts", [], merged, flags).map((message) => message.id)).toEqual(["message-1"]);
  });

  it("prefers live inbox messages over stale snapshot data in inbox view", () => {
    const liveInbox = [
      { id: "live-1", label_ids: ["INBOX", "IMPORTANT"], internal_date: "200" },
      { id: "live-2", label_ids: ["INBOX"], internal_date: "100" },
    ];
    const staleSnapshot = [
      { id: "stale-1", label_ids: ["INBOX", "IMPORTANT"], internal_date: "300" },
    ];
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: {} };

    expect(resolveSourceMessages("inbox", liveInbox, staleSnapshot, flags).map((message) => message.id)).toEqual(["live-1", "live-2"]);
  });

  it("keeps all-mail results in strict reverse chronological order", () => {
    const snapshot = [
      { id: "older", label_ids: ["INBOX"], internal_date: "100" },
      { id: "newest", label_ids: ["INBOX"], internal_date: "300" },
      { id: "middle", label_ids: ["INBOX"], internal_date: "200" },
    ];
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: {} };

    expect(resolveSourceMessages("all", [], snapshot, flags).map((message) => message.id)).toEqual(["newest", "middle", "older"]);
  });

  it("treats sent mail as done by default", () => {
    const snapshot = [
      { id: "sent-1", label_ids: ["SENT"], internal_date: "300" },
      { id: "done-1", label_ids: ["INBOX"], internal_date: "200" },
    ];
    const flags = { todos: [], snoozed: [], done: ["done-1"], doneRemoved: [], drafts: [], saved: {} };

    expect(resolveSourceMessages("done", [], snapshot, flags).map((message) => message.id)).toEqual(["sent-1", "done-1"]);
    expect(isSentMessage(snapshot[0])).toBe(true);
    expect(isDoneMessage(snapshot[0], flags)).toBe(true);
  });

  it("treats mailbox-authored mail as sent even without an explicit SENT label", () => {
    const sentMessage = {
      id: "sent-implicit",
      mailbox: "owner@example.com",
      from: "Owner <owner@example.com>",
      label_ids: ["INBOX"],
      internal_date: "300",
    };
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: {} };

    expect(isSentMessage(sentMessage)).toBe(true);
    expect(isDoneMessage(sentMessage, flags)).toBe(true);
  });

  it("lets sent mail be moved back out of done", () => {
    const sentMessage = { id: "sent-1", label_ids: ["SENT"], internal_date: "300" };
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: ["sent-1"], drafts: [], saved: {} };

    expect(resolveSourceMessages("done", [], [sentMessage], flags)).toEqual([]);
    expect(isDoneMessage(sentMessage, flags)).toBe(false);
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
