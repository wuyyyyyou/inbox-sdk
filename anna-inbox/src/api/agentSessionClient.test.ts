import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { AI_SIDEBAR_SYSTEM_PROMPT } from "../app/aiAgentSystemPrompt";
import { runAiAgentTurn, stripTerminalDoneMarker } from "./agentSessionClient";

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
    expect(AI_SIDEBAR_SYSTEM_PROMPT).toContain("call query_mail_evidence exactly once");
    expect(AI_SIDEBAR_SYSTEM_PROMPT).toContain("creates one QueryPlan");
    expect(AI_SIDEBAR_SYSTEM_PROMPT).toContain("recent_conversation is the recent visible transcript");
    expect(AI_SIDEBAR_SYSTEM_PROMPT).toContain("Do not replace a clear confirmation with a generic feature menu");
    expect(AI_SIDEBAR_SYSTEM_PROMPT).toContain("base conclusions on bodyFull evidence");
    expect(AI_SIDEBAR_SYSTEM_PROMPT).toContain("Never describe it as a 7/30/60-day search");
    expect(AI_SIDEBAR_SYSTEM_PROMPT).toContain("Never call start_ai_turn");
    expect(AI_SIDEBAR_SYSTEM_PROMPT).toContain("each confirmed email must be its own list item");
    expect(AI_SIDEBAR_SYSTEM_PROMPT).toContain("Never add a detached reference list");
    expect(agentClientSource).toContain("tool_end");
  });

  it("exposes the composite read-only evidence tool via Executa describe", () => {
    const names = new Set((toolManifest.tools || []).map((tool) => String(tool.name || "")));
    expect(names.has("query_mail_evidence")).toBe(true);
    expect(names.has("propose_inbox_actions")).toBe(true);
    const evidence = (toolManifest.tools || []).find((tool) => tool.name === "query_mail_evidence");
    expect(String(evidence?.description || "")).toMatch(/QueryPlan|cache/i);
  });

  it("declares the Host Agent tool whitelist with fully qualified Executa names", () => {
    const appManifest = JSON.parse(
      readFileSync(new URL("../../manifest.json", import.meta.url), "utf8"),
    ) as { ui?: { host_api?: { agent?: { tools?: string[] } } } };
    const tools = appManifest.ui?.host_api?.agent?.tools || [];
    const prefix = "tool_riazm4777_inbox_executa_dnsb9fqu__";

    expect(tools).toHaveLength(7);
    expect(tools).toContain(`${prefix}query_mail_evidence`);
    expect(tools).not.toContain(`${prefix}search_email`);
    expect(tools).not.toContain(`${prefix}read_email`);
    expect(tools).not.toContain(`${prefix}start_ai_turn`);
    expect(tools).not.toContain(`${prefix}get_mail_agent_run`);
    expect(tools).not.toContain(`${prefix}list_cached_emails`);
    expect(tools).not.toContain(`${prefix}apply_proposed_actions`);
    expect(tools).not.toContain(`${prefix}send_mail`);
  });

  it("removes only a terminal standalone DONE marker", () => {
    expect(stripTerminalDoneMarker("Answer\n[DONE]")).toBe("Answer");
    expect(stripTerminalDoneMarker("Answer\n[DONE]\nMore detail")).toBe("Answer\n\nMore detail");
    expect(stripTerminalDoneMarker("[DONE] means complete.")).toBe("[DONE] means complete.");
  });

  it("keeps confirmed evidence from a JSON-encoded tool_end output", async () => {
    const result = await runAiAgentTurn(
      {
        agent: {
          session: async () => ({
            run: () => [{
              choices: [{
                delta: {
                  tool_end: {
                    name: "query_mail_evidence",
                    output: JSON.stringify({
                      success: true,
                      data: {
                        match_status: "confirmed",
                        results: [{ thread_id: "thread-123" }],
                      },
                    }),
                  },
                },
              }],
            }],
          }),
        },
      },
      "conversation-1",
      "Find the invoice email",
    );

    expect(result.toolOutcomes).toContainEqual(expect.objectContaining({
      match_status: "confirmed",
      results: [{ thread_id: "thread-123" }],
    }));
  });

  it("cancels the active Host Agent run on stop, not only AbortController", () => {
    expect(agentClientSource).toContain("export async function cancelAiAgentTurn");
    expect(agentClientSource).toContain("session.cancel");
    expect(agentClientSource).toContain("activeRuns");
  });
});
