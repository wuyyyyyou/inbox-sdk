import { describe, expect, it } from "vitest";
import { unwrapToolResult } from "./mailAgentClient";

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
});
