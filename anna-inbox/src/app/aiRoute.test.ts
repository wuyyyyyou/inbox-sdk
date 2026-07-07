import { describe, expect, it } from "vitest";
import type { AiChatMessage, AiMailContextRef } from "../types/mail";
import { buildRevisionPrompt, decideAiRoute, resolveMailContext } from "./aiRoute";

const mailContext: AiMailContextRef = {
  kind: "gmail_thread",
  mailbox: "Me@Example.com",
  thread_id: "thread-1",
  anchor_message_id: "message-1",
  latest_message_id: "message-2",
};

const draftMessage: AiChatMessage = {
  id: "assistant-1",
  role: "assistant",
  content: "Drafted.",
  timestamp: "2026-07-07T00:00:00Z",
  mailContext,
  artifact: {
    type: "draft_reply",
    mailbox: "me@example.com",
    thread_id: "thread-1",
    body: "Original draft",
    source_prompt: "Write a reply",
  },
};

describe("decideAiRoute", () => {
  it.each(["Change it", "Make it shorter", "Try again", "Too long"])(
    "routes %s to the current draft",
    (input) => expect(decideAiRoute(input, { messages: [draftMessage] }).kind).toBe("mail_context"),
  );
  it("clarifies a context-dependent command without context", () => {
    expect(decideAiRoute("Change it", { messages: [] }).kind).toBe("clarify");
    expect(decideAiRoute("Do it", { messages: [] }).kind).toBe("clarify");
  });
  it.each(["Find urgent emails", "What needs my reply?", "帮我找一下 Stripe 发票"])(
    "routes %s to scan",
    (input) => expect(decideAiRoute(input, { messages: [] }).kind).toBe("scan"),
  );
  it.each(["你好", "what can you do?", "这是什么意思？"])(
    "routes %s to chat",
    (input) => expect(decideAiRoute(input, { messages: [draftMessage] }).kind).toBe("chat"),
  );
  it("uses the open thread for a reply request", () => {
    expect(decideAiRoute("写一封回复", { messages: [], currentMailContext: mailContext }).kind).toBe("mail_context");
  });
  it("keeps an explicit search request on scan even when it contains a pronoun", () => {
    expect(decideAiRoute("Find this email", { messages: [draftMessage] }).kind).toBe("scan");
  });
});

describe("mail context resolution", () => {
  it("prefers the last assistant artifact over the open thread", () => {
    const other = { ...mailContext, thread_id: "thread-2" };
    expect(resolveMailContext([draftMessage], other)).toEqual({ context: mailContext, draftToRevise: "Original draft" });
  });
  it("quotes the previous draft in the existing visible_prompt parameter", () => {
    const prompt = buildRevisionPrompt("Make it shorter", "Original draft");
    expect(prompt).toContain("Make it shorter");
    expect(prompt).toContain("Original draft");
  });
});
