import { describe, expect, it } from "vitest";
import { resolveMailboxSelection } from "./mailboxSelection";

describe("resolveMailboxSelection", () => {
  it("保留平台返回的多个已选邮箱，而不是只留下第一个", () => {
    const result = resolveMailboxSelection({
      mailboxes: [
        { email: "first@example.test", authorized: true, selected: true },
        { email: "second@example.test", authorized: true, selected: true },
      ],
      selected: ["first@example.test", "second@example.test"],
      fallback: "fallback@example.test",
    });

    expect(result.selected).toEqual(["first@example.test", "second@example.test"]);
    expect(result.primary).toBe("first@example.test");
    expect(result.mailboxes.map((mailbox) => mailbox.selected)).toEqual([true, true]);
  });
});
