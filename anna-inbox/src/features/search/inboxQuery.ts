import type { InboxMessage } from "../../types/mail";

export type InboxQueryField = "subject" | "body" | "from" | "to" | "is" | "has" | "before" | "after";

export type InboxQueryExpression =
  | { kind: "term"; field: InboxQueryField | "any"; value: string }
  | { kind: "and" | "or"; terms: InboxQueryExpression[] };

export interface ParsedInboxQuery {
  expression: InboxQueryExpression | null;
  error: string;
}

const FIELDS: InboxQueryField[] = ["subject", "body", "from", "to", "is", "has", "before", "after"];
const STATUS_VALUES = ["sent", "unread", "done", "inbox", "snoozed", "starred", "important", "draft", "trash", "spam", "all"];
const SUGGESTION_PLACEHOLDERS: Record<string, string> = {
  "AND": "Combine two search queries",
  "OR": "Search for either of two queries",
  "subject:": "Words in the subject line",
  "body:": "Words in cached message content",
  "from:": "Specify the sender",
  "to:": "Specify a recipient",
  "is:": "Sent, unread, draft, trash, spam, and more",
  "has:": "Attachments",
  "before:": "Messages before a date (YYYY-MM-DD)",
  "after:": "Messages after a date (YYYY-MM-DD)",
};
const OPERATORS = ["AND", "OR"];

function joinExpression(kind: "and" | "or", terms: InboxQueryExpression[]): InboxQueryExpression {
  return terms.length === 1 ? terms[0] : { kind, terms };
}

/**
 * 解析 Inbox 本地查询。语法只支持空白分隔的关键词和 AND/OR，AND 的优先级高于 OR。
 */
export function parseInboxQuery(input: string): ParsedInboxQuery {
  const raw = String(input || "").trim();
  if (!raw) return { expression: null, error: "" };
  const tokens = raw.split(/\s+/u);
  const groups: InboxQueryExpression[] = [];
  let andTerms: InboxQueryExpression[] = [];
  let expectingTerm = true;

  for (const token of tokens) {
    const upper = token.toUpperCase();
    if (OPERATORS.includes(upper)) {
      if (expectingTerm) return { expression: null, error: `The ${upper} operator needs a term before and after it.` };
      if (upper === "OR") {
        groups.push(joinExpression("and", andTerms));
        andTerms = [];
      }
      expectingTerm = true;
      continue;
    }
    if (!expectingTerm) return { expression: null, error: "Add an AND or OR operator between search terms." };

    const separator = token.indexOf(":");
    let field: InboxQueryField | "any" = "any";
    let value = token;
    if (separator >= 0) {
      const candidate = token.slice(0, separator).toLowerCase();
      value = token.slice(separator + 1);
      if (!FIELDS.includes(candidate as InboxQueryField)) return { expression: null, error: `Unknown search field: ${candidate || token}.` };
      if (!value) return { expression: null, error: `The ${candidate}: field needs a keyword.` };
      if (candidate === "is" && !STATUS_VALUES.includes(value.toLowerCase())) return { expression: null, error: `is: must use ${STATUS_VALUES.join(", ")}.` };
      if (candidate === "has" && value.toLowerCase() !== "attachment") return { expression: null, error: "has: currently supports attachment only." };
      if ((candidate === "before" || candidate === "after") && !/^\d{4}-\d{2}-\d{2}$/u.test(value)) return { expression: null, error: `${candidate}: must use YYYY-MM-DD.` };
      field = candidate as InboxQueryField;
    }
    andTerms.push({ kind: "term", field, value: value.toLowerCase() });
    expectingTerm = false;
  }

  if (expectingTerm) return { expression: null, error: "The final operator needs a search term after it." };
  groups.push(joinExpression("and", andTerms));
  return { expression: joinExpression("or", groups), error: "" };
}

function hasMatch(value: string | null | undefined, term: string): boolean {
  return String(value || "").toLowerCase().includes(term);
}

function matchesTerm(message: InboxMessage, term: Extract<InboxQueryExpression, { kind: "term" }>, cachedBody?: string): boolean {
  const body = cachedBody === undefined ? (message.body_cached ? message.body_preview : "") : cachedBody;
  if (term.field === "subject") return hasMatch(message.subject || message.latest_subject, term.value);
  if (term.field === "body") return hasMatch(body, term.value);
  if (term.field === "from") return hasMatch(message.from, term.value);
  if (term.field === "to") return hasMatch(message.to, term.value);
  const labels = new Set((message.label_ids || []).map((label) => label.toUpperCase()));
  if (term.field === "has") return term.value === "attachment" && Boolean(message.has_attachment || message.attachment_count);
  if (term.field === "is") {
    const checks: Record<string, boolean> = {
      sent: labels.has("SENT"), unread: Boolean(message.unread || labels.has("UNREAD")), done: labels.has("DONE"), inbox: labels.has("INBOX"), snoozed: labels.has("SNOOZED"), starred: Boolean(message.starred || labels.has("STARRED")), important: Boolean(message.important || labels.has("IMPORTANT")), draft: Boolean(message.draft_local || labels.has("DRAFT")), trash: labels.has("TRASH"), spam: labels.has("SPAM"), all: true,
    };
    return checks[term.value] || false;
  }
  if (term.field === "before" || term.field === "after") {
    const rawDate = message.internal_date || message.date || "";
    const timestamp = /^\d+$/u.test(String(rawDate)) ? Number(rawDate) : Date.parse(String(rawDate));
    if (!Number.isFinite(timestamp)) return false;
    const boundary = Date.parse(`${term.value}T00:00:00`);
    return term.field === "before" ? timestamp < boundary : timestamp >= boundary;
  }
  return [message.from, message.to, message.subject, message.latest_subject, message.snippet, body].some((value) => hasMatch(value, term.value));
}

function matchesExpression(message: InboxMessage, expression: InboxQueryExpression, cachedBody?: string): boolean {
  if (expression.kind === "term") return matchesTerm(message, expression, cachedBody);
  const matcher = expression.kind === "and" ? "every" : "some";
  return expression.terms[matcher]((term) => matchesExpression(message, term, cachedBody));
}

/** 仅在语法有效时匹配；调用方可据此避免在错误输入时意外筛选邮件。 */
export function matchInboxQuery(message: InboxMessage, parsed: ParsedInboxQuery, cachedBody?: string): boolean {
  return Boolean(parsed.expression && !parsed.error && matchesExpression(message, parsed.expression, cachedBody));
}

/** 为输入末尾尚未完成的字段名提供操作符建议。 */
export function getInboxQuerySuggestions(input: string): string[] {
  const raw = String(input || "");
  // 首次聚焦时展示可用字段；已完成的单个条件则不弹出菜单。
  if (!raw.trim()) return FIELDS.map((field) => `${field}:`);
  if (/\s$/u.test(raw) && raw.trim()) return [];
  const current = raw.trim().split(/\s+/u).at(-1)?.toLowerCase() || "";
  if (!current.includes(":")) {
    const fields = FIELDS.filter((field) => field.startsWith(current)).map((field) => `${field}:`);
    if (fields.length) return fields;
  }
  const complete = parseInboxQuery(raw);
  if (complete.expression && !complete.error) return [];
  if (current.startsWith("is:")) return STATUS_VALUES.filter((value) => value.startsWith(current.slice(3))).map((value) => `is:${value}`);
  if (current.startsWith("has:")) return "attachment".startsWith(current.slice(4)) ? ["has:attachment"] : [];
  if (current.includes(":")) return [];
  return [];
}

/** 返回建议的简短占位说明，状态值建议无需额外说明。 */
export function getInboxQuerySuggestionPlaceholder(suggestion: string): string {
  return SUGGESTION_PLACEHOLDERS[suggestion] || "";
}

/** 将当前未完成的 token 替换为补全项，供主页搜索与 Split 查询编辑器复用。 */
export function applyInboxQuerySuggestion(input: string, suggestion: string): string {
  const firstToken = !/\s/u.test(input.trim());
  return /\s$/u.test(input)
    ? `${input}${suggestion} `
    : input.replace(/\S*$/u, `${suggestion}${firstToken ? "" : " "}`);
}

/** 将输入拆为供编辑器高亮的字段、值和逻辑运算符片段。 */
export function splitInboxQueryTokens(input: string): Array<{ text: string; kind: "field" | "value" | "operator" | "plain" }> {
  return String(input || "").split(/(\s+)/u).filter(Boolean).flatMap<{ text: string; kind: "field" | "value" | "operator" | "plain" }>((token) => {
    if (/^\s+$/u.test(token)) return [{ text: token, kind: "plain" as const }];
    if (/^(AND|OR)$/iu.test(token)) return [{ text: token, kind: "operator" as const }];
    const match = /^(subject|body|from|to|is|has|before|after):(.*)$/iu.exec(token);
    return match ? [{ text: `${match[1]}:`, kind: "field" as const }, { text: match[2], kind: "value" as const }] : [{ text: token, kind: "plain" as const }];
  });
}

/** 返回可安全用于 UI 高亮的关键词，不把字段名和逻辑操作符作为命中内容。 */
export function getInboxQueryHighlightTerms(parsed: ParsedInboxQuery): string[] {
  const terms: string[] = [];
  const visit = (expression: InboxQueryExpression | null) => {
    if (!expression) return;
    if (expression.kind === "term") {
      if (!terms.includes(expression.value)) terms.push(expression.value);
      return;
    }
    expression.terms.forEach(visit);
  };
  if (!parsed.error) visit(parsed.expression);
  return terms;
}
