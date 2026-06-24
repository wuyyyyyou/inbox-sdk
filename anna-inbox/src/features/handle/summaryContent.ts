function asString(value: unknown): string {
  return typeof value === "string" ? value : String(value ?? "");
}

function asList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => asString(item).trim()).filter(Boolean);
  const text = asString(value).trim();
  return text ? [text] : [];
}

function parseStructuredNoise(text: string): Record<string, unknown> | null {
  const trimmed = text.trim();
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) return null;
  try {
    const parsed = JSON.parse(trimmed);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function isSummaryPayloadShape(value: Record<string, unknown>): boolean {
  const keys = [
    "thread_kind",
    "headline",
    "what_happened",
    "open_questions",
    "reply_focus",
    "related_context",
    "should_show",
    "confidence",
  ];
  return keys.some((key) => key in value);
}

function firstMeaningfulSegment(value: string): string {
  return value.split(/\r?\n+/).map((part) => part.trim()).find(Boolean) || "";
}

export function normalizeEmailSummaryItems(value: unknown): string[] {
  return asList(value)
    .map(firstMeaningfulSegment)
    .filter(Boolean)
    .filter((item) => {
      const parsed = parseStructuredNoise(item);
      return !(parsed && isSummaryPayloadShape(parsed));
    });
}

export function uniqueLines(lines: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const line of lines) {
    const text = line.trim();
    const key = text.toLowerCase();
    if (text && !seen.has(key)) {
      seen.add(key);
      result.push(text);
    }
  }
  return result;
}

export function contactContextLines(contactContext: Record<string, unknown>): string[] {
  const topics = Array.isArray(contactContext.relevant_topics) ? contactContext.relevant_topics : [];
  return uniqueLines(topics.map((topic) => {
    const raw = topic && typeof topic === "object" ? topic as Record<string, unknown> : {};
    return [raw.title, raw.summary, raw.open_loop].map(asString).filter(Boolean).join(" · ");
  }).filter(Boolean));
}
