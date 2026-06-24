import { describe, expect, it } from "vitest";
import { contactContextLines, normalizeEmailSummaryItems, uniqueLines } from "./summaryContent";

describe("summary content helpers", () => {
  it("filters structured thread summary JSON from visible summary items", () => {
    expect(normalizeEmailSummaryItems([
      "Naveen replied on Jun 24, 2026, accepting the invite.",
      "{\"thread_kind\":\"long_thread\",\"headline\":\"Judge fee\",\"reply_focus\":\"Confirm budget\",\"related_context\":[],\"should_show\":true,\"confidence\":\"low\"}",
    ])).toEqual([
      "Naveen replied on Jun 24, 2026, accepting the invite.",
    ]);
  });

  it("keeps the first meaningful line from multiline summary content", () => {
    expect(normalizeEmailSummaryItems("  \nFirst line\nSecond line")).toEqual(["First line"]);
  });

  it("formats contact context topics into readable lines", () => {
    expect(contactContextLines({
      relevant_topics: [
        { title: "Hackathon", summary: "Accepted judge invite", open_loop: "Needs fee approval" },
      ],
    })).toEqual([
      "Hackathon · Accepted judge invite · Needs fee approval",
    ]);
  });

  it("deduplicates lines case-insensitively", () => {
    expect(uniqueLines(["Hello", "hello ", "World"])).toEqual(["Hello", "World"]);
  });
});
