/**
 * App 侧 L1 LLM（anna.llm.stream / complete）。
 * stream 用于闲聊等纯文本体验；失败时回退 complete，再失败由调用方走 Executa。
 */

export type LlmMessage = {
  role: "system" | "user" | "assistant";
  content: string;
};

export type LlmStreamHandlers = {
  onToken?: (text: string) => void;
  signal?: AbortSignal;
};

function extractCompleteText(value: unknown): string {
  if (!value) return "";
  if (typeof value === "string") return value;
  if (typeof value !== "object") return "";
  const obj = value as Record<string, unknown>;
  if (typeof obj.text === "string") return obj.text;
  const content = obj.content;
  if (typeof content === "string") return content;
  if (content && typeof content === "object") {
    const c = content as Record<string, unknown>;
    if (typeof c.text === "string") return c.text;
  }
  return "";
}

function isAsyncIterable(value: unknown): value is AsyncIterable<unknown> {
  return Boolean(value && typeof value === "object" && Symbol.asyncIterator in (value as object));
}

/**
 * 短闲聊 system（与后端 chat_general 同职责，保持极短）。
 */
export function chatStreamSystemPrompt(language: "zh" | "en", memorySummary = ""): string {
  const lang = language === "zh" ? "Simplified Chinese" : "English";
  const memory = memorySummary.trim()
    ? `\nUser preferences:\n${memorySummary.trim().slice(0, 400)}`
    : "";
  return (
    `You are Anna, a concise inbox assistant in a chat-only turn.\n` +
    `Do not claim to have scanned or read the user's email.\n` +
    `No silent Gmail mutations.\n` +
    `Always finish every sentence and the full answer; never stop mid-sentence or mid-list.\n` +
    `Respond in ${lang}.${memory}`
  );
}

/**
 * 通过 Host `llm.stream` 逐 token 生成；不支持时回退 `llm.complete`。
 */
export async function streamLlmText(
  client: any,
  messages: LlmMessage[],
  options: LlmStreamHandlers & { maxTokens?: number } = {},
): Promise<string> {
  // 默认 2048：避免 Host stopReason=length 在句中截断；调用方可再收紧。
  const maxTokens = Math.max(64, Math.min(Number(options.maxTokens || 2048), 4096));
  const llm = client?.llm;
  if (!llm) {
    throw new Error("anna.llm is unavailable in this runtime");
  }

  // Prefer stream (beta.71+); fall back to complete on -32601 / missing method.
  if (typeof llm.stream === "function") {
    try {
      const stream = await llm.stream(
        { messages, maxTokens },
        { timeoutMs: 180_000, signal: options.signal },
      );
      let full = "";
      if (isAsyncIterable(stream)) {
        for await (const event of stream) {
          if (options.signal?.aborted) {
            throw new DOMException("The request was aborted.", "AbortError");
          }
          if (!event || typeof event !== "object") continue;
          const ev = event as Record<string, unknown>;
          const kind = String(ev.event || ev.type || "");
          if (kind === "model_token" || kind === "token") {
            const piece = String(ev.text || ev.content || "");
            if (piece) {
              full += piece;
              options.onToken?.(piece);
            }
            continue;
          }
          if (kind === "complete" || kind === "done") {
            const terminal = extractCompleteText(ev) || extractCompleteText(ev.content);
            if (terminal && !full) {
              full = terminal;
              options.onToken?.(terminal);
            } else if (terminal && terminal.length > full.length) {
              // 部分 Host 只在 complete 给全文
              const delta = terminal.slice(full.length);
              full = terminal;
              if (delta) options.onToken?.(delta);
            }
          }
        }
        if (full.trim()) return full.trim();
      } else {
        // 非 iterable：尝试当 complete 结果解析
        const text = extractCompleteText(stream);
        if (text.trim()) {
          options.onToken?.(text);
          return text.trim();
        }
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      // Method not found → fall through to complete
      if (!/-32601|not found|unavailable|is not a function/i.test(message)) {
        throw error;
      }
    }
  }

  if (typeof llm.complete !== "function") {
    throw new Error("anna.llm.complete is unavailable");
  }
  const result = await llm.complete(
    { messages, maxTokens },
    { timeoutMs: 180_000, signal: options.signal },
  );
  const text = extractCompleteText(result);
  if (!text.trim()) {
    throw new Error("anna.llm returned empty content");
  }
  options.onToken?.(text);
  return text.trim();
}
