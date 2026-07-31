import { describe, expect, it } from "vitest";
import {
  AUTO_SYNC_OPTIONS,
  DEFAULT_INBOX_SETTINGS,
  clampInboxSettings,
  groupSplitMessages,
  splitInboxMessages,
  splitImportantMessages,
} from "./inboxSettings";

describe("inbox settings", () => {
  it("uses the agreed defaults and clamps invalid values", () => {
    expect(AUTO_SYNC_OPTIONS).toEqual([0, 5, 15, 30, 60]);
    expect(DEFAULT_INBOX_SETTINGS).toMatchObject({
      display_range_days: 30,
      time_section_mode: "detailed",
      stars_enabled: true,
      stars_limit: 10,
      todos_enabled: true,
      todos_limit: 10,
      llm_status_poll_seconds: 60,
      auto_sync_seconds: 5,
    });

    expect(clampInboxSettings({ display_range_days: 9, stars_limit: 1000, llm_status_poll_seconds: 15 as never })).toMatchObject({
      display_range_days: 30,
      stars_limit: 50,
      llm_status_poll_seconds: 60,
    });
    expect(clampInboxSettings({ llm_status_poll_seconds: 0 }).llm_status_poll_seconds).toBe(0);
    expect(clampInboxSettings({ llm_status_poll_seconds: 120 }).llm_status_poll_seconds).toBe(120);
  });

  it("fills defaults for Split fields saved by earlier versions", () => {
    expect(clampInboxSettings({
      custom_categories: [
        { id: "bills", name: "Bills", query: "subject:bill" },
        { id: "invalid", name: "Invalid", query: "from:invalid@example.com", hide_when_empty: "yes", bundling_behavior: "group" },
      ],
    } as never).custom_categories).toEqual([
      { id: "bills", name: "Bills", query: "subject:bill", hide_when_empty: false, bundling_behavior: "default" },
      { id: "invalid", name: "Invalid", query: "from:invalid@example.com", hide_when_empty: false, bundling_behavior: "default" },
    ]);
  });

  it("normalizes missing or non-array custom_categories to an empty list", () => {
    expect(clampInboxSettings({} as never).custom_categories).toEqual([]);
    expect(clampInboxSettings({ custom_categories: null } as never).custom_categories).toEqual([]);
    expect(clampInboxSettings({ custom_categories: {} } as never).custom_categories).toEqual([]);
    expect(() => splitInboxMessages([], { ...DEFAULT_INBOX_SETTINGS, custom_categories: undefined as never })).not.toThrow();
  });

  it("places starred then todo messages before remaining important messages without duplicates", () => {
    const messages = [
      { id: "star", starred: true, important: true },
      { id: "todo", important: true },
      { id: "rest", important: true },
    ];
    const groups = splitImportantMessages(messages, {
      ...DEFAULT_INBOX_SETTINGS,
      stars_limit: 1,
      todos_limit: 1,
    }, new Set(["star", "todo"]));

    expect(groups.map((group) => group.kind)).toEqual(["stars", "todos", "important"]);
    expect(groups.flatMap((group) => group.messages.map((message) => message.id))).toEqual(["star", "todo", "rest"]);
  });

  it("allows custom Splits to overlap while Other excludes every Split match", () => {
    const messages = [
      { id: "important", important: true, from: "Important <important@example.com>" },
      { id: "bill", from: "Billing <billing@example.com>", subject: "Invoice" },
      { id: "report", from: "Billing <billing@example.com>", subject: "Monthly report" },
      { id: "other", from: "Other <other@example.com>", subject: "Hello" },
    ];
    const result = splitInboxMessages(messages, {
      ...DEFAULT_INBOX_SETTINGS,
      custom_categories: [
        { id: "billing", name: "Billing", query: "from:billing", hide_when_empty: false, bundling_behavior: "default" },
        { id: "reports", name: "Reports", query: "subject:report", hide_when_empty: false, bundling_behavior: "none" },
      ],
    });

    expect(result.important.map((message) => message.id)).toEqual(["important"]);
    expect(result.custom.billing.map((message) => message.id)).toEqual(["bill", "report"]);
    expect(result.custom.reports.map((message) => message.id)).toEqual(["report"]);
    expect(result.other.map((message) => message.id)).toEqual(["other"]);
  });

  it("matches custom Splits against an expanded source so is:unread includes todos", () => {
    const plain = [
      { id: "inbox-unread", label_ids: ["INBOX", "UNREAD"], unread: true },
      { id: "inbox-read", label_ids: ["INBOX"], unread: false },
    ];
    const expanded = [
      ...plain,
      { id: "todo-unread", label_ids: ["UNREAD"], unread: true },
    ];
    const result = splitInboxMessages(
      plain,
      {
        ...DEFAULT_INBOX_SETTINGS,
        custom_categories: [
          { id: "unread", name: "Unread", query: "is:unread", hide_when_empty: false, bundling_behavior: "default" },
        ],
      },
      expanded,
    );

    expect(result.custom.unread.map((message) => message.id)).toEqual([
      "inbox-unread",
      "todo-unread",
    ]);
    expect(result.other.map((message) => message.id)).toEqual(["inbox-read"]);
  });

  it("groups a Split by sender or leaves it ungrouped", () => {
    const messages = [
      { id: "first", from: "Alice <alice@example.com>" },
      { id: "second", from: "Bob <bob@example.com>" },
      { id: "third", from: "Alice <alice@example.com>" },
    ];

    expect(groupSplitMessages(messages, "by_sender", "detailed").map((group) => ({ label: group.label, ids: group.messages.map((message) => message.id) }))).toEqual([
      { label: "Alice", ids: ["first", "third"] },
      { label: "Bob", ids: ["second"] },
    ]);
    expect(groupSplitMessages(messages, "none", "detailed")).toEqual([{ label: "", messages }]);
  });
});
