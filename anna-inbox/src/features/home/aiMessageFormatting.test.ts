import { describe, expect, it } from "vitest";
import {
  measureAiMessageBlocks,
  parseAiMessageInline,
  parseAiMessageMarkdown,
  sliceAiMessageBlocks,
} from "./aiMessageFormatting";

describe("parseAiMessageInline", () => {
  it.each([
    ["[THREAD_REF_thread-123]", "thread-123"],
    ["[THREADREF_thread-456]", "thread-456"],
  ])("parses %s as a thread_ref instead of text", (token, threadId) => {
    expect(parseAiMessageInline(`Before ${token} after`)).toEqual([
      { type: "text", value: "Before " },
      { type: "thread_ref", threadId },
      { type: "text", value: " after" },
    ]);
    expect(parseAiMessageInline(`Before ${token} after`).some((node) => node.type === "text" && node.value.includes(token))).toBe(false);
  });

  it("silently removes unavailable thread reference tokens", () => {
    const nodes = parseAiMessageInline("Before [THREADREF_] [THREAD_REF_bad id] after");
    expect(nodes.filter((node) => node.type === "text").map((node) => node.value).join(""))
      .toBe("Before   after");
    expect(nodes.some((node) => node.type === "text" && /\[THREAD(?:_REF)?_/.test(node.value))).toBe(false);
  });
});

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
        indent: 0,
        items: [
          [{ type: "bold", value: "撰写" }, { type: "text", value: "邮件" }],
          [{ type: "link", label: "**官网**", href: "https://example.com/" }],
        ],
      },
      {
        type: "ordered_list",
        indent: 0,
        start: 1,
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
        indent: 0,
        items: [
          [{ type: "text", value: "Access to Enterprise APIs requires monthly fees." }],
          [{ type: "text", value: "The Self-Service team cannot assist with migration." }],
          [{ type: "text", value: "Expect response delays." }],
        ],
      },
    ]);
  });

  it("recognizes a heading appended to the previous sentence", () => {
    expect(parseAiMessageMarkdown("说明已补齐。 ## 邮件线索总结")).toEqual([
      { type: "paragraph", content: [{ type: "text", value: "说明已补齐。" }] },
      { type: "heading", level: 2, content: [{ type: "text", value: "邮件线索总结" }] },
    ]);
  });

  it("parses Shortwave-compatible non-table Markdown blocks", () => {
    expect(parseAiMessageMarkdown("#### Note\n\n*italic* ~~old~~ `code`\n\n> Quote\n\n---\n\n```txt\nexample\n```"))
      .toEqual([
        { type: "heading", level: 4, content: [{ type: "text", value: "Note" }] },
        {
          type: "paragraph",
          content: [
            { type: "italic", value: "italic" },
            { type: "text", value: " " },
            { type: "strikethrough", value: "old" },
            { type: "text", value: " " },
            { type: "code", value: "code" },
          ],
        },
        { type: "blockquote", content: [{ type: "text", value: "Quote" }] },
        { type: "divider" },
        { type: "code_block", language: "txt", code: "example" },
      ]);
  });

  it("splits one-line email metadata into stable display rows", () => {
    expect(parseAiMessageMarkdown("发件人：Johnny Tube 主题：Re: Collaboration invite 时间：7月20日 [THREAD_REF_thread-1]")).toEqual([
      { type: "metadata", label: "发件人", content: [{ type: "text", value: "Johnny Tube" }] },
      { type: "metadata", label: "主题", content: [{ type: "text", value: "Re: Collaboration invite" }] },
      { type: "metadata", label: "时间", content: [{ type: "text", value: "7月20日" }] },
      { type: "metadata", label: "", content: [{ type: "thread_ref", threadId: "thread-1" }] },
    ]);
  });

  it("keeps multiple thread references inline with one finding", () => {
    expect(parseAiMessageMarkdown("- Related conversations [THREAD_REF_thread-1] [THREAD_REF_thread-2]")).toEqual([
      {
        type: "unordered_list",
        indent: 0,
        items: [[
          { type: "text", value: "Related conversations " },
          { type: "thread_ref", threadId: "thread-1" },
          { type: "text", value: " " },
          { type: "thread_ref", threadId: "thread-2" },
        ]],
      },
    ]);
  });

  it("renders an unclosed leading bold marker in email metadata as bold text", () => {
    expect(parseAiMessageMarkdown("发件人： ** Tony (SaneBox)\n主题： ** Kate, Book your SaneBox walkthrough call today!")).toEqual([
      { type: "metadata", label: "发件人", content: [{ type: "bold", value: "Tony (SaneBox)" }] },
      { type: "metadata", label: "主题", content: [{ type: "bold", value: "Kate, Book your SaneBox walkthrough call today!" }] },
    ]);
  });

  it("turns a piped sorting explanation into an ordered priority list", () => {
    expect(parseAiMessageMarkdown("优先级 | 依据 | 1-2 | 需要立即行动 | 3 | 关系维护")).toEqual([
      { type: "heading", level: 2, content: [{ type: "text", value: "排序依据" }] },
      {
        type: "ordered_list",
        indent: 0,
        start: 1,
        items: [
          [{ type: "bold", value: "优先级 1-2" }, { type: "text", value: "：需要立即行动" }],
          [{ type: "bold", value: "优先级 3" }, { type: "text", value: "：关系维护" }],
        ],
      },
    ]);
  });

  it("normalizes host selected-email dumps into one list item per email", () => {
    const raw = [
      "你现在选择了以下 4 封邮件：",
      "",
      "**Re: New Event: kate zhou",
      "11:45am Wed, 22 Jul 2026",
      "Discovery Call**",
      "Security alert",
      "Your Google data is ready to download",
      "Re: Update on the Advanced AI and Automation Solutions feature for Anna AI",
    ].join("\n");
    expect(parseAiMessageMarkdown(raw)).toEqual([
      {
        type: "paragraph",
        content: [{ type: "text", value: "你现在选择了以下 4 封邮件：" }],
      },
      {
        type: "unordered_list",
        indent: 0,
        items: [
          [{ type: "text", value: "Re: New Event: kate zhou 11:45am Wed, 22 Jul 2026 Discovery Call" }],
          [{ type: "text", value: "Security alert" }],
          [{ type: "text", value: "Your Google data is ready to download" }],
          [{ type: "text", value: "Re: Update on the Advanced AI and Automation Solutions feature for Anna AI" }],
        ],
      },
    ]);
  });

  it("keeps appointment date and time in the same email list item", () => {
    const raw = [
      "已逐封评估 3 封邮件：1 封可能需要回复，2 封无需我方操作。待处理邮件:",
      "",
      "- **Founding Full-Stack Engineer Available** 回复 Monika 表示已收到信息，并会在有相关需求或合适人选时进行引荐。跳过主题：Getting started with Claude Cowork, Appointment booked: 1stColab Intro (Kate Zhou) @ Tue Jul 21",
      "  2026 7:30am",
      "- 8am (GMT+8) (kate@anna.partners)。",
    ].join("\n");

    expect(parseAiMessageMarkdown(raw)).toEqual([
      {
        type: "paragraph",
        content: [{ type: "text", value: "已逐封评估 3 封邮件：1 封可能需要回复，2 封无需我方操作。待处理邮件:" }],
      },
      {
        type: "unordered_list",
        indent: 0,
        items: [[{
          type: "bold",
          value: "Founding Full-Stack Engineer Available",
        }, {
          type: "text",
          value: " 回复 Monika 表示已收到信息，并会在有相关需求或合适人选时进行引荐。跳过主题：Getting started with Claude Cowork, Appointment booked: 1stColab Intro (Kate Zhou) @ Tue Jul 21 2026 7:30am 8am (GMT+8) (kate@anna.partners)。",
        }]],
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

    // budget 12 �?Title(5) + "First i"(7)
    const mid = sliceAiMessageBlocks(source, 12);
    expect(mid[0]).toEqual({
      type: "heading",
      level: 2,
      content: [{ type: "text", value: "Title" }],
    });
    expect(mid[1]?.type).toBe("ordered_list");
    if (mid[1]?.type === "ordered_list") {
      expect(mid[1].indent).toBe(0);
      expect(mid[1].start).toBe(1);
      expect(mid[1].items.length).toBe(1);
      expect(mid[1].items[0]).toEqual([{ type: "text", value: "First i" }]);
    }

    expect(sliceAiMessageBlocks(source, total)).toEqual(source);
    expect(sliceAiMessageBlocks(source, 0)).toEqual([]);
  });

  it("does not invent reordered list markers from incomplete markdown", () => {
    // Incomplete raw markdown would re-parse prefixes as new list shapes;
    // slicing the full tree must keep item 1 before item 2.
    // Alpha(5) + Beta(4) + Gamma(5); budget 8 �?Alpha + "Bet"
    const full = parseAiMessageMarkdown("1. Alpha\n2. Beta\n3. Gamma");
    const partial = sliceAiMessageBlocks(full, 8);
    expect(partial).toEqual([
      {
        type: "ordered_list",
        indent: 0,
        start: 1,
        items: [[{ type: "text", value: "Alpha" }], [{ type: "text", value: "Bet" }]],
      },
    ]);
  });
});

describe("split inline lists", () => {
  it("splits inline ordered list items", () => {
    const full = parseAiMessageMarkdown("1. Alpha 2. Beta 3. Gamma");
    expect(full).toHaveLength(1);
    expect(full[0]).toEqual({
      type: "ordered_list",
      indent: 0,
      start: 1,
      items: [
        [{ type: "text", value: "Alpha" }],
        [{ type: "text", value: "Beta" }],
        [{ type: "text", value: "Gamma" }],
      ],
    });
  });

  it("splits inline unordered list items", () => {
    const full = parseAiMessageMarkdown("* Alpha * Beta * Gamma");
    expect(full).toHaveLength(1);
    expect(full[0]).toEqual({
      type: "unordered_list",
      indent: 0,
      items: [
        [{ type: "text", value: "Alpha" }],
        [{ type: "text", value: "Beta" }],
        [{ type: "text", value: "Gamma" }],
      ],
    });
  });

  it("handles unordered lists with leading spaces", () => {
    const full = parseAiMessageMarkdown(" * Alpha * Beta * Gamma");
    expect(full).toHaveLength(1);
    expect(full[0]).toEqual({
      type: "unordered_list",
      indent: 1,
      items: [
        [{ type: "text", value: "Alpha" }],
        [{ type: "text", value: "Beta" }],
        [{ type: "text", value: "Gamma" }],
      ],
    });
  });
});
