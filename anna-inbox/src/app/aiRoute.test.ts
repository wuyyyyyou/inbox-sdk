import { describe, expect, it } from "vitest";
import type { AiChatMessage, AiMailContextRef } from "../types/mail";
import { buildRevisionPrompt, buildScanFollowupRequest, decideAiRoute, resolveMailContext } from "./aiRoute";

const mailContext: AiMailContextRef = {
  kind: "gmail_thread",
  mailbox: "Me@Example.com",
  thread_id: "thread-1",
  anchor_message_id: "message-1",
  latest_message_id: "message-2",
};

const composeContext: AiMailContextRef = {
  kind: "compose",
  session_id: "compose-session-1",
  mailbox: "me@example.com",
  recipients: ["reader@example.com"],
  subject: "Hello",
  body: "",
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
  it("routes Compose first-draft and improvement prompts through the Compose context", () => {
    expect(decideAiRoute("Write a first draft about Hello", { messages: [], currentMailContext: composeContext }).kind).toBe("mail_context");
    expect(decideAiRoute("Suggest changes to improve my draft", { messages: [], currentMailContext: { ...composeContext, body: "Hi." } }).kind).toBe("mail_context");
  });
  it("keeps an explicit search request on scan even when it contains a pronoun", () => {
    expect(decideAiRoute("Find this email", { messages: [draftMessage] }).kind).toBe("scan");
  });
  it("keeps a time constraint in the preceding inbox scan", () => {
    const messages: AiChatMessage[] = [{
      id: "scan-1",
      role: "user",
      content: "找出最近需要回复的邮件",
      timestamp: "2026-07-07T00:00:00Z",
      kind: "scan",
    }];
    expect(decideAiRoute("最近五天内", { messages }).kind).toBe("scan");
    expect(buildScanFollowupRequest(messages, "最近五天内")).toContain("找出最近需要回复的邮件");
    expect(buildScanFollowupRequest(messages, "最近五天内")).toContain("最近五天内");
  });
  it("reuses the preceding request when the user continues a scan", () => {
    const messages: AiChatMessage[] = [{
      id: "scan-1",
      role: "user",
      content: "Find unanswered follow-ups",
      timestamp: "2026-07-07T00:00:00Z",
      kind: "scan",
    }];
    expect(decideAiRoute("继续", { messages }).kind).toBe("scan");
    expect(buildScanFollowupRequest(messages, "继续")).toBe("Find unanswered follow-ups");
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
  it("keeps Compose context separate from Gmail thread identifiers", () => {
    expect(resolveMailContext([], composeContext)).toEqual({ context: composeContext, draftToRevise: "" });
    expect("thread_id" in composeContext).toBe(false);
  });
});
