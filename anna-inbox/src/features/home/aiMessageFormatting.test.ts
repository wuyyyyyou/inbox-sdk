import { describe, expect, it } from "vitest";
import {
  measureAiMessageBlocks,
  parseAiMessageMarkdown,
  sliceAiMessageBlocks,
} from "./aiMessageFormatting";

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

describe("sliceAiMessageBlocks", () => {
  const source = parseAiMessageMarkdown(
    "## Title\n\n1. First item\n2. Second item\n\n- Bullet A\n- Bullet B",
  );

  it("keeps list structure stable while revealing by char budget", () => {
    // Title(5) + First item(10) + Second item(11) + Bullet A(8) + Bullet B(8)
    const total = measureAiMessageBlocks(source);
    expect(total).toBe(42);

    // budget 12 → Title(5) + "First i"(7)
    const mid = sliceAiMessageBlocks(source, 12);
    expect(mid[0]).toEqual({
      type: "heading",
      level: 2,
      content: [{ type: "text", value: "Title" }],
    });
    expect(mid[1]?.type).toBe("ordered_list");
    if (mid[1]?.type === "ordered_list") {
      expect(mid[1].items.length).toBe(1);
      expect(mid[1].items[0]).toEqual([{ type: "text", value: "First i" }]);
    }

    expect(sliceAiMessageBlocks(source, total)).toEqual(source);
    expect(sliceAiMessageBlocks(source, 0)).toEqual([]);
  });

  it("does not invent reordered list markers from incomplete markdown", () => {
    // Incomplete raw markdown would re-parse prefixes as new list shapes;
    // slicing the full tree must keep item 1 before item 2.
    // Alpha(5) + Beta(4) + Gamma(5); budget 8 → Alpha + "Bet"
    const full = parseAiMessageMarkdown("1. Alpha\n2. Beta\n3. Gamma");
    const partial = sliceAiMessageBlocks(full, 8);
    expect(partial).toEqual([
      {
        type: "ordered_list",
        items: [[{ type: "text", value: "Alpha" }], [{ type: "text", value: "Bet" }]],
      },
    ]);
  });
});
