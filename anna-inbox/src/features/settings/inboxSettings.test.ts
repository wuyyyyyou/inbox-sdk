import { describe, expect, it } from "vitest";
import {
  DEFAULT_INBOX_SETTINGS,
  clampInboxSettings,
  splitImportantMessages,
} from "./inboxSettings";

describe("inbox settings", () => {
  it("uses the agreed defaults and clamps invalid values", () => {
    expect(DEFAULT_INBOX_SETTINGS).toMatchObject({
      display_range_days: 30,
      time_section_mode: "detailed",
      stars_enabled: true,
      stars_limit: 10,
      todos_enabled: true,
      todos_limit: 10,
    });

    expect(clampInboxSettings({ display_range_days: 9, stars_limit: 1000 })).toMatchObject({
      display_range_days: 30,
      stars_limit: 50,
    });
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
});
