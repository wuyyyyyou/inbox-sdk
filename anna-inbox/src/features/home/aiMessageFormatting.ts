export type AiMessageInline =
  | { type: "text"; value: string }
  | { type: "bold"; value: string }
  | { type: "link"; label: string; href: string }
  | { type: "thread_ref"; threadId: string };

export type AiMessageBlock =
  | { type: "paragraph"; content: AiMessageInline[] }
  | { type: "heading"; level: 1 | 2 | 3; content: AiMessageInline[] }
  | { type: "unordered_list"; indent: number; items: AiMessageInline[][] }
  | { type: "ordered_list"; indent: number; start: number; items: AiMessageInline[][] };

const headingPattern = /^(#{1,3})\s+(.+)$/;
const unorderedListPattern = /^(\s*)[-*]\s+(.+)$/;
const orderedListPattern = /^(\s*)(\d+)[.)]\s+(.+)$/;
const inlinePattern = /\[THREAD_REF_([^\]\s]+)\]|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|\*\*([^*]+)\*\*/g;

function splitInlineUnorderedListItems(line: string): string[] {
  return Array.from(line.matchAll(/(?:^|\s+)[-*]\s+(.+?)(?=\s+[-*]\s+|$)/g), (match) => match[1].trim());
}

function splitInlineOrderedListItems(line: string): string[] {
  return Array.from(line.matchAll(/(?:^|\s+)\d+[.)]\s+(.+?)(?=\s+\d+[.)]\s+|$)/g), (match) => match[1].trim());
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
      const indent = unordered[1].length;
      const items: AiMessageInline[][] = [];
      while (index < lines.length) {
        const item = lines[index].match(unorderedListPattern);
        if (!item || item[1].length !== indent) break;
        const inlineItems = splitInlineUnorderedListItems(lines[index]);
        for (const inlineItem of inlineItems.length ? inlineItems : [item[2]]) {
          items.push(parseAiMessageInline(inlineItem));
        }
        index += 1;
      }
      blocks.push({ type: "unordered_list", indent, items });
      continue;
    }
    const ordered = line.match(orderedListPattern);
    if (ordered) {
      const indent = ordered[1].length;
      const items: AiMessageInline[][] = [];
      const start = parseInt(ordered[2], 10);
      
      while (index < lines.length) {
        const item = lines[index].match(orderedListPattern);
        if (!item || item[1].length !== indent) break;
        const inlineItems = splitInlineOrderedListItems(lines[index]);
        for (const inlineItem of inlineItems.length ? inlineItems : [item[3]]) {
          items.push(parseAiMessageInline(inlineItem));
        }
        index += 1;
      }
      blocks.push({ type: "ordered_list", indent, start, items });
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

/** Visible character cost of one inline node (thread_ref is atomic). */
function measureInlineNode(node: AiMessageInline): number {
  if (node.type === "text" || node.type === "bold") return node.value.length;
  if (node.type === "link") return node.label.length;
  return 1;
}

export function measureAiMessageInline(nodes: AiMessageInline[]): number {
  return nodes.reduce((sum, node) => sum + measureInlineNode(node), 0);
}

export function measureAiMessageBlocks(blocks: AiMessageBlock[]): number {
  return blocks.reduce((sum, block) => {
    if (block.type === "unordered_list" || block.type === "ordered_list") {
      return sum + block.items.reduce((itemSum, item) => itemSum + measureAiMessageInline(item), 0);
    }
    return sum + measureAiMessageInline(block.content);
  }, 0);
}

function sliceInlineNodes(
  nodes: AiMessageInline[],
  budget: number,
): { nodes: AiMessageInline[]; remaining: number } {
  if (budget <= 0) return { nodes: [], remaining: 0 };
  const next: AiMessageInline[] = [];
  let remaining = budget;
  for (const node of nodes) {
    if (remaining <= 0) break;
    if (node.type === "text" || node.type === "bold") {
      if (node.value.length <= remaining) {
        next.push(node);
        remaining -= node.value.length;
      } else {
        next.push({ type: node.type, value: node.value.slice(0, remaining) });
        remaining = 0;
      }
      continue;
    }
    if (node.type === "link") {
      if (node.label.length <= remaining) {
        next.push(node);
        remaining -= node.label.length;
      } else {
        next.push({ type: "link", label: node.label.slice(0, remaining), href: node.href });
        remaining = 0;
      }
      continue;
    }
    // thread_ref: reveal atomically when at least one char of budget remains
    next.push(node);
    remaining -= 1;
  }
  return { nodes: next, remaining };
}

/**
 * Reveal a prefix of already-parsed markdown blocks by visible character budget.
 * Keeps block structure stable so typewriter never re-parses incomplete markdown.
 */
export function sliceAiMessageBlocks(blocks: AiMessageBlock[], maxChars: number): AiMessageBlock[] {
  if (maxChars <= 0) return [];
  const total = measureAiMessageBlocks(blocks);
  if (maxChars >= total) return blocks;

  const next: AiMessageBlock[] = [];
  let remaining = maxChars;

  for (const block of blocks) {
    if (remaining <= 0) break;

    if (block.type === "paragraph" || block.type === "heading") {
      const sliced = sliceInlineNodes(block.content, remaining);
      if (!sliced.nodes.length) break;
      if (block.type === "heading") {
        next.push({ type: "heading", level: block.level, content: sliced.nodes });
      } else {
        next.push({ type: "paragraph", content: sliced.nodes });
      }
      remaining = sliced.remaining;
      continue;
    }

    const items: AiMessageInline[][] = [];
    for (const item of block.items) {
      if (remaining <= 0) break;
      const sliced = sliceInlineNodes(item, remaining);
      if (!sliced.nodes.length) break;
      items.push(sliced.nodes);
      remaining = sliced.remaining;
    }
    if (items.length) {
      next.push(
        block.type === "ordered_list"
          ? { type: "ordered_list", indent: block.indent, start: block.start, items }
          : { type: "unordered_list", indent: block.indent, items },
      );
    }
  }

  return next;
}
