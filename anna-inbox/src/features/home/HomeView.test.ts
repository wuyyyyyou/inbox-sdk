import { describe, expect, it } from "vitest";
import { accountDisplayName, aiSearchStatus, gmailAuthorizationError, gmailTrashUrl, hasMailboxScanError, isAiConversationNearBottom, isDoneMessage, isDraftMessage, isGmailAuthorizationRequired, isImportantMessage, isSentMessage, isStarredMessage, isTrashMessage, mergeDraftOverlayMessages, messageParticipant, resolveSourceMessages, senderParts, shouldShowImportantIcon } from "./HomeView";

describe("gmailAuthorizationError", () => {
  it("includes the source returned by the authorization check", () => {
    expect(gmailAuthorizationError("none")).toBe(
      "No authorized mailbox detected (source: none).",
    );
    expect(gmailAuthorizationError("runtime")).toBe(
      "No authorized mailbox detected (source: runtime).",
    );
  });
});

describe("isGmailAuthorizationRequired", () => {
  it("distinguishes missing authorization from an auth-check error", () => {
    expect(isGmailAuthorizationRequired({ checked: true, authorized: false, source: "none" })).toBe(true);
    expect(isGmailAuthorizationRequired({ checked: true, authorized: false, source: "error" })).toBe(false);
    expect(isGmailAuthorizationRequired({ checked: true, authorized: true, source: "runtime" })).toBe(false);
  });
});

describe("gmailTrashUrl", () => {
  it("targets Trash for the selected Gmail account", () => {
    expect(gmailTrashUrl(" user+work@example.com ")).toBe(
      "https://mail.google.com/mail/?authuser=user%2Bwork%40example.com#trash",
    );
  });
});

describe("isAiConversationNearBottom", () => {
  it("allows a small layout tolerance at the bottom", () => {
    expect(isAiConversationNearBottom({ scrollHeight: 1000, scrollTop: 376, clientHeight: 600 })).toBe(true);
  });

  it("detects when the user has scrolled away from new messages", () => {
    expect(isAiConversationNearBottom({ scrollHeight: 1000, scrollTop: 300, clientHeight: 600 })).toBe(false);
  });
});

describe("aiSearchStatus", () => {
  it("uses Chinese status copy for Chinese scan results", () => {
    expect(aiSearchStatus({ title: "收件箱整理", sections: [{ items: [{ subject: "Update" }] }] }))
      .toBe("找到 1 个相关邮件线程。");
  });

  it("keeps English status copy for English scan results", () => {
    expect(aiSearchStatus({ title: "Inbox summary", sections: [{ items: [{ subject: "Update" }] }] }))
      .toBe("Found 1 relevant thread.");
  });
});

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
  it("projects trashed mail only into Trash while preserving its Gmail labels", () => {
    const trashed = {
      id: "trashed-1",
      label_ids: ["TRASH", "STARRED", "IMPORTANT", "SENT"],
      internal_date: "300",
    };
    const flags = {
      todos: [trashed.id],
      snoozed: [trashed.id],
      done: [trashed.id],
      doneRemoved: [],
      drafts: [],
      saved: { [trashed.id]: trashed },
    };

    expect(isTrashMessage(trashed)).toBe(true);
    expect(resolveSourceMessages("trash", [], [trashed], flags)).toEqual([trashed]);
    for (const view of ["inbox", "todos", "starred", "snoozed", "done", "drafts", "sent", "all"] as const) {
      expect(resolveSourceMessages(view, [], [trashed], flags)).toEqual([]);
    }
    expect(trashed.label_ids).toEqual(["TRASH", "STARRED", "IMPORTANT", "SENT"]);
  });

  it("excludes Gmail and local drafts from Trash", () => {
    const gmailDraft = { id: "gmail-draft", label_ids: ["TRASH", "DRAFT"], internal_date: "300" };
    const localDraft = { id: "local-draft", label_ids: ["TRASH"], internal_date: "200", draft_local: true };
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: {} };

    expect(resolveSourceMessages("trash", [], [gmailDraft, localDraft], flags)).toEqual([]);
  });

  it("uses current Gmail state instead of a stale saved workflow copy", () => {
    const saved = { id: "message-1", label_ids: ["INBOX"], internal_date: "100" };
    const current = { ...saved, label_ids: ["TRASH", "STARRED"] };
    const flags = { todos: [saved.id], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: { [saved.id]: saved } };

    expect(resolveSourceMessages("todos", [], [current], flags)).toEqual([]);
  });

  it("keeps a drafted starred message in both category projections", () => {
    const base = [{ id: "message-1", thread_id: "thread-1", label_ids: ["INBOX", "STARRED"], subject: "Hello" }];
    const drafts = [{ id: "message-1", thread_id: "thread-1", label_ids: ["INBOX", "STARRED", "DRAFT"], draft_local: true, draft_body: "Reply" }];
    const merged = mergeDraftOverlayMessages(base, drafts);
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: {} };

    expect(resolveSourceMessages("starred", [], merged, flags).map((message) => message.id)).toEqual(["message-1"]);
    expect(resolveSourceMessages("drafts", [], merged, flags).map((message) => message.id)).toEqual(["message-1"]);
  });

  it("preserves the inbox message direction when applying a draft from the same thread", () => {
    const inboxMessage = {
      id: "received-1",
      thread_id: "thread-1",
      mailbox: "owner@example.com",
      from: "World of AI <team@worldofai.example>",
      to: "Owner <owner@example.com>",
      label_ids: ["INBOX"],
      internal_date: "100",
    };
    const draft = {
      id: "sent-1",
      thread_id: "thread-1",
      mailbox: "owner@example.com",
      from: "Owner <owner@example.com>",
      to: "World of AI <team@worldofai.example>",
      draft_body: "Thanks for the update.",
      draft_local: true,
      label_ids: ["DRAFT", "SENT"],
      internal_date: "200",
    };

    const [merged] = mergeDraftOverlayMessages([inboxMessage], [draft]);

    expect(merged.from).toBe(inboxMessage.from);
    expect(merged.to).toBe(inboxMessage.to);
    expect(merged.draft_body).toBe(draft.draft_body);
    expect(isSentMessage(merged)).toBe(false);
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

  it("keeps only the latest message for a thread projection", () => {
    const inbox = [
      { id: "thread-1-old", thread_id: "thread-1", label_ids: ["INBOX"], internal_date: "1719360000000", subject: "Re: Feature Invite" },
      { id: "thread-1-new", thread_id: "thread-1", label_ids: ["INBOX"], internal_date: "1720051200000", subject: "Re: Feature Invite" },
      { id: "thread-2", thread_id: "thread-2", label_ids: ["INBOX"], internal_date: "1719446400000", subject: "Other" },
    ];
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: {} };

    expect(resolveSourceMessages("inbox", inbox, [], flags).map((message) => message.id)).toEqual(["thread-1-new", "thread-2"]);
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
