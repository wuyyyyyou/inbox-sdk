import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { ensureConfirmedThreadReference } from "./aiThreadReferences";

const controllerSource = readFileSync(
  new URL("./useAppController.ts", import.meta.url),
  "utf8",
);
const homeViewSource = readFileSync(
  new URL("../features/home/HomeView.tsx", import.meta.url),
  "utf8",
);
const threadReferencesSource = readFileSync(
  new URL("./aiThreadReferences.ts", import.meta.url),
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
      /<button[\s\S]*?aria-label=\{t\("mail\.manageSplits"\)\}[\s\S]*?<\/button>/,
    )?.[0];

    expect(manageSplitsButton).toContain('data-tooltip={t("mail.manageSplits")}');
    expect(manageSplitsButton).not.toContain('title="Manage Splits"');
  });
});

describe("AI scan invocation", () => {
  it("normalizes artifacts from every supported Host/local result shape", () => {
    expect(controllerSource).toContain("function normalizeAiArtifactPayload");
    expect(controllerSource).toMatch(/for \(const key of \[\"artifact\", \"artifacts\", \"data\", \"result\"\]\)/);
    expect(controllerSource).toContain("artifact: found[0]");
    expect(controllerSource).toContain("artifacts: found");
  });

  it("leaves draft card creation to the Host artifact", () => {
    expect(controllerSource).not.toContain("function isThreadDraftRequest");
    expect(controllerSource).not.toContain("function canRecoverThreadDraft");
    expect(controllerSource).not.toContain("client.draftAiReply({");
    expect(controllerSource).not.toContain("两步草稿流");
  });

  it("prioritizes a Host draft artifact over a later evidence result", () => {
    expect(controllerSource).toContain("function latestDraftOutcome");
    expect(controllerSource).toContain('|| (draftOutcome ? "draft" : "")');
    expect(controllerSource).toContain("...(draftOutcome || {})");
  });

  it("keeps the current Scan Plan for remaining pollable scans and passes it to Agent context", () => {
    expect(controllerSource.match(/wait_timeout_seconds: 45/g)?.length || 0).toBeGreaterThanOrEqual(1);
    expect(controllerSource.match(/scan_window_days: scanScope\.scan_window_days/g)?.length || 0).toBeGreaterThanOrEqual(2);
    expect(controllerSource.match(/max_messages: scanScope\.max_messages/g)?.length || 0).toBeGreaterThanOrEqual(2);
    expect(controllerSource).toMatch(/max_messages: plan\.max_messages/);
    expect(controllerSource).toMatch(/display_range_days: rangeDays/);
    expect(controllerSource).toMatch(/const \{ display_range_days: _displayRangeDays, \.\.\.sidebarUiContext \} = uiContext/);
    expect(controllerSource).toMatch(/recent_conversation: recentConversation/);
    expect(controllerSource).toMatch(/\.slice\(-4\)/);
  });

  it("uses the Inbox display range as the AI scan time range", () => {
    expect(controllerSource).toMatch(/scan_window_days: clampInt\(settings\.display_range_days, plan\.scan_window_days, 1, 90\)/);
  });

  it("wires Host Agent session with optional local start_ai_turn sidebar path", () => {
    expect(controllerSource).toMatch(/runAiAgentTurn\(/);
    expect(controllerSource).toMatch(/buildAiAgentContent\(/);
    expect(controllerSource).toMatch(/buildAiTurnUiContext\(/);
    expect(controllerSource).toMatch(/resolveAiSidebarMode\(/);
    expect(controllerSource).toMatch(/selected_threads:/);
    const sidebarHandler = controllerSource.match(/async sendAiChatMessage\(options = \{\}\)[\s\S]*?retryAiMessage\(messageId\)/)?.[0] || "";
    // 默认 host 走 session；local 开关下走 start_ai_turn 本地 Router。
    expect(sidebarHandler).toContain("runAiAgentTurn(");
    expect(sidebarHandler).toContain("client.startAiTurn(");
    expect(sidebarHandler).toContain('sidebarMode === "local"');
    expect(sidebarHandler).toContain("aiSidebarModeByConversationRef.current.get(conversationId)");
    expect(sidebarHandler).toContain("aiSidebarModeByConversationRef.current.set(conversationId, sidebarMode)");
    expect(sidebarHandler).toContain("messages: messagesWithUser");
    expect(sidebarHandler).toContain("ui_context: sidebarUiContext");
    expect(controllerSource).toContain("search_field: args.searchField || \"\"");
    expect(controllerSource).toContain("searchField: options.searchField");
    expect(controllerSource).toContain("const agentRequest = String(options.agentPrompt || userRequest)");
    expect(controllerSource).not.toMatch(/decideAiRoute\(/);
  });

  it("keeps only confirmed thread references and attaches them to the reply", () => {
    expect(controllerSource).toContain("ensureConfirmedThreadReference");
    expect(controllerSource).toContain("confirmedEvidenceThreadIdsFromOutcomes");
    expect(controllerSource).toContain('from "./aiThreadReferences"');
    expect(threadReferencesSource).toContain("removeDirectMailLinks");
    expect(threadReferencesSource).toContain("isGmailMessageUrl");
    expect(threadReferencesSource).toContain("hostname === \"mail.google.com\"");
    expect(threadReferencesSource).toContain("line.toLowerCase().includes(subject)");
    expect(threadReferencesSource).toContain("const fallbackLineIndex");
    expect(threadReferencesSource).toContain("const fallbackLineIndex = lines.findIndex");
    expect(threadReferencesSource).toContain("lines[fallbackLineIndex].trimEnd()");
    expect(threadReferencesSource).toContain("const fallbackThreadId");
    expect(threadReferencesSource).toContain("${lines[lineIndex].trimEnd()} [THREAD_REF_${threadId}]");
    expect(threadReferencesSource).toContain("[THREAD_REF_${threadId}]");
    expect(controllerSource).toContain("confirmedEvidenceThreadIds(payload)");
    expect(controllerSource).toContain("confirmedEvidenceThreadLabels(payload, confirmedThreadIds)");
    expect(controllerSource).toContain("evidence_thread_labels");
    expect(threadReferencesSource).toContain("THREAD_?REF_?");
  });

  it("does not append an unrelated confirmed candidate after an inline evidence reference", () => {
    const result = ensureConfirmedThreadReference(
      "该收据的实付金额为 **$11.00**。 [THREAD_REF_eleven]",
      new Set(["eleven", "x-payment"]),
      {
        eleven: "Your receipt from Eleven Labs Inc.",
        "x-payment": "HK$54.00 payment to X was unsuccessful again",
      },
    );

    expect(result).toContain("[THREAD_REF_eleven]");
    expect(result).not.toContain("THREAD_REF_x-payment");
  });

  it("renders evidence references as compact subject links", () => {
    expect(homeViewSource).toContain("truncateThreadReferenceLabel");
    expect(homeViewSource).toContain("const limit = 48");
    expect(homeViewSource).toContain("title={fullLabel}");
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
    expect(controllerSource).toMatch(/aiSidebarModeByConversationRef\.current\.delete\(previousConversationId\)/);
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

  it("keeps cached messages visible while the background History sync runs", () => {
    expect(controllerSource).toContain(
      "const hasVisibleInbox = s.inboxSnapshotMessages.length > 0 || s.inboxMessages.length > 0;",
    );
    expect(controllerSource).toContain("inboxSnapshotLoading: !hasVisibleInbox,");
  });

  it("invalidates mail and draft requests when switching mailboxes", () => {
    expect(controllerSource).toMatch(
      /inboxRequestSequence\.current \+= 1;[\s\S]*draftRequestSequence\.current \+= 1;/,
    );
  });
});
