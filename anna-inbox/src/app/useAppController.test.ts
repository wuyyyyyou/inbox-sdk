import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { ensureConfirmedThreadReference, stripInternalThreadReferences } from "./aiThreadReferences";

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
      /const currentSettings = await loadInboxSettings\(currentMailbox\)/,
    );
    expect(controllerSource).toMatch(
      /preloadMailboxSnapshot\(currentMailbox, rangeDays\)/,
    );
    expect(controllerSource).toMatch(
      /await Promise\.all\(\[[\s\S]*?loadInboxSettings\(currentMailbox\),/,
    );
  });

  it("discovers the current platform mailbox before loading its snapshot", () => {
    expect(controllerSource.indexOf("const mailboxState = await loadMailboxes();")).toBeLessThan(
      controllerSource.indexOf("inboxAvailable = await preloadMailboxSnapshot(currentMailbox, rangeDays);"),
    );
    expect(controllerSource).toContain('lastSnapshotSourceRef.current === "cache"');
    expect(controllerSource).toContain("void silentSyncInbox(rangeDays, currentMailbox);");
  });
});

describe("toast queue", () => {
  it("keeps a bounded queue with independent expiration callbacks", () => {
    expect(controllerSource).toContain("const MAX_VISIBLE_TOASTS = 4");
    expect(controllerSource).toContain("const toastTimers = useRef(new Map<string, number>())");
    expect(controllerSource).toContain("onExpire?: () => void | Promise<void>");
    expect(controllerSource).toContain("while (toastItemsRef.current.length >= MAX_VISIBLE_TOASTS)");
    expect(controllerSource).toContain("expireToast(toastItemsRef.current[0].id);");
  });

  it("removes an action toast before invoking its callback so undo cannot expire afterward", () => {
    const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
    expect(appSource).toContain("<div className=\"toast-stack\"");
    expect(appSource).toContain("toasts.map((toast)");
    expect(appSource.indexOf("dismissToast(toast.id);")).toBeLessThan(
      appSource.indexOf("toast.onAction?.();"),
    );
  });
});

describe("Manage Splits tooltip", () => {
  it("does not render the retired Manage Splits control", () => {
    expect(homeViewSource).not.toContain('aria-label={t("mail.manageSplits")}');
  });
});

describe("AI scan invocation", () => {
  it("normalizes artifacts from every supported Host/local result shape", () => {
    expect(controllerSource).toContain("function normalizeAiArtifactPayload");
    expect(controllerSource).toMatch(/for \(const key of \[\"artifact\", \"artifacts\", \"data\", \"result\"\]\)/);
    expect(controllerSource).toContain("artifact: found[0]");
    expect(controllerSource).toContain("artifacts: found");
  });

  it("keeps completed batch drafts on the pending message while the next draft is generating", () => {
    expect(controllerSource).toContain("function partialBatchDraftArtifacts");
    expect(controllerSource).toContain("partialBatchDraftArtifacts(status.partial)");
    expect(controllerSource).toContain("artifacts: partialDrafts");
  });

  it("renders every final batch draft artifact even when a primary artifact is present", () => {
    expect(homeViewSource).toContain("const batchArtifacts =");
    expect(homeViewSource).toContain("Array.isArray(message.artifacts) && message.artifacts.length");
    expect(homeViewSource).not.toContain("Array.isArray(message.artifacts) && message.artifacts.length > 1");
  });

  it("keeps every completed batch compose artifact while the batch is still running", () => {
    expect(controllerSource).toContain("function partialBatchComposeArtifacts");
    expect(controllerSource.match(/composeArtifacts: streamedComposeArtifacts/g)?.length || 0).toBeGreaterThanOrEqual(2);
    expect(controllerSource).toMatch(/value\.batch_compose_draft \? \[value\.batch_compose_draft\]/);
  });

  it("exposes one batch compose draft RPC instead of one RPC per card", () => {
    expect(controllerSource).toContain("saveComposeDraftBatch(mailbox, drafts)");
    expect(controllerSource).toContain("client.saveComposeDraftBatch");
  });

  it("merges final compose artifacts with streamed partials and preserves cards on error", () => {
    expect(controllerSource).toContain("const finalComposeArtifacts = resolvedComposeArtifacts.length");
    expect(controllerSource).toContain("composeArtifacts: finalComposeArtifacts.length ? finalComposeArtifacts : undefined");
    expect(controllerSource).toContain("composeArtifacts: streamedComposeArtifacts.length ? streamedComposeArtifacts : undefined");
    expect(controllerSource).toContain("回复“继续”可继续生成");
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
    expect(controllerSource).toMatch(/recent_conversation: _recentConversation/);
    expect(controllerSource).toMatch(/Host Session 自己维护多轮对话/);
  });

  it("uses the Inbox display range as the AI scan time range", () => {
    expect(controllerSource).toMatch(/scan_window_days: clampInt\(settings\.display_range_days, plan\.scan_window_days, 1, 90\)/);
  });

  it("defaults to local Sampling while keeping storage host as the Session override", () => {
    expect(controllerSource).toMatch(/runAiAgentTurn\(/);
    expect(controllerSource).toMatch(/buildAiAgentContent\(/);
    expect(controllerSource).toMatch(/buildAiTurnUiContext\(/);
    expect(controllerSource).toMatch(/resolveAiSidebarMode\(/);
    expect(controllerSource).toContain('const aiSidebarBackendModeRef = useRef<AiSidebarMode>("local")');
    expect(controllerSource).toMatch(/selected_threads:/);
    const sidebarHandler = controllerSource.match(/async sendAiChatMessage\(options = \{\}\)[\s\S]*?retryAiMessage\(messageId\)/)?.[0] || "";
    // 默认 local 走 start_ai_turn；storage host 开关下走 Session。
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

  it("allows the current mail context thread before cleaning assistant references", () => {
    expect(controllerSource).toContain("function currentMailContextThread");
    expect(controllerSource).toMatch(
      /String\(payload\.kind \|\| \"\"\) !== \"mail_context\"[\s\S]*?String\(context\.kind \|\| \"\"\) !== \"thread\"/,
    );
    expect(controllerSource).toMatch(
      /const confirmedThreadIds = confirmedEvidenceThreadIds\(payload\);[\s\S]*?const currentMailContextThreadRef = currentMailContextThread\(payload\);[\s\S]*?confirmedThreadIds\.add\(currentMailContextThreadRef\.threadId\);[\s\S]*?ensureConfirmedThreadReference\(/,
    );
    expect(controllerSource).toContain('context.subject || "").replace(/\\s+/g, " ").trim().slice(0, 160)');
    expect(controllerSource).toContain('subject || "Open email"');
  });

  it("adds other confirmed references when their subjects are present", () => {
    const result = ensureConfirmedThreadReference(
      "已找到两封邮件：\n- Your receipt from Eleven Labs Inc. [THREAD_REF_eleven]\n- HK$54.00 payment to X was unsuccessful again",
      new Set(["eleven", "x-payment"]),
      {
        eleven: "Your receipt from Eleven Labs Inc.",
        "x-payment": "HK$54.00 payment to X was unsuccessful again",
      },
    );

    expect(result).toContain("[THREAD_REF_eleven]");
    expect(result).toContain("[THREAD_REF_x-payment]");
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

  it("hides internal references while the answer is still streaming", () => {
    expect(stripInternalThreadReferences("结论 [THREADREF_19e5d3b43916cdc6]"))
      .toBe("结论");
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

  it("persists every normalized draft artifact with the submitted source prompt", () => {
    const detailPromptHandler = controllerSource.match(
      /async submitMailContextPrompt\(request\)[\s\S]*?async sendInboxThreadReply/,
    )?.[0] || "";

    expect(detailPromptHandler).toContain("const artifacts: DraftReplyArtifact[] = isDraftRequest");
    expect(detailPromptHandler).toContain("(payload.artifacts || []).map((artifact) => ({");
    expect(detailPromptHandler).toContain("source_prompt: request.visiblePrompt");
    expect(detailPromptHandler).toContain("artifacts: artifacts.length ? artifacts : undefined");
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

  it("loads only the target mailbox Ask history and clears the active conversation on switch", () => {
    expect(controllerSource).toContain("void saveCurrentAiConversationBeforeMailboxSwitch(previousMailbox)");
    expect(controllerSource).toContain("void loadAiAskHistory(primary)");
    expect(controllerSource).toContain("askHistory: []");
    expect(controllerSource).toContain("aiChatMessages: []");
  });

  it("persists Ask history through the APS-scoped client and clears only the current mailbox", () => {
    expect(controllerSource).toContain("client.saveAiAskHistory");
    expect(controllerSource).toContain("await saveAiAskHistory(mailbox, [])");
    expect(controllerSource).not.toMatch(/async clearHistory\(\)[\s\S]*?client\.clearHistory\(\)/);
  });
});

describe("indexed search request races", () => {
  it("guards first-page responses and errors by request sequence, mailbox, and query", () => {
    expect(controllerSource).toContain("const indexedSearchRequestSequence = useRef(0);");
    expect(controllerSource).toContain("const requestId = ++indexedSearchRequestSequence.current;");
    expect(controllerSource).toMatch(
      /requestId === indexedSearchRequestSequence\.current[\s\S]*?normalizedMailbox\(current\.selectedMailboxes\[0\] \|\| current\.mailbox\) === normalizedMailbox\(mailbox\)/,
    );
    expect(controllerSource).toMatch(
      /setState\(\(current\) => isCurrentSearch\(current, true\)[\s\S]*?indexedSearchMessages: payload\.messages \|\| \[\]/,
    );
    expect(controllerSource).toMatch(
      /catch \(error\)[\s\S]*?setState\(\(current\) => isCurrentSearch\(current, true\)[\s\S]*?indexedSearchMessages: \[\]/,
    );
  });

  it("invalidates indexed searches on unmount and mailbox switch without changing load-more guards", () => {
    expect(controllerSource).toContain("indexedSearchRequestSequence.current += 1;");
    expect(controllerSource).toMatch(
      /if \(current\.indexedSearchQuery !== query\) return \{ \.\.\.current, indexedSearchLoading: false \};/,
    );
  });
});
