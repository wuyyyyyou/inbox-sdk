export function formatBeijingTimestamp(value: unknown): string {
  if (!value) return "";
  const date = new Date(Number(value) || String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function normalizeSubject(subject: unknown): string {
  return String(subject || "Untitled email").replace(/^(re|fw|fwd):\s*/i, "").trim() || "Untitled email";
}
