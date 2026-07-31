/** AI 回复中的线程引用必须来自本轮已确认的 Evidence。 */
function removeUnconfirmedThreadReferences(text: string, allowedThreadIds: Set<string>): string {
  return text
    .replace(/\[THREAD_?REF_?([A-Za-z0-9_-]+)\]/gi, (_reference, threadId: string) => (
      allowedThreadIds.has(threadId) ? `[THREAD_REF_${threadId}]` : ""
    ))
    .replace(/\[THREAD(?!_REF_[A-Za-z0-9_-]+\])[^\]]*\]/gi, "");
}

function isGmailMessageUrl(value: string): boolean {
  try {
    const url = new URL(value);
    const hostname = url.hostname.toLowerCase();
    return hostname === "mail.google.com" || hostname.endsWith(".mail.google.com");
  } catch {
    return false;
  }
}

function removeDirectMailLinks(text: string): string {
  // 邮件入口必须来自本轮已确认 Evidence，不能相信模型自行拼出的 Gmail URL。
  return text
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/gi, (match, _label: string, href: string) => (
      isGmailMessageUrl(href) ? "" : match
    ))
    .replace(/^[\t ]*(?:(?:[-*]|\d+[.)])\s*)?$/gm, "")
    .replace(/[\t ]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export function ensureConfirmedThreadReference(
  text: string,
  allowedThreadIds: Set<string>,
  threadReferenceLabels: Record<string, string>,
): string {
  let cleaned = removeDirectMailLinks(removeUnconfirmedThreadReferences(text, allowedThreadIds))
    .replace(/\[THREAD(?!_REF_)[^\]\s]*\]/g, "")
    .trim();
  if (!allowedThreadIds.size) return cleaned;
  const existingIds = new Set(
    Array.from(cleaned.matchAll(/\[THREAD_REF_([^\]\s]+)\]/g), (match) => match[1]),
  );
  // 模板/模型已把证据嵌入结论时，其余确认命中仍只是候选，不能再自动追加。
  // 这样链接与对应事实保持相邻，也不会把宽查询中的无关邮件伪装成同一结论的证据。
  if (existingIds.size) return cleaned;
  // 优先把入口贴到包含邮件主题的发现项末尾，保持“结论 → 证据”的扫描阅读顺序。
  const lines = cleaned.split("\n");
  const fallbackLineIndex = lines.findIndex((line) => {
    const trimmed = line.trim();
    return Boolean(trimmed) && !/^#{1,4}\s/.test(trimmed);
  });
  for (const threadId of allowedThreadIds) {
    if (existingIds.has(threadId)) continue;
    const subject = String(threadReferenceLabels[threadId] || "").trim().toLowerCase();
    const lineIndex = subject
      ? lines.findIndex((line) => line.toLowerCase().includes(subject))
      : -1;
    if (lineIndex < 0) continue;
    lines[lineIndex] = `${lines[lineIndex].trimEnd()} [THREAD_REF_${threadId}]`;
    existingIds.add(threadId);
  }
  if (fallbackLineIndex >= 0) {
    const fallbackThreadId = [...allowedThreadIds].find((threadId) => !existingIds.has(threadId));
    if (fallbackThreadId) {
      lines[fallbackLineIndex] = `${lines[fallbackLineIndex].trimEnd()} [THREAD_REF_${fallbackThreadId}]`;
    }
  }
  cleaned = lines.join("\n");
  return cleaned;
}
