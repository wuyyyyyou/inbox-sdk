import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const controllerSource = readFileSync(
  new URL("./useAppController.ts", import.meta.url),
  "utf8",
);
const homeViewSource = readFileSync(
  new URL("../features/home/HomeView.tsx", import.meta.url),
  "utf8",
);

describe("inbox startup settings", () => {
  it("loads inbox settings before snapshot preload so display range is respected", () => {
    expect(controllerSource).toMatch(
      /const bootSettings = await loadInboxSettings\(bootMailbox\)/,
    );
    expect(controllerSource).toMatch(
      /preloadMailboxSnapshot\(bootMailbox, rangeDays\)/,
    );
    expect(controllerSource).toMatch(
      /await Promise\.all\(\[[\s\S]*?loadInboxSettings\(currentMailbox\),/,
    );
  });
});

describe("Manage Splits tooltip", () => {
  it("uses only the custom tooltip instead of a browser title tooltip", () => {
    const manageSplitsButton = homeViewSource.match(
      /<button[\s\S]*?aria-label="Manage Splits"[\s\S]*?<\/button>/,
    )?.[0];

    expect(manageSplitsButton).toContain('data-tooltip="Manage Splits"');
    expect(manageSplitsButton).not.toContain('title="Manage Splits"');
  });
});

describe("AI scan invocation", () => {
  it("keeps the current Scan Plan for remaining pollable scans and passes it to Agent context", () => {
    expect(controllerSource.match(/wait_timeout_seconds: 45/g)?.length || 0).toBeGreaterThanOrEqual(1);
    expect(controllerSource.match(/scan_window_days: scanScope\.scan_window_days/g)?.length || 0).toBeGreaterThanOrEqual(2);
    expect(controllerSource.match(/max_messages: scanScope\.max_messages/g)?.length || 0).toBeGreaterThanOrEqual(2);
    expect(controllerSource).toMatch(/max_messages: plan\.max_messages/);
    expect(controllerSource).toMatch(/display_range_days: rangeDays/);
  });

  it("uses the Inbox display range as the AI scan time range", () => {
    expect(controllerSource).toMatch(/scan_window_days: clampInt\(settings\.display_range_days, plan\.scan_window_days, 1, 90\)/);
  });

  it("wires the Host Agent session path for the sidebar", () => {
    expect(controllerSource).toMatch(/runAiAgentTurn\(/);
    expect(controllerSource).toMatch(/buildAiAgentContent\(/);
    expect(controllerSource).toMatch(/buildAiTurnUiContext\(/);
    expect(controllerSource).toMatch(/selected_threads:/);
    const sidebarHandler = controllerSource.match(/async sendAiChatMessage\(options = \{\}\)[\s\S]*?retryAiMessage\(messageId\)/)?.[0] || "";
    expect(sidebarHandler).not.toContain("client.startAiTurn(");
    expect(controllerSource).not.toMatch(/decideAiRoute\(/);
    expect(controllerSource).not.toMatch(/isAiTurnEnabled\(/);
  });

  it("routes thread detail prompts through the same unified AI turn", () => {
    const detailPromptHandler = controllerSource.match(/async submitMailContextPrompt\(request\)[\s\S]*?async sendInboxThreadReply/)?.[0] || "";
    expect(detailPromptHandler).toContain("client.startAiTurn(");
    expect(detailPromptHandler).not.toContain("client.startInboxMailPrompt(");
    expect(detailPromptHandler).toContain("requested_artifact: requestedArtifact");
  });

  it("clears the Host session when a sidebar conversation is discarded", () => {
    expect(controllerSource).toMatch(/clearAiAgentSession\(previousConversationId\)/);
    expect(controllerSource).toMatch(/clearAiAgentSession\(entry\.conversationId \|\| ""\)/);
  });

  it("stops the Host Agent run when the user presses Stop", () => {
    const stopHandler = controllerSource.match(/stopAiGeneration\(\) \{[\s\S]*?startNewAiConversation\(\)/)?.[0] || "";
    expect(stopHandler).toContain("cancelAiAgentTurn");
    expect(stopHandler).toContain("run.controller.abort()");
  });
});

describe("mailbox switching", () => {
  it("renders the first page from the target mailbox cache before background sync", () => {
    expect(controllerSource).toMatch(
      /const cached = await client\.listCachedEmails\(primary, rangeDays, 100, "all", 0\)/,
    );
    expect(controllerSource).toMatch(
      /void silentSyncInbox\(rangeDays, primary\)/,
    );
    expect(controllerSource).not.toMatch(
      /await preloadMailboxSnapshot\(primary, rangeDays, true, \{ skipLiveGmail: true \}\)/,
    );
  });

  it("invalidates mail and draft requests when switching mailboxes", () => {
    expect(controllerSource).toMatch(
      /inboxRequestSequence\.current \+= 1;[\s\S]*draftRequestSequence\.current \+= 1;/,
    );
  });
});
