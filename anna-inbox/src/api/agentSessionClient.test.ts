import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { AI_SIDEBAR_SYSTEM_PROMPT } from "../app/aiAgentSystemPrompt";
import { stripTerminalDoneMarker } from "./agentSessionClient";

const agentClientSource = readFileSync(
  new URL("./agentSessionClient.ts", import.meta.url),
  "utf8",
);

const toolManifest = JSON.parse(
  readFileSync(new URL("../../../inbox-tool/manifest.json", import.meta.url), "utf8"),
) as { tools?: Array<{ name?: string; description?: string }> };

describe("AI sidebar Host Agent contract", () => {
  it("uses the official session systemPrompt instead of a custom rule field", () => {
    expect(agentClientSource).toContain("systemPrompt: AI_SIDEBAR_SYSTEM_PROMPT");
    expect(AI_SIDEBAR_SYSTEM_PROMPT.length).toBeLessThanOrEqual(4000);
    expect(AI_SIDEBAR_SYSTEM_PROMPT).toContain("Never use Markdown tables");
    expect(AI_SIDEBAR_SYSTEM_PROMPT).toContain("6 searches and 45 candidates");
    expect(AI_SIDEBAR_SYSTEM_PROMPT).toContain("local inbox cache only");
    expect(AI_SIDEBAR_SYSTEM_PROMPT).toContain("Never call start_ai_turn");
    expect(agentClientSource).toContain("tool_end");
  });

  it("exposes non-mutation search/read tools via Executa describe", () => {
    const names = new Set((toolManifest.tools || []).map((tool) => String(tool.name || "")));
    expect(names.has("search_email")).toBe(true);
    expect(names.has("read_email")).toBe(true);
    expect(names.has("propose_inbox_actions")).toBe(true);
    const search = (toolManifest.tools || []).find((tool) => tool.name === "search_email");
    expect(String(search?.description || "")).toMatch(/bodySnippet|LOCAL inbox cache/i);
    expect(String(search?.description || "")).not.toMatch(/bodyFull.*default/i);
  });

  it("declares the Host Agent tool whitelist with fully qualified Executa names", () => {
    const appManifest = JSON.parse(
      readFileSync(new URL("../../manifest.json", import.meta.url), "utf8"),
    ) as { ui?: { host_api?: { agent?: { tools?: string[] } } } };
    const tools = appManifest.ui?.host_api?.agent?.tools || [];
    const prefix = "tool_riazm4777_inbox_executa_dnsb9fqu__";

    expect(tools).toHaveLength(9);
    expect(tools).toContain(`${prefix}search_email`);
    expect(tools).toContain(`${prefix}read_email`);
    expect(tools).not.toContain(`${prefix}start_ai_turn`);
    expect(tools).not.toContain(`${prefix}get_mail_agent_run`);
    expect(tools).not.toContain(`${prefix}list_cached_emails`);
    expect(tools).not.toContain(`${prefix}apply_proposed_actions`);
    expect(tools).not.toContain(`${prefix}send_mail`);
  });

  it("removes only a terminal standalone DONE marker", () => {
    expect(stripTerminalDoneMarker("Answer\n[DONE]")).toBe("Answer");
    expect(stripTerminalDoneMarker("[DONE] means complete.")).toBe("[DONE] means complete.");
  });

  it("cancels the active Host Agent run on stop, not only AbortController", () => {
    expect(agentClientSource).toContain("export async function cancelAiAgentTurn");
    expect(agentClientSource).toContain("session.cancel");
    expect(agentClientSource).toContain("activeRuns");
  });
});
