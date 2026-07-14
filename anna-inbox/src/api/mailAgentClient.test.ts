import { describe, expect, it, vi } from "vitest";
import { MailAgentClient, unwrapToolResult } from "./mailAgentClient";

describe("unwrapToolResult", () => {
  it("unwraps jsonrpc and handle_invoke envelopes", () => {
    expect(unwrapToolResult({ jsonrpc: "2.0", result: { success: true, tool: "x", data: { ok: true } } })).toEqual({ ok: true });
  });

  it("unwraps direct success payloads", () => {
    expect(unwrapToolResult({ success: true, data: { value: 1 } })).toEqual({ value: 1 });
  });

  it("throws on error payloads", () => {
    expect(() => unwrapToolResult({ error: "bad" })).toThrow("bad");
    expect(() => unwrapToolResult({ success: false, error: "failed" })).toThrow("failed");
  });

  it("exposes startAiTurn with the custom-scan invoke timeout", async () => {
    const invoke = vi.fn().mockResolvedValue({ success: true, data: { run_id: "at-1", status: "done" } });
    const client = new MailAgentClient(async () => ({
      connected: true,
      mode: "host",
      client: { tools: { invoke } },
    } as never));
    await expect(client.startAiTurn({ user_text: "hi", run_id: "at-1" })).resolves.toMatchObject({ run_id: "at-1" });
    expect(invoke).toHaveBeenCalledWith(
      expect.objectContaining({ method: "start_ai_turn", timeoutMs: 120_000 }),
      expect.objectContaining({ timeoutMs: 120_000 }),
    );
  });

  it("retries a safe AI scan invocation after an HTML response", async () => {
    vi.useFakeTimers();
    const invoke = vi.fn()
      .mockRejectedValueOnce(new Error("Unexpected token '<', '<!DOCTYPE' is not valid JSON"))
      .mockResolvedValue({ success: true, data: { run_id: "scan-1", status: "queued" } });
    const client = new MailAgentClient(async () => ({
      connected: true,
      mode: "host",
      client: { tools: { invoke } },
    } as never));
    const resultPromise = client.startCustomScan({ run_id: "scan-1" });
    await vi.runAllTimersAsync();
    await expect(resultPromise).resolves.toMatchObject({ run_id: "scan-1", status: "queued" });
    expect(invoke).toHaveBeenCalledTimes(2);
    expect(invoke).toHaveBeenLastCalledWith(
      expect.objectContaining({ timeoutMs: 120_000 }),
      expect.objectContaining({ timeoutMs: 120_000 }),
    );
    vi.useRealTimers();
  });
});
