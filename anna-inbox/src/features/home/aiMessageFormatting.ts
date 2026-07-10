export type AiMessageInline =
  | { type: "text"; value: string }
  | { type: "bold"; value: string }
  | { type: "link"; label: string; href: string }
  | { type: "thread_ref"; threadId: string };

export type AiMessageBlock =
  | { type: "paragraph"; content: AiMessageInline[] }
  | { type: "heading"; level: 1 | 2 | 3; content: AiMessageInline[] }
  | { type: "unordered_list"; items: AiMessageInline[][] }
  | { type: "ordered_list"; items: AiMessageInline[][] };

const headingPattern = /^(#{1,3})\s+(.+)$/;
const unorderedListPattern = /^[-*]\s+(.+)$/;
const orderedListPattern = /^\d+[.)]\s+(.+)$/;
const inlinePattern = /\[THREAD_REF_([^\]\s]+)\]|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|\*\*([^*]+)\*\*/g;

function splitInlineUnorderedListItems(line: string): string[] {
  return Array.from(line.matchAll(/(?:^|\s+)\*\s+(.+?)(?=\s+\*\s+|$)/g), (match) => match[1].trim());
}

function isSupportedUrl(value: string) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:";
  } catch {
    return false;
  }
}

export function parseAiMessageInline(text: string): AiMessageInline[] {
  const nodes: AiMessageInline[] = [];
  let index = 0;
  inlinePattern.lastIndex = 0;
  for (const match of text.matchAll(inlinePattern)) {
    const matchIndex = match.index ?? 0;
    if (matchIndex > index) nodes.push({ type: "text", value: text.slice(index, matchIndex) });
    if (match[1]) {
      nodes.push({ type: "thread_ref", threadId: match[1] });
    } else if (match[2] && match[3] && isSupportedUrl(match[3])) {
      nodes.push({ type: "link", label: match[2], href: match[3] });
    } else if (match[4]) {
      nodes.push({ type: "bold", value: match[4] });
    } else {
      nodes.push({ type: "text", value: match[0] });
    }
    index = matchIndex + match[0].length;
  }
  if (index < text.length) nodes.push({ type: "text", value: text.slice(index) });
  return nodes;
}

export function parseAiMessageMarkdown(text: string): AiMessageBlock[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: AiMessageBlock[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const heading = line.match(headingPattern);
    if (heading) {
      blocks.push({
        type: "heading",
        level: heading[1].length as 1 | 2 | 3,
        content: parseAiMessageInline(heading[2]),
      });
      index += 1;
      continue;
    }
    const unordered = line.match(unorderedListPattern);
    if (unordered) {
      const items: AiMessageInline[][] = [];
      while (index < lines.length) {
        const item = lines[index].match(unorderedListPattern);
        if (!item) break;
        const inlineItems = splitInlineUnorderedListItems(lines[index]);
        for (const inlineItem of inlineItems.length ? inlineItems : [item[1]]) {
          items.push(parseAiMessageInline(inlineItem));
        }
        index += 1;
      }
      blocks.push({ type: "unordered_list", items });
      continue;
    }
    const ordered = line.match(orderedListPattern);
    if (ordered) {
      const items: AiMessageInline[][] = [];
      while (index < lines.length) {
        const item = lines[index].match(orderedListPattern);
        if (!item) break;
        items.push(parseAiMessageInline(item[1]));
        index += 1;
      }
      blocks.push({ type: "ordered_list", items });
      continue;
    }
    const paragraph: string[] = [];
    while (index < lines.length && lines[index].trim()) {
      if (headingPattern.test(lines[index]) || unorderedListPattern.test(lines[index]) || orderedListPattern.test(lines[index])) break;
      paragraph.push(lines[index]);
      index += 1;
    }
    if (paragraph.length) blocks.push({ type: "paragraph", content: parseAiMessageInline(paragraph.join("\n")) });
  }
  return blocks;
}
