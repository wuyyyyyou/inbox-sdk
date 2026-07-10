import { describe, expect, it } from "vitest";
import { parseAiMessageMarkdown } from "./aiMessageFormatting";

describe("parseAiMessageMarkdown", () => {
  it("parses the supported blocks and inline content", () => {
    expect(parseAiMessageMarkdown("## 邮件相关\n\n- **撰写**邮件\n- [**官网**](https://example.com/)\n\n1. [THREAD_REF_thread-123]")).toEqual([
      {
        type: "heading",
        level: 2,
        content: [{ type: "text", value: "邮件相关" }],
      },
      {
        type: "unordered_list",
        items: [
          [{ type: "bold", value: "撰写" }, { type: "text", value: "邮件" }],
          [{ type: "link", label: "**官网**", href: "https://example.com/" }],
        ],
      },
      {
        type: "ordered_list",
        items: [[{ type: "thread_ref", threadId: "thread-123" }]],
      },
    ]);
  });

  it("leaves unsupported URLs as text", () => {
    expect(parseAiMessageMarkdown("[local](javascript:alert(1))")).toEqual([
      {
        type: "paragraph",
        content: [{ type: "text", value: "[local](javascript:alert(1))" }],
      },
    ]);
  });

  it("splits multiple unordered items written on one line", () => {
    expect(parseAiMessageMarkdown("* Access to Enterprise APIs requires monthly fees. * The Self-Service team cannot assist with migration. * Expect response delays.")).toEqual([
      {
        type: "unordered_list",
        items: [
          [{ type: "text", value: "Access to Enterprise APIs requires monthly fees." }],
          [{ type: "text", value: "The Self-Service team cannot assist with migration." }],
          [{ type: "text", value: "Expect response delays." }],
        ],
      },
    ]);
  });
});
