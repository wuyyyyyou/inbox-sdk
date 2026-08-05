import { describe, expect, it } from "vitest";
import { applyInboxQuerySuggestion, getInboxQueryHighlightTerms, getInboxQuerySuggestions, matchInboxQuery, parseInboxQuery, splitInboxQueryTokens } from "./inboxQuery";

describe("Inbox query language", () => {
  it("parses AND before OR", () => {
    const parsed = parseInboxQuery("subject:invoice OR from:alice AND body:paid");

    expect(parsed).toMatchObject({
      expression: {
        kind: "or",
        terms: [
          { kind: "term", field: "subject", value: "invoice" },
          {
            kind: "and",
            terms: [
              { kind: "term", field: "from", value: "alice" },
              { kind: "term", field: "body", value: "paid" },
            ],
          },
        ],
      },
    });
  });

  it("matches individual fields and bare terms case-insensitively", () => {
    const message = {
      id: "mail-1",
      from: "Alice Example <alice@example.com>",
      to: "Owner <owner@example.com>",
      subject: "Invoice is ready",
      snippet: "Payment received",
      body_preview: "Paid in full",
      body_cached: true,
    };

    expect(matchInboxQuery(message, parseInboxQuery("from:ALICE"))).toBe(true);
    expect(matchInboxQuery(message, parseInboxQuery("to:owner"))).toBe(true);
    expect(matchInboxQuery(message, parseInboxQuery("subject:invoice"))).toBe(true);
    expect(matchInboxQuery(message, parseInboxQuery("body:PAID"))).toBe(true);
    expect(matchInboxQuery(message, parseInboxQuery("received"))).toBe(true);
    expect(matchInboxQuery({ ...message, body_preview: "", body_cached: false }, parseInboxQuery("body:paid"), "")).toBe(false);
  });

  it("keeps spaces inside field values and highlights the complete value", () => {
    const query = "subject:Re: Collaboration: Meet Anna";
    const parsed = parseInboxQuery(query);

    expect(parsed.expression).toEqual({ kind: "term", field: "subject", value: "re: collaboration: meet anna" });
    expect(matchInboxQuery({ id: "mail-phrase", subject: "Re: Collaboration: Meet Anna" }, parsed)).toBe(true);
    expect(splitInboxQueryTokens(query)).toEqual([
      { text: "subject:", kind: "field" },
      { text: "Re: Collaboration: Meet Anna", kind: "value" },
    ]);
  });

  it("parses field-qualified quoted phrases and parentheses", () => {
    expect(parseInboxQuery('subject:"project alpha" AND body:"paid invoice"')).toMatchObject({
      expression: {
        kind: "and",
        terms: [
          { kind: "term", field: "subject", value: "project alpha" },
          { kind: "term", field: "body", value: "paid invoice" },
        ],
      },
    });
    expect(parseInboxQuery('(subject:"project alpha" OR body:"paid invoice") AND invoice').error).toBe("");
  });

  it("reports invalid expressions and offers only operator suggestions", () => {
    expect(parseInboxQuery("subject:").error).toContain("subject");
    expect(parseInboxQuery("foo AND OR bar").error).toContain("operator");
    expect(parseInboxQuery("label:inbox").error).toContain("Unknown");
    expect(getInboxQuerySuggestions("su")).toEqual(["subject:"]);
    expect(getInboxQueryHighlightTerms(parseInboxQuery("subject:invoice AND body:paid"))).toEqual(["invoice", "paid"]);
  });

  it("matches local status, attachments, and ISO date conditions", () => {
    const message = { id: "mail-2", label_ids: ["INBOX", "STARRED"], unread: true, has_attachment: true, internal_date: "1783814400000" };
    expect(matchInboxQuery(message, parseInboxQuery("is:starred AND is:unread"))).toBe(true);
    expect(matchInboxQuery(message, parseInboxQuery("has:attachment"))).toBe(true);
    expect(matchInboxQuery(message, parseInboxQuery("after:2026-07-10 AND before:2026-07-13"))).toBe(true);
    expect(parseInboxQuery("is:unknown").error).toContain("is:");
    expect(parseInboxQuery("before:July").error).toContain("YYYY-MM-DD");
    expect(getInboxQuerySuggestions("")).toContain("is:");
    expect(getInboxQuerySuggestions("is:sent")).toEqual([]);
  });

  it("supports Shortwave-style -exclude and is:todo via todoIds context", () => {
    const message = {
      id: "todo-1",
      from: "Bot <noreply@example.com>",
      subject: "Invoice reminder",
      label_ids: ["INBOX"],
    };
    expect(matchInboxQuery(message, parseInboxQuery("invoice AND -from:noreply"))).toBe(false);
    expect(matchInboxQuery(message, parseInboxQuery("invoice AND -from:other"))).toBe(true);
    expect(matchInboxQuery(message, parseInboxQuery("is:todo"), undefined, { todoIds: ["todo-1"] })).toBe(true);
    expect(matchInboxQuery(message, parseInboxQuery("is:todo"), undefined, { todoIds: ["other"] })).toBe(false);
    expect(parseInboxQuery("is:inbox AND is:todo").error).toBe("");
    expect(parseInboxQuery("-").error).toContain("-");
  });

  it("inserts a selected completion with the same token behavior as inbox search", () => {
    expect(applyInboxQuerySuggestion("su", "subject:")).toBe("subject:");
    expect(applyInboxQuerySuggestion("from:alice ", "is:")).toBe("from:alice is: ");
    expect(applyInboxQuerySuggestion("from:ali", "from:alice")).toBe("from:alice");
  });
});
