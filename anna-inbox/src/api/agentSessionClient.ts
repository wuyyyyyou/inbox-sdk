import { AI_SIDEBAR_SYSTEM_PROMPT } from "../app/aiAgentSystemPrompt";

export type AgentToolOutcome = Record<string, unknown>;

export type AgentTurnResult = {
  text: string;
  toolOutcomes: AgentToolOutcome[];
};

export function stripTerminalDoneMarker(text: string): string {
  // Host 可能输出独立 [DONE] 行，也可能把 [DONE] 直接粘在最后一行末尾。
  return text
    .replace(/^[ \t]*\[DONE\][ \t]*$/gim, "")
    .replace(/\[DONE\][ \t]*$/i, "")
    .trimEnd();
}

type AgentSessionHandle = {
  run: (args: Record<string, unknown>, options?: Record<string, unknown>) => Promise<unknown> | unknown;
  cancel?: (runId: string | Record<string, unknown>) => Promise<unknown>;
  delete?: () => Promise<unknown>;
  app_session_uuid?: string;
  appSessionUuid?: string;
};

const sessions = new Map<string, AgentSessionHandle>();
/** 当前对话进行中的 Host run，供 Stop 时调用 session.cancel。 */
const activeRuns = new Map<string, { session: AgentSessionHandle; runId: string }>();

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

function isAsyncIterable(value: unknown): value is AsyncIterable<unknown> {
  return Boolean(value && typeof value === "object" && Symbol.asyncIterator in (value as object));
}

function extractText(value: unknown): string {
  if (typeof value === "string") return value;
  if (!isRecord(value)) return "";
  if (typeof value.text === "string") return value.text;
  if (typeof value.content === "string") return value.content;
  if (isRecord(value.content) && typeof value.content.text === "string") return value.content.text;
  const choices = value.choices;
  if (Array.isArray(choices)) {
    return choices.map((choice) => {
      if (!isRecord(choice)) return "";
      const delta = choice.delta;
      return isRecord(delta) && typeof delta.content === "string" ? delta.content : "";
    }).join("");
  }
  return "";
}

function unwrapToolPayload(value: unknown, depth = 0): Record<string, unknown> | null {
  // Host / Executa 常嵌套 {success, data:{success, data:{...}}}；最多剥 4 层。
  if (!isRecord(value) || depth > 4) return isRecord(value) ? value : null;
  if (isRecord(value.data)) {
    const nested = unwrapToolPayload(value.data, depth + 1);
    if (nested) {
      // 优先内层业务字段，但保留外层 run 状态（若内层没有）
      return {
        ...value,
        ...nested,
        data: undefined,
      };
    }
  }
  return value;
}

function extractToolOutcome(frame: Record<string, unknown>): AgentToolOutcome | null {
  const payload = frame.data ?? frame.result ?? frame.tool_result ?? frame.toolResult;
  // tool_end 帧：{name, output} 中 output 可能是 JSON 字符串
  const rawOutput = frame.output ?? (isRecord(frame.delta) ? frame.delta : null);
  let candidate: unknown = payload ?? rawOutput;
  if (!payload && isRecord(rawOutput)) candidate = rawOutput.output ?? rawOutput;
  if (typeof candidate === "string") {
    try {
      candidate = JSON.parse(candidate);
    } catch {
      return null;
    }
  }
  const unwrapped = unwrapToolPayload(candidate);
  return unwrapped;
}

function extractRunId(value: unknown): string {
  if (!isRecord(value)) return "";
  const direct = value.run_id ?? value.runId;
  if (typeof direct === "string" && direct.trim()) return direct.trim();
  if (isRecord(value.payload)) {
    const nested = value.payload.run_id ?? value.payload.runId;
    if (typeof nested === "string" && nested.trim()) return nested.trim();
  }
  if (Array.isArray(value.choices)) {
    for (const choice of value.choices) {
      if (!isRecord(choice) || !isRecord(choice.delta)) continue;
      const meta = choice.delta.run_meta ?? choice.delta;
      if (isRecord(meta)) {
        const fromMeta = meta.run_id ?? meta.runId;
        if (typeof fromMeta === "string" && fromMeta.trim()) return fromMeta.trim();
      }
    }
  }
  return "";
}

function rememberActiveRun(conversationId: string, session: AgentSessionHandle, runId: string) {
  if (!runId) return;
  activeRuns.set(conversationId, { session, runId });
}

async function createSession(client: any): Promise<AgentSessionHandle> {
  const agent = client?.agent;
  if (!agent || typeof agent.session !== "function") {
    throw new Error("anna.agent.session is unavailable in this runtime");
  }
  const session = await agent.session({
    submode: "auto",
    systemPrompt: AI_SIDEBAR_SYSTEM_PROMPT,
  });
  if (!session || typeof session.run !== "function") {
    throw new Error("anna.agent.session returned an invalid session");
  }
  return session as AgentSessionHandle;
}

/**
 * 中止指定对话当前 Host Agent run。
 * AbortController 只能停前端消费；必须再调 session.cancel 才会停 Host 侧任务。
 */
export async function cancelAiAgentTurn(conversationId: string): Promise<void> {
  const active = activeRuns.get(conversationId);
  activeRuns.delete(conversationId);
  if (!active?.runId) return;
  const { session, runId } = active;
  if (typeof session.cancel !== "function") return;
  try {
    await session.cancel(runId);
  } catch {
    // 部分 Host SDK 接受对象形参
    try {
      await session.cancel({ run_id: runId, runId });
    } catch {
      // best-effort：UI 已停止，Host 取消失败只记入控制台由调用方处理
    }
  }
}

export async function runAiAgentTurn(
  client: any,
  conversationId: string,
  content: string,
  options: {
    signal?: AbortSignal;
    onText?: (text: string) => void;
    onToolOutcome?: (outcome: AgentToolOutcome) => void;
  } = {},
): Promise<AgentTurnResult> {
  let session = sessions.get(conversationId);
  if (!session) {
    session = await createSession(client);
    sessions.set(conversationId, session);
  }

  if (options.signal?.aborted) {
    throw new DOMException("The request was aborted.", "AbortError");
  }

  const stream = await session.run({ content }, { timeoutMs: 300_000, signal: options.signal });
  rememberActiveRun(conversationId, session, extractRunId(stream));

  let text = "";
  const toolOutcomes: AgentToolOutcome[] = [];
  const pushOutcome = (outcome: AgentToolOutcome | null) => {
    if (!outcome) return;
    toolOutcomes.push(outcome);
    options.onToolOutcome?.(outcome);
  };

  const consume = (raw: unknown) => {
    if (!isRecord(raw)) return;
    const event = String(raw.event || raw.type || "");
    const runId = extractRunId(raw);
    if (runId) rememberActiveRun(conversationId, session!, runId);
    if (event === "tool_result" || event === "toolResult") {
      pushOutcome(extractToolOutcome(raw));
    }
    // harness / 部分 Host：rpc.stream → choices[].delta.tool_end.output
    const choices = Array.isArray(raw.choices) ? raw.choices : [];
    for (const choice of choices) {
      if (!isRecord(choice) || !isRecord(choice.delta)) continue;
      const toolEnd = choice.delta.tool_end ?? choice.delta.toolEnd;
      if (isRecord(toolEnd)) {
        pushOutcome(extractToolOutcome(toolEnd));
      }
      const toolResult = choice.delta.tool_result ?? choice.delta.toolResult;
      if (isRecord(toolResult)) {
        pushOutcome(extractToolOutcome(toolResult));
      }
    }
    // 嵌套 payload.choices（rpc.stream 外壳）
    if (isRecord(raw.payload)) {
      const nestedChoices = Array.isArray(raw.payload.choices) ? raw.payload.choices : [];
      for (const choice of nestedChoices) {
        if (!isRecord(choice) || !isRecord(choice.delta)) continue;
        const toolEnd = choice.delta.tool_end ?? choice.delta.toolEnd;
        if (isRecord(toolEnd)) {
          pushOutcome(extractToolOutcome(toolEnd));
        }
      }
      // 再解一层：{event:rpc.stream, payload:{payload:{choices...}}}
      if (isRecord(raw.payload.payload)) {
        const innerChoices = Array.isArray(raw.payload.payload.choices)
          ? raw.payload.payload.choices
          : [];
        for (const choice of innerChoices) {
          if (!isRecord(choice) || !isRecord(choice.delta)) continue;
          const toolEnd = choice.delta.tool_end ?? choice.delta.toolEnd;
          if (isRecord(toolEnd)) {
            pushOutcome(extractToolOutcome(toolEnd));
          }
        }
      }
    }
    const piece = extractText(raw);
    if (piece) {
      // 部分 Host 仅在 final 给全文；避免与已收到的 delta 重复拼接。
      const nextText = event === "final" && piece.startsWith(text) ? piece : `${text}${piece}`;
      const delta = nextText.slice(text.length);
      text = nextText;
      if (delta) options.onText?.(delta);
    }
    if (event === "error") {
      throw new Error(String(raw.message || raw.error || "Agent run failed"));
    }
  };

  try {
    if (isAsyncIterable(stream)) {
      for await (const frame of stream) {
        if (options.signal?.aborted) {
          await cancelAiAgentTurn(conversationId);
          throw new DOMException("The request was aborted.", "AbortError");
        }
        consume(frame);
      }
    } else if (Array.isArray(stream)) {
      for (const frame of stream) {
        if (options.signal?.aborted) {
          await cancelAiAgentTurn(conversationId);
          throw new DOMException("The request was aborted.", "AbortError");
        }
        consume(frame);
      }
    } else {
      consume(stream);
    }
    return { text: stripTerminalDoneMarker(text).trim(), toolOutcomes };
  } finally {
    // 正常结束或异常后清理 active run；Stop 路径会先 cancel 再 abort
    if (!options.signal?.aborted) {
      activeRuns.delete(conversationId);
    }
  }
}

export async function clearAiAgentSession(conversationId: string): Promise<void> {
  await cancelAiAgentTurn(conversationId);
  const session = sessions.get(conversationId);
  sessions.delete(conversationId);
  await session?.delete?.();
}
