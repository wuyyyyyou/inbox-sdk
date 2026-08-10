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

  it("routes AI Ask history reads and writes to APS with mailbox scope", async () => {
    const invoke = vi.fn().mockResolvedValue({ success: true, data: { mailbox: "owner@example.com", entries: [], etag: "etag-1" } });
    const client = new MailAgentClient(async () => ({
      connected: true,
      mode: "host",
      client: { tools: { invoke } },
    } as never));

    await client.loadAiAskHistory("owner@example.com");
    await client.saveAiAskHistory("owner@example.com", [], "etag-1");

    expect(invoke).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({
        method: "get_ai_ask_history",
        args: { mailbox: "owner@example.com", storage_provider: "aps" },
      }),
      expect.objectContaining({ timeoutMs: 180_000 }),
    );
    expect(invoke).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({
        method: "save_ai_ask_history",
        args: { mailbox: "owner@example.com", storage_provider: "aps", if_match: "etag-1", entries: [] },
      }),
      expect.objectContaining({ timeoutMs: 180_000 }),
    );
  });

  it("does not retry a background scan start after a transport error", async () => {
    const invoke = vi.fn().mockRejectedValue(new Error("fetch failed"));
    const client = new MailAgentClient(async () => ({
      connected: true,
      mode: "host",
      client: { tools: { invoke } },
    } as never));
    await expect(client.startCustomScan({ run_id: "scan-1" })).rejects.toThrow("fetch failed");
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(invoke).toHaveBeenCalledWith(
      expect.objectContaining({ method: "start_custom_scan", timeoutMs: 120_000 }),
      expect.objectContaining({ timeoutMs: 120_000 }),
    );
  });

  it("does not retry an inbox thread assist start after a transport error", async () => {
    const invoke = vi.fn().mockRejectedValue(new Error("fetch failed"));
    const client = new MailAgentClient(async () => ({
      connected: true,
      mode: "host",
      client: { tools: { invoke } },
    } as never));

    await expect(client.startInboxThreadAssist({ thread_id: "thread-1" })).rejects.toThrow("fetch failed");
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(invoke).toHaveBeenCalledWith(
      expect.objectContaining({ method: "start_inbox_thread_assist" }),
      expect.objectContaining({ timeoutMs: 180_000 }),
    );
  });

  it("does not retry an AI turn start after a transport error", async () => {
    const invoke = vi.fn().mockRejectedValue(new Error("fetch failed"));
    const client = new MailAgentClient(async () => ({
      connected: true,
      mode: "host",
      client: { tools: { invoke } },
    } as never));

    await expect(client.startAiTurn({ user_text: "hi" })).rejects.toThrow("fetch failed");
    expect(invoke).toHaveBeenCalledTimes(1);
  });

  it("reconnects and retries a safe read on the replacement runtime", async () => {
    const disconnectedInvoke = vi.fn().mockRejectedValue(new Error("fetch failed"));
    const recoveredInvoke = vi.fn().mockResolvedValue({ success: true, data: { run_id: "run-1", status: "done" } });
    let current = {
      connected: true,
      mode: "host",
      client: { tools: { invoke: disconnectedInvoke } },
    } as never;
    const reconnect = vi.fn(async () => {
      current = {
        connected: true,
        mode: "host",
        client: { tools: { invoke: recoveredInvoke } },
      } as never;
      return current;
    });
    const client = new MailAgentClient(async () => current, reconnect);

    await expect(client.getRun("run-1")).resolves.toMatchObject({ run_id: "run-1", status: "done" });
    expect(reconnect).toHaveBeenCalledTimes(1);
    expect(recoveredInvoke).toHaveBeenCalledTimes(1);
  });

  it("restores the runtime but does not replay a mail mutation", async () => {
    const disconnectedInvoke = vi.fn().mockRejectedValue(new Error("fetch failed"));
    const recoveredInvoke = vi.fn().mockResolvedValue({ success: true, data: { ok: true } });
    let current = {
      connected: true,
      mode: "host",
      client: { tools: { invoke: disconnectedInvoke } },
    } as never;
    const reconnect = vi.fn(async () => {
      current = {
        connected: true,
        mode: "host",
        client: { tools: { invoke: recoveredInvoke } },
      } as never;
      return current;
    });
    const client = new MailAgentClient(async () => current, reconnect);

    await expect(client.setMessageStarred("user@example.com", "message-1", true)).rejects.toThrow("fetch failed");
    expect(reconnect).toHaveBeenCalledTimes(1);
    expect(recoveredInvoke).not.toHaveBeenCalled();
  });
});
