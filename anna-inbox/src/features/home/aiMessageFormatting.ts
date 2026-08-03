export type AiMessageInline =
  | { type: "text"; value: string }
  | { type: "bold"; value: string }
  | { type: "italic"; value: string }
  | { type: "strikethrough"; value: string }
  | { type: "code"; value: string }
  | { type: "link"; label: string; href: string }
  | { type: "thread_ref"; threadId: string };

export type AiMessageBlock =
  | { type: "paragraph"; content: AiMessageInline[] }
  | { type: "heading"; level: 1 | 2 | 3 | 4; content: AiMessageInline[] }
  | { type: "unordered_list"; indent: number; items: AiMessageInline[][] }
  | { type: "ordered_list"; indent: number; start: number; items: AiMessageInline[][] }
  | { type: "metadata"; label: string; content: AiMessageInline[] }
  | { type: "blockquote"; content: AiMessageInline[] }
  | { type: "code_block"; language: string; code: string }
  | { type: "divider" };

const headingPattern = /^(#{1,4})\s+(.+)$/;
const unorderedListPattern = /^(\s*)[-*]\s+(.+)$/;
const orderedListPattern = /^(\s*)(\d+)[.)]\s+(.+)$/;
const quotePattern = /^>\s?(.*)$/;
const codeFencePattern = /^```([^\s`]*)\s*$/;
const dividerPattern = /^(?:-{3,}|\*{3,}|_{3,})\s*$/;
const inlinePattern = /\[THREAD_REF_([^\]\s]+)\]|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|\*\*([^*]+)\*\*|~~([^~]+)~~|`([^`]+)`|\*([^*]+)\*|_([^_]+)_/g;
const metadataLabelPattern = /(发件人|主题|时间|寄件人|Sender|Subject|Date|Time)\s*[：:]/gi;

function metadataRowsFromLine(line: string): Array<{ label: string; value: string }> | null {
  const matches = Array.from(line.matchAll(metadataLabelPattern));
  if (!matches.length || (matches[0].index || 0) > 4) return null;
  const rows = matches.map((match, index) => {
    const start = (match.index || 0) + match[0].length;
    const end = index + 1 < matches.length ? matches[index + 1].index || line.length : line.length;
    return {
      label: match[1],
      value: line.slice(start, end).replace(/^\s*\|?\s*|\s*\|?\s*$/g, "").trim(),
    };
  }).filter((row) => row.value);
  return rows.length ? rows : null;
}

function normalizeMarkdownTables(text: string): string {
  const lines = text.split('\n');
  const result: string[] = [];
  let inTable = false;
  let headers: string[] = [];

  for (let i = 0; i < lines.length; i++) {
    const originalLine = lines[i];
    const trimmedLine = originalLine.trim();

    // Detect header row
    if (trimmedLine.startsWith('|') && trimmedLine.endsWith('|') && trimmedLine.indexOf('---') === -1) {
      const cells = trimmedLine.split('|').map(cell => cell.trim()).filter(Boolean);
      if (cells.length > 0) {
        headers = cells;
        inTable = true;
        result.push('## ' + headers.join(' | ')); // Format header as a heading
        i++; // Skip the separator line
        if (lines[i] && lines[i].trim().match(/^\|[-—]+\|([-—]+\|)*$/)) { // Handle separator line
            i++;
        }
        continue;
      }
    }

    // Detect data rows
    if (inTable && trimmedLine.startsWith('|') && trimmedLine.endsWith('|')) {
      const cells = trimmedLine.split('|').map(cell => cell.trim()).filter(Boolean);
      if (cells.length === headers.length) {
        for (let j = 0; j < headers.length; j++) {
          result.push(`- **${headers[j]}**: ${cells[j]}`);
        }
        result.push(''); // Add a blank line for separation between rows
        continue;
      } else {
        // If cell count doesn't match, it's not a valid table row, break out of table mode
        inTable = false;
        headers = [];
      }
    } else {
      inTable = false;
      headers = [];
    }

    result.push(originalLine); // Preserve original line with leading spaces if not a table part
  }

  return result.join('\n');
}

function normalizeInlineHeadings(text: string): string {
  // 模型偶尔会把标题紧接在上一句后面，先补行再交给块级 Markdown 解析。
  return text.replace(/([^\n])\s+(#{1,4}\s+)/g, "$1\n\n$2");
}

function normalizeMultilineBold(text: string): string {
  // Host 偶发把多行主题包进同一个 **...**，折叠成单行避免段落/加粗错乱。
  return text.replace(/\*\*([^*]+)\*\*/g, (_match, inner: string) => {
    const collapsed = String(inner).replace(/\s*\n\s*/g, " ").trim();
    return collapsed ? `**${collapsed}**` : "";
  });
}

function isStructuralMarkdownLine(line: string): boolean {
  const trimmed = line.trim();
  if (!trimmed) return false;
  return Boolean(
    headingPattern.test(trimmed)
    || unorderedListPattern.test(line)
    || orderedListPattern.test(line)
    || quotePattern.test(trimmed)
    || codeFencePattern.test(trimmed)
    || dividerPattern.test(trimmed)
    || metadataRowsFromLine(trimmed),
  );
}

function stripWrappingBold(line: string): string {
  const trimmed = line.trim();
  const wrapped = trimmed.match(/^\*\*(.+)\*\*$/);
  return wrapped ? wrapped[1].trim() : trimmed;
}

function normalizeSelectedEmailListing(text: string): string {
  // Host 列出“已选邮件”时常输出裸主题行；在选择引导语后把连续裸行收成列表项。
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const result: string[] = [];
  const selectionIntro = /选择了以下\s*\d*\s*封邮件|已选择\s*\d*\s*封邮件|you (?:have )?selected (?:the following )?\d*\s*emails?/i;
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    result.push(line);
    if (!selectionIntro.test(line)) {
      index += 1;
      continue;
    }
    index += 1;
    while (index < lines.length && !lines[index].trim()) {
      result.push(lines[index]);
      index += 1;
    }
    const bare: string[] = [];
    while (index < lines.length) {
      const current = lines[index];
      const trimmed = current.trim();
      if (!trimmed) {
        let lookAhead = index + 1;
        while (lookAhead < lines.length && !lines[lookAhead].trim()) lookAhead += 1;
        if (lookAhead >= lines.length || isStructuralMarkdownLine(lines[lookAhead])) break;
        index += 1;
        continue;
      }
      if (isStructuralMarkdownLine(current)) break;
      bare.push(stripWrappingBold(current));
      index += 1;
    }
    if (bare.length >= 1) {
      for (const item of bare) result.push(`- ${item}`);
    }
  }
  return result.join("\n");
}

function normalizePipedRanking(text: string): string {
  return text.replace(/^([^\n]*?(?:排序|优先级|Priority|Ranking)[^\n]*\|[^\n]*)$/gim, (line) => {
    const cells = line.split("|").map((cell) => cell.trim()).filter(Boolean);
    const firstRank = cells.findIndex((cell) => /^\d+(?:\s*[-–]\s*\d+)?$/.test(cell));
    if (firstRank < 0 || firstRank + 1 >= cells.length) return line;
    const rows: string[] = ["## 排序依据"];
    for (let index = firstRank; index + 1 < cells.length; index += 2) {
      if (!/^\d+(?:\s*[-–]\s*\d+)?$/.test(cells[index])) break;
      rows.push(`${rows.length}. **优先级 ${cells[index]}**：${cells[index + 1]}`);
    }
    return rows.length > 1 ? rows.join("\n") : line;
  });
}


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
    } else if (match[5]) {
      nodes.push({ type: "strikethrough", value: match[5] });
    } else if (match[6]) {
      nodes.push({ type: "code", value: match[6] });
    } else if (match[7] || match[8]) {
      nodes.push({ type: "italic", value: match[7] || match[8] });
    } else {
      nodes.push({ type: "text", value: match[0] });
    }
    index = matchIndex + match[0].length;
  }
  if (index < text.length) nodes.push({ type: "text", value: text.slice(index) });
  return nodes;
}

function parseMetadataInline(text: string): AiMessageInline[] {
  const value = text.trim();
  // 部分模型会给邮件元数据加上未闭合的前置 **，避免把标记直接显示给用户。
  if (value.startsWith("**") && !value.slice(2).includes("**")) {
    const boldValue = value.slice(2).trim();
    return boldValue ? [{ type: "bold", value: boldValue }] : [];
  }
  return parseAiMessageInline(value);
}

export function parseAiMessageMarkdown(text: string): AiMessageBlock[] {
  const lines = normalizeInlineHeadings(
    normalizeSelectedEmailListing(
      normalizeMultilineBold(
        normalizeMarkdownTables(normalizePipedRanking(text)),
      ),
    ),
  ).replace(/\r\n?/g, "\n").split("\n");
  const blocks: AiMessageBlock[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const metadataRows = metadataRowsFromLine(line);
    if (metadataRows) {
      for (const row of metadataRows) {
        const threadRefs = row.value.match(/\[THREAD_REF_[^\]\s]+\]/g) || [];
        const value = row.value.replace(/\s*\|?\s*\[THREAD_REF_[^\]\s]+\]\s*/g, "").trim();
        if (value) blocks.push({ type: "metadata", label: row.label, content: parseMetadataInline(value) });
        for (const threadRef of threadRefs) {
          blocks.push({ type: "metadata", label: "", content: parseAiMessageInline(threadRef) });
        }
      }
      index += 1;
      continue;
    }
    const codeFence = line.match(codeFencePattern);
    if (codeFence) {
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !codeFencePattern.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push({ type: "code_block", language: codeFence[1], code: codeLines.join("\n") });
      continue;
    }
    if (dividerPattern.test(line)) {
      blocks.push({ type: "divider" });
      index += 1;
      continue;
    }
    const quote = line.match(quotePattern);
    if (quote) {
      blocks.push({ type: "blockquote", content: parseAiMessageInline(quote[1]) });
      index += 1;
      continue;
    }
    const heading = line.match(headingPattern);
    if (heading) {
      blocks.push({
        type: "heading",
        level: heading[1].length as 1 | 2 | 3 | 4,
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
      if (
        codeFencePattern.test(lines[index])
        || dividerPattern.test(lines[index])
        || quotePattern.test(lines[index])
        || headingPattern.test(lines[index])
        || unorderedListPattern.test(lines[index])
        || orderedListPattern.test(lines[index])
      ) break;
      paragraph.push(lines[index]);
      index += 1;
    }
    if (paragraph.length) blocks.push({ type: "paragraph", content: parseAiMessageInline(paragraph.join("\n")) });
  }
  return blocks;
}

/** Visible character cost of one inline node (thread_ref is atomic). */
function measureInlineNode(node: AiMessageInline): number {
  if (node.type === "text" || node.type === "bold" || node.type === "italic" || node.type === "strikethrough" || node.type === "code") return node.value.length;
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
    if (block.type === "code_block") return sum + block.code.length;
    if (block.type === "divider") return sum;
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
    if (node.type === "text" || node.type === "bold" || node.type === "italic" || node.type === "strikethrough" || node.type === "code") {
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

    if (block.type === "paragraph" || block.type === "heading" || block.type === "blockquote" || block.type === "metadata") {
      const sliced = sliceInlineNodes(block.content, remaining);
      if (!sliced.nodes.length) break;
      if (block.type === "heading") {
        next.push({ type: "heading", level: block.level, content: sliced.nodes });
      } else if (block.type === "blockquote") {
        next.push({ type: "blockquote", content: sliced.nodes });
      } else if (block.type === "metadata") {
        next.push({ type: "metadata", label: block.label, content: sliced.nodes });
      } else {
        next.push({ type: "paragraph", content: sliced.nodes });
      }
      remaining = sliced.remaining;
      continue;
    }

    if (block.type === "divider") {
      next.push(block);
      continue;
    }

    if (block.type === "code_block") {
      const code = block.code.slice(0, remaining);
      if (!code) break;
      next.push({ ...block, code });
      remaining -= code.length;
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
