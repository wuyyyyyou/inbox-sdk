import { describe, expect, it } from "vitest";
import { applyInboxQuerySuggestion, getInboxQueryHighlightTerms, getInboxQuerySuggestionPlaceholder, getInboxQuerySuggestions, localizeInboxQueryError, matchInboxQuery, parseInboxQuery, splitInboxQueryTokens } from "./inboxQuery";

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
    expect(getInboxQuerySuggestions("subject:su")).toEqual([]);
    expect(getInboxQuerySuggestions("subject:s")).toEqual([]);
    expect(getInboxQuerySuggestions("body:bo")).toEqual([]);
    expect(getInboxQuerySuggestions("from:alice")).toEqual([]);
    expect(getInboxQuerySuggestions("to:me")).toEqual([]);
    expect(getInboxQuerySuggestions("is:un")).toEqual(["is:unread"]);
    expect(getInboxQuerySuggestions("has:at")).toEqual(["has:attachment"]);
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
    expect(applyInboxQuerySuggestion("has", "has:")).toBe("has:");
    expect(applyInboxQuerySuggestion("from:alice ", "is:")).toBe("from:alice is:");
    expect(applyInboxQuerySuggestion("is:un", "is:unread")).toBe("is:unread ");
    expect(applyInboxQuerySuggestion("has:at", "has:attachment")).toBe("has:attachment ");
    expect(applyInboxQuerySuggestion("from:ali", "from:alice")).toBe("from:alice ");
    expect(applyInboxQuerySuggestion("to:cicala an", "AND")).toBe("to:cicala AND ");
  });

  it("suggests logical operators only after a valid condition", () => {
    expect(getInboxQuerySuggestions("to:cicala ")).toEqual(["AND", "OR"]);
    expect(getInboxQuerySuggestions("subject:")).toEqual([]);
    expect(getInboxQuerySuggestions("to:cicala an")).toEqual(["AND"]);
    expect(getInboxQuerySuggestions("to:cicala and")).toEqual(["AND"]);
    expect(getInboxQuerySuggestions("to:cicala or")).toEqual(["OR"]);
    expect(getInboxQuerySuggestions("is:important AND is:")).toContain("is:important");
    expect(getInboxQuerySuggestions("is:important AND is:")).not.toContain("AND");
    expect(getInboxQuerySuggestionPlaceholder("AND", (key) => key === "search.suggestion.and" ? "Combine two search queries" : key))
      .toBe("Combine two search queries");
    expect(getInboxQuerySuggestionPlaceholder("OR", (key) => key === "search.suggestion.or" ? "搜索符合任一条件的邮件" : key))
      .toBe("搜索符合任一条件的邮件");
    expect(splitInboxQueryTokens("to:cicala an")).toEqual([
      { text: "to:", kind: "field" },
      { text: "cicala ", kind: "value" },
      { text: "an", kind: "plain" },
    ]);
    expect(splitInboxQueryTokens("is:important AND is:").map((token) => token.kind)).toEqual([
      "field", "value", "plain", "operator", "plain", "field", "value",
    ]);
    expect(splitInboxQueryTokens("is:sent AND")).toEqual([
      { text: "is:", kind: "field" },
      { text: "sent ", kind: "value" },
      { text: "AND", kind: "operator" },
    ]);
  });

  it("keeps incomplete logical operator prefixes as plain editing content", () => {
    // 未完成的 a/an/o 只能按普通可编辑内容高亮（配合 is-editing 显示橙色），
    // 只有完整确认的 AND/OR 才作为逻辑操作符高亮。
    for (const prefix of ["a", "an", "o"]) {
      expect(splitInboxQueryTokens(`to:cicala ${prefix}`)).toEqual([
        { text: "to:", kind: "field" },
        { text: "cicala ", kind: "value" },
        { text: prefix, kind: "plain" },
      ]);
    }
    for (const operator of ["and", "or"]) {
      expect(splitInboxQueryTokens(`to:cicala ${operator}`)).toEqual([
        { text: "to:", kind: "field" },
        { text: "cicala ", kind: "value" },
        { text: operator, kind: "operator" },
      ]);
    }
  });

  it("maps status suggestions to concise localized descriptions", () => {
    const translations: Record<string, string> = {
      "search.suggestion.isUnread": "Unread messages",
    };
    const t = (key: string) => translations[key] || `missing:${key}`;
    expect(getInboxQuerySuggestionPlaceholder("is:unread", t)).toBe("Unread messages");
    expect(getInboxQuerySuggestionPlaceholder("is:unread", (key) => key === "search.suggestion.isUnread" ? "未读邮件" : key)).toBe("未读邮件");
  });

  it("maps the complete attachment suggestion in both locales", () => {
    expect(getInboxQuerySuggestionPlaceholder("has:attachment", (key) => key === "search.suggestion.hasAttachment" ? "Messages with attachments" : key))
      .toBe("Messages with attachments");
    expect(getInboxQuerySuggestionPlaceholder("has:attachment", (key) => key === "search.suggestion.hasAttachment" ? "包含附件的邮件" : key))
      .toBe("包含附件的邮件");
  });

  it("localizes query errors while preserving the English wording", () => {
    const zh = (key: string, params?: Record<string, string | number>) => {
      const messages: Record<string, string> = {
        "search.error.fieldNeedsKeyword": `${params?.field}: 字段需要关键词。`,
        "search.error.invalidDate": `${params?.field}: 必须使用 YYYY-MM-DD 格式。`,
      };
      return messages[key] || key;
    };
    const en = (key: string, params?: Record<string, string | number>) => {
      const messages: Record<string, string> = {
        "search.error.fieldNeedsKeyword": `The ${params?.field}: field needs a keyword.`,
        "search.error.invalidDate": `${params?.field}: must use YYYY-MM-DD.`,
      };
      return messages[key] || key;
    };

    expect(localizeInboxQueryError(parseInboxQuery("after:").error, zh)).toBe("after: 字段需要关键词。");
    expect(localizeInboxQueryError(parseInboxQuery("after:").error, en)).toBe("The after: field needs a keyword.");
    expect(localizeInboxQueryError(parseInboxQuery("before:July").error, zh)).toBe("before: 必须使用 YYYY-MM-DD 格式。");
    expect(localizeInboxQueryError(parseInboxQuery("before:July").error, en)).toBe("before: must use YYYY-MM-DD.");
  });
});
