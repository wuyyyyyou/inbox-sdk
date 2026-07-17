import { describe, expect, it } from "vitest";
import {
  mergeInboxMessagesById,
  pruneInboxMessagesToCacheWindow,
  sameInboxMessageContent,
} from "./inboxMessageOrder";

describe("mergeInboxMessagesById", () => {
  it("preserves object identity when content is unchanged", () => {
    const existing = [
      { id: "a", subject: "A", internal_date: "300", label_ids: ["INBOX"] },
      { id: "b", subject: "B", internal_date: "200", label_ids: ["INBOX"] },
    ];
    const incoming = [
      { id: "a", subject: "A", internal_date: "300", label_ids: ["INBOX"] },
      { id: "b", subject: "B", internal_date: "200", label_ids: ["INBOX"] },
    ];
    const merged = mergeInboxMessagesById(existing, incoming);
    expect(merged).toHaveLength(2);
    expect(merged[0]).toBe(existing[0]);
    expect(merged[1]).toBe(existing[1]);
  });

  it("updates only changed messages and keeps others stable", () => {
    const existing = [
      { id: "a", subject: "A", internal_date: "300", label_ids: ["INBOX"], unread: true },
      { id: "b", subject: "B", internal_date: "200", label_ids: ["INBOX"] },
    ];
    const incoming = [
      { id: "a", subject: "A", internal_date: "300", label_ids: ["INBOX"], unread: false },
      { id: "c", subject: "C", internal_date: "400", label_ids: ["INBOX"] },
    ];
    const merged = mergeInboxMessagesById(existing, incoming);
    expect(merged.map((item) => item.id)).toEqual(["c", "a", "b"]);
    expect(merged.find((item) => item.id === "b")).toBe(existing[1]);
    expect(merged.find((item) => item.id === "a")).not.toBe(existing[0]);
    expect(merged.find((item) => item.id === "a")?.unread).toBe(false);
  });
});

describe("sameInboxMessageContent", () => {
  it("treats label order as part of identity only by value join", () => {
    const left = { id: "a", label_ids: ["INBOX", "UNREAD"] };
    const right = { id: "a", label_ids: ["INBOX", "UNREAD"] };
    expect(sameInboxMessageContent(left, right)).toBe(true);
  });
});

describe("pruneInboxMessagesToCacheWindow", () => {
  it("removes in-window ids missing from keepIds", () => {
    const now = Date.now();
    const messages = [
      { id: "keep", internal_date: String(now - 1000) },
      { id: "gone", internal_date: String(now - 2000) },
    ];
    const next = pruneInboxMessagesToCacheWindow(messages, new Set(["keep"]), 7);
    expect(next.map((item) => item.id)).toEqual(["keep"]);
  });

  it("keeps out-of-window messages even when missing from keepIds", () => {
    const now = Date.now();
    const oldTs = now - 40 * 24 * 60 * 60 * 1000;
    const messages = [
      { id: "old-expanded", internal_date: String(oldTs) },
      { id: "gone", internal_date: String(now - 1000) },
    ];
    const next = pruneInboxMessagesToCacheWindow(messages, new Set(), 7);
    expect(next.map((item) => item.id)).toEqual(["old-expanded"]);
  });
});
