import { describe, expect, it } from "vitest";
import {
  buildQuickReplyPrompt,
  isOutboundMessageForMailbox,
  matchesDraftArtifact,
  parseSnoozeInput,
  senderParts,
  splitAddresses,
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
});
