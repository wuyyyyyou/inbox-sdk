import { describe, expect, it } from "vitest";
import { replaceInboxSplit } from "./SplitsManager";

describe("SplitsManager helpers", () => {
  it("replaces an existing Split query without changing other Splits", () => {
    const splits = [
      { id: "reports", name: "Reports", query: "is:unread", hide_when_empty: false, bundling_behavior: "default" as const },
      { id: "billing", name: "Billing", query: "from:billing", hide_when_empty: true, bundling_behavior: "by_sender" as const },
    ];

    expect(replaceInboxSplit(splits, { ...splits[0], query: "subject:report" })).toEqual([
      { ...splits[0], query: "subject:report" },
      splits[1],
    ]);
  });
});
