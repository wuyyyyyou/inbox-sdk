import type { InboxMessage } from "../../types/mail";
import { tFallback, type TranslateFn } from "../../i18n";

export type InboxQueryField = "subject" | "body" | "from" | "to" | "is" | "has" | "before" | "after";

export type InboxQueryExpression =
  | { kind: "term"; field: InboxQueryField | "any"; value: string; exclude?: boolean }
  | { kind: "and" | "or"; terms: InboxQueryExpression[] };

export interface ParsedInboxQuery {
  expression: InboxQueryExpression | null;
  error: string;
}

/** 本地 workflow 标记：is:todo 依赖前端 todos 列表，不写在 Gmail label 上。 */
export type InboxQueryMatchContext = {
  todoIds?: Iterable<string>;
};

const FIELDS: InboxQueryField[] = ["subject", "body", "from", "to", "is", "has", "before", "after"];
const STATUS_VALUES = [
  "sent",
  "unread",
  "done",
  "todo",
  "inbox",
  "snoozed",
  "starred",
  "important",
  "draft",
  "trash",
  "spam",
  "all",
];
const SUGGESTION_PLACEHOLDERS: Record<string, Parameters<TranslateFn>[0]> = {
  "AND": "search.suggestion.and",
  "OR": "search.suggestion.or",
  "subject:": "search.suggestion.subject",
  "body:": "search.suggestion.body",
  "from:": "search.suggestion.from",
  "to:": "search.suggestion.to",
  "is:": "search.suggestion.is",
  "has:": "search.suggestion.has",
  "before:": "search.suggestion.before",
  "after:": "search.suggestion.after",
};
type InboxQueryTokenKind = "field" | "value" | "operator" | "plain";
type InboxQueryToken = { text: string; kind: InboxQueryTokenKind };

/** 仅识别由空白包围的逻辑运算符，字段值中的普通空格属于搜索内容。 */
function findQueryOperators(input: string): Array<{ index: number; text: "AND" | "OR" }> {
  return Array.from(input.matchAll(/\b(AND|OR)\b/giu)).flatMap((match) => {
    const index = match.index ?? -1;
    const before = index > 0 ? input[index - 1] : "";
    const after = input[index + match[0].length] || "";
    return index >= 0 && (!before || /\s/u.test(before)) && (!after || /\s/u.test(after))
      ? [{ index, text: match[0].toUpperCase() as "AND" | "OR" }]
      : [];
  });
}

function joinExpression(kind: "and" | "or", terms: InboxQueryExpression[]): InboxQueryExpression {
  return terms.length === 1 ? terms[0] : { kind, terms };
}

function parseTermToken(token: string): { term: Extract<InboxQueryExpression, { kind: "term" }> | null; error: string } {
  let raw = token;
  let exclude = false;
  // Shortwave / Gmail 风格：-term 排除匹配；仅一元前缀，不做 -- 或单独 -
  if (raw.startsWith("-") && raw.length > 1) {
    exclude = true;
    raw = raw.slice(1);
  }
  if (!raw || raw === "-") {
    return { term: null, error: "The - operator needs a search term after it." };
  }

  const separator = raw.indexOf(":");
  let field: InboxQueryField | "any" = "any";
  let value = raw;
  if (separator >= 0) {
    const candidate = raw.slice(0, separator).toLowerCase();
    value = raw.slice(separator + 1);
    if (!FIELDS.includes(candidate as InboxQueryField)) {
      return { term: null, error: `Unknown search field: ${candidate || raw}.` };
    }
    if (!value) return { term: null, error: `The ${candidate}: field needs a keyword.` };
    if (candidate === "is" && !STATUS_VALUES.includes(value.toLowerCase())) {
      return { term: null, error: `is: must use ${STATUS_VALUES.join(", ")}.` };
    }
    if (candidate === "has" && value.toLowerCase() !== "attachment") {
      return { term: null, error: "has: currently supports attachment only." };
    }
    if ((candidate === "before" || candidate === "after") && !/^\d{4}-\d{2}-\d{2}$/u.test(value)) {
      return { term: null, error: `${candidate}: must use YYYY-MM-DD.` };
    }
    field = candidate as InboxQueryField;
  }
  return {
    term: {
      kind: "term",
      field,
      value: value.toLowerCase(),
      ...(exclude ? { exclude: true } : {}),
    },
    error: "",
  };
}

/**
 * 解析 Inbox 本地查询。
 * - 空格属于搜索词内容；多个条件需用 AND/OR 连接（AND 优先于 OR）
 * - `-term` / `-from:x`：排除匹配（Shortwave 风格一元否定）
 */
export function parseInboxQuery(input: string): ParsedInboxQuery {
  const raw = String(input || "").trim();
  if (!raw) return { expression: null, error: "" };
  const groups: InboxQueryExpression[] = [];
  let andTerms: InboxQueryExpression[] = [];
  let termStart = 0;

  for (const operator of findQueryOperators(raw)) {
    const token = raw.slice(termStart, operator.index).trim();
    if (!token) return { expression: null, error: `The ${operator.text} operator needs a term before and after it.` };
    const parsed = parseTermToken(token);
    if (!parsed.term) return { expression: null, error: parsed.error };
    andTerms.push(parsed.term);
    if (operator.text === "OR") {
      groups.push(joinExpression("and", andTerms));
      andTerms = [];
    }
    termStart = operator.index + operator.text.length;
  }

  const finalToken = raw.slice(termStart).trim();
  if (!finalToken) return { expression: null, error: "The final operator needs a search term after it." };
  const parsed = parseTermToken(finalToken);
  if (!parsed.term) return { expression: null, error: parsed.error };
  andTerms.push(parsed.term);
  groups.push(joinExpression("and", andTerms));
  return { expression: joinExpression("or", groups), error: "" };
}

function hasMatch(value: string | null | undefined, term: string): boolean {
  return String(value || "").toLowerCase().includes(term);
}

function matchesTerm(
  message: InboxMessage,
  term: Extract<InboxQueryExpression, { kind: "term" }>,
  cachedBody?: string,
  context?: InboxQueryMatchContext,
): boolean {
  const body = cachedBody === undefined ? (message.body_cached ? message.body_preview : "") : cachedBody;
  let hit = false;
  if (term.field === "subject") hit = hasMatch(message.subject || message.latest_subject, term.value);
  else if (term.field === "body") hit = hasMatch(body, term.value);
  else if (term.field === "from") hit = hasMatch(message.from, term.value);
  else if (term.field === "to") hit = hasMatch(message.to, term.value);
  else if (term.field === "has") {
    hit = term.value === "attachment" && Boolean(message.has_attachment || message.attachment_count);
  } else if (term.field === "is") {
    const labels = new Set((message.label_ids || []).map((label) => label.toUpperCase()));
    if (term.value === "todo") {
      const todoIds = new Set(Array.from(context?.todoIds || []).map(String));
      hit = todoIds.has(String(message.id || ""));
    } else {
      const checks: Record<string, boolean> = {
        sent: labels.has("SENT"),
        unread: Boolean(message.unread || labels.has("UNREAD")),
        done: labels.has("DONE"),
        inbox: labels.has("INBOX"),
        snoozed: labels.has("SNOOZED"),
        starred: Boolean(message.starred || labels.has("STARRED")),
        important: Boolean(message.important || labels.has("IMPORTANT")),
        draft: Boolean(message.draft_local || labels.has("DRAFT")),
        trash: labels.has("TRASH"),
        spam: labels.has("SPAM"),
        all: true,
      };
      hit = Boolean(checks[term.value]);
    }
  } else if (term.field === "before" || term.field === "after") {
    const rawDate = message.internal_date || message.date || "";
    const timestamp = /^\d+$/u.test(String(rawDate)) ? Number(rawDate) : Date.parse(String(rawDate));
    if (!Number.isFinite(timestamp)) hit = false;
    else {
      const boundary = Date.parse(`${term.value}T00:00:00`);
      hit = term.field === "before" ? timestamp < boundary : timestamp >= boundary;
    }
  } else {
    hit = [message.from, message.to, message.subject, message.latest_subject, message.snippet, body]
      .some((value) => hasMatch(value, term.value));
  }
  return term.exclude ? !hit : hit;
}

function matchesExpression(
  message: InboxMessage,
  expression: InboxQueryExpression,
  cachedBody?: string,
  context?: InboxQueryMatchContext,
): boolean {
  if (expression.kind === "term") return matchesTerm(message, expression, cachedBody, context);
  const matcher = expression.kind === "and" ? "every" : "some";
  return expression.terms[matcher]((term) => matchesExpression(message, term, cachedBody, context));
}

/** 仅在语法有效时匹配；调用方可据此避免在错误输入时意外筛选邮件。 */
export function matchInboxQuery(
  message: InboxMessage,
  parsed: ParsedInboxQuery,
  cachedBody?: string,
  context?: InboxQueryMatchContext,
): boolean {
  return Boolean(
    parsed.expression
    && !parsed.error
    && matchesExpression(message, parsed.expression, cachedBody, context),
  );
}

/** 为输入末尾尚未完成的字段名提供操作符建议。 */
export function getInboxQuerySuggestions(input: string): string[] {
  const raw = String(input || "");
  // 首次聚焦时展示可用字段；已完成的单个条件则不弹出菜单。
  if (!raw.trim()) return FIELDS.map((field) => `${field}:`);
  if (/\s$/u.test(raw) && raw.trim()) return [];
  const current = raw.trim().split(/\s+/u).at(-1)?.toLowerCase() || "";
  const bare = current.startsWith("-") ? current.slice(1) : current;
  if (!bare.includes(":")) {
    const fields = FIELDS.filter((field) => field.startsWith(bare)).map((field) => `${field}:`);
    if (fields.length) return fields;
  }
  const complete = parseInboxQuery(raw);
  if (complete.expression && !complete.error) return [];
  if (bare.startsWith("is:")) {
    return STATUS_VALUES.filter((value) => value.startsWith(bare.slice(3))).map((value) => `is:${value}`);
  }
  if (bare.startsWith("has:")) return "attachment".startsWith(bare.slice(4)) ? ["has:attachment"] : [];
  if (bare.includes(":")) return [];
  return [];
}

/** 返回建议的简短占位说明，状态值建议无需额外说明。 */
export function getInboxQuerySuggestionPlaceholder(suggestion: string, t?: TranslateFn): string {
  const key = SUGGESTION_PLACEHOLDERS[suggestion];
  return key ? (t ? t(key) : tFallback(key)) : "";
}

/** 将当前未完成的 token 替换为补全项，供主页搜索与 Split 查询编辑器复用。 */
export function applyInboxQuerySuggestion(input: string, suggestion: string): string {
  const firstToken = !/\s/u.test(input.trim());
  return /\s$/u.test(input)
    ? `${input}${suggestion} `
    : input.replace(/\S*$/u, `${suggestion}${firstToken ? "" : " "}`);
}

function appendQueryHighlightTerm(tokens: InboxQueryToken[], text: string): void {
  const firstContentIndex = text.search(/\S/u);
  if (firstContentIndex < 0) {
    if (text) tokens.push({ text, kind: "plain" });
    return;
  }
  const core = text.trim();
  const trailing = text.slice(firstContentIndex + core.length);
  if (firstContentIndex) tokens.push({ text: text.slice(0, firstContentIndex), kind: "plain" });

  const match = /^(-?)(subject|body|from|to|is|has|before|after):(.*)$/iu.exec(core);
  if (match) {
    if (match[1]) tokens.push({ text: "-", kind: "operator" });
    tokens.push({ text: `${match[2]}:`, kind: "field" }, { text: match[3], kind: "value" });
  } else if (core.startsWith("-") && core.length > 1) {
    tokens.push({ text: "-", kind: "operator" }, { text: core.slice(1), kind: "plain" });
  } else {
    tokens.push({ text: core, kind: "plain" });
  }
  if (trailing) tokens.push({ text: trailing, kind: "plain" });
}

/** 将输入拆为供编辑器高亮的字段、值和逻辑运算符片段。 */
export function splitInboxQueryTokens(input: string): InboxQueryToken[] {
  const raw = String(input || "");
  const tokens: InboxQueryToken[] = [];
  let termStart = 0;
  for (const operator of findQueryOperators(raw)) {
    appendQueryHighlightTerm(tokens, raw.slice(termStart, operator.index));
    tokens.push({ text: operator.text, kind: "operator" });
    termStart = operator.index + operator.text.length;
  }
  appendQueryHighlightTerm(tokens, raw.slice(termStart));
  return tokens;
}

/** 返回可安全用于 UI 高亮的关键词，不把字段名和逻辑操作符作为命中内容。 */
export function getInboxQueryHighlightTerms(parsed: ParsedInboxQuery): string[] {
  const terms: string[] = [];
  const visit = (expression: InboxQueryExpression | null) => {
    if (!expression) return;
    if (expression.kind === "term") {
      if (expression.exclude) return;
      if (!terms.includes(expression.value)) terms.push(expression.value);
      return;
    }
    expression.terms.forEach(visit);
  };
  if (!parsed.error) visit(parsed.expression);
  return terms;
}
