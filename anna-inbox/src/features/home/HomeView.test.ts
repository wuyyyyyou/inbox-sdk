import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { PlusIcon, accountDisplayName, aiSearchStatus, aiThinkingElapsedLabel, composeDraftOverlayMessages, formatInboxTabCount, gmailAuthorizationError, gmailTrashUrl, hasMailboxScanError, inboxLastSyncedLabel, isAiConversationNearBottom, isDoneMessage, isDraftMessage, isGmailAuthorizationRequired, isImportantMessage, isMailFeedNearBottom, isSentMessage, isStarredMessage, isTrashMessage, isUnreadMessage, mergeDraftOverlayMessages, mergeInboxSearchSourceMessages, messageParticipant, nextFeedRangeDays, renderAiUserMessageContent, resolveSourceMessages, restoreInboxWorkflow, senderParts, shouldShowImportantIcon, transitionInboxWorkflow } from "./HomeView";

const homeViewSource = readFileSync(new URL("./HomeView.tsx", import.meta.url), "utf8");
const workflowOf = (flags: { todos: string[]; snoozed: string[]; done: string[]; doneRemoved?: string[] }) => ({
  todos: flags.todos,
  snoozed: flags.snoozed,
  done: flags.done,
  snoozedUntil: {},
  doneRemoved: flags.doneRemoved || [],
});

describe("display range switching", () => {
  it("reprojects the cached inbox instead of triggering a Gmail rescan", () => {
    expect(homeViewSource).toContain('actions.loadCachedInboxEmails("all", configuredDays, 0, false)');
    expect(homeViewSource).not.toContain("// 用户在设置里改 display range\n      void syncInbox(configuredDays, true);");
  });
});

describe("search suggestion submission", () => {
  it("submits a complete query directly and rechecks active search after sync", () => {
    expect(homeViewSource).toContain("if (parsedSearch.expression && !parsedSearch.error) {");
    expect(homeViewSource).toContain("applySearch(search);");
    expect(homeViewSource).toContain("const refreshActiveSearch = () => {");
    expect(homeViewSource).toContain("void searchIndexedEmailsRef.current(value).catch(() => undefined);");
  });

  it("keeps typing and incomplete suggestions in edit mode without executing search", () => {
    expect(homeViewSource).not.toContain("setFilter(\"search\");\n      void searchIndexedEmailsRef.current(value).catch(() => undefined);");
    expect(homeViewSource).toContain("(suggestion: string, executeComplete = false)");
    expect(homeViewSource).toContain("if (executeComplete && parsed.expression && !parsed.error)");
    expect(homeViewSource).toContain("onClick={() => applySuggestion(suggestion, true)}");
  });

  it("prioritizes explicit Enter submission over an open completion menu", () => {
    const enterHandler = homeViewSource.match(/else if \(event\.key === "Enter"\) \{[\s\S]*?\n                  \}\n                \}/)?.[0] || "";
    expect(enterHandler).toContain("if (parsedSearch.expression && !parsedSearch.error)");
    expect(enterHandler).toContain("if (hasIncompleteSuggestion)");
    expect(enterHandler).toContain("applySearch(search);");
    expect(enterHandler).toContain("applySuggestion(searchSuggestions[searchSuggestionIndex]);");
  });

  it("retains an incomplete field completion and closes the menu for direct value entry", () => {
    expect(homeViewSource).toContain("const hasIncompleteSuggestion = searchSuggestions.some");
    expect(homeViewSource).toContain("applySuggestion(searchSuggestions[searchSuggestionIndex]);");
    expect(homeViewSource).toContain("setSearchFocused(false);");
    expect(homeViewSource).toContain("window.requestAnimationFrame");
    expect(homeViewSource).toContain("const next = applyInboxQuerySuggestion(search, suggestion);");
    expect(homeViewSource).toContain("if (executeComplete && parsed.expression && !parsed.error)");
  });

  it("restores the pre-search filter without changing mailbox state when cleared", () => {
    const clearStart = homeViewSource.indexOf('setSearch("");', homeViewSource.indexOf("mail-search-clear"));
    const clearHandler = homeViewSource.slice(clearStart, clearStart + 180);
    expect(clearHandler).toContain("setFilter(filterBeforeSearch);");
    expect(clearHandler).not.toContain('setMailboxView("inbox")');
    expect(clearHandler).not.toContain("searchIndexedEmailsRef");
  });

  it("restores the pre-search filter when the input is manually deleted", () => {
    const changeHandler = homeViewSource.match(/onChange=\{\(event\) => \{[\s\S]*?setSearchSuggestionIndex\(0\);\n\s*\}\}/)?.[0] || "";
    expect(changeHandler).toContain('if (!next.trim() && (activeSearch || filter === "search"))');
    expect(changeHandler).toContain("setActiveSearch(\"\");");
    expect(changeHandler).toContain("setFilter(filterBeforeSearch);");
    expect(changeHandler).toContain("setSearchFocused(true);");
    expect(changeHandler).not.toContain("searchIndexedEmailsRef");
  });
});

describe("AI thread reference", () => {
  it("opens a detail placeholder before resolving the referenced thread page", () => {
    const handler = homeViewSource.match(/const openMailDetailFromAi = useCallback\([\s\S]*?const handleGmailThreadAction = useCallback/)?.[0] || "";
    expect(handler).toContain("const openingPlaceholder: InboxMessage");
    expect(handler.indexOf("setExternalDetailMessage(openingPlaceholder)")).toBeLessThan(
      handler.indexOf("await actions.loadInboxThreadPage"),
    );
  });

  it("ignores stale AI detail opens after the drawer is closed", () => {
    expect(homeViewSource).toContain("aiDetailOpenTokenRef");
    expect(homeViewSource).toContain("if (isStaleOpen()) return");
    const closeHandler = homeViewSource.match(/const closeDetailDrawer = useCallback\([\s\S]*?\}, \[selectedId\]\);/)?.[0] || "";
    expect(closeHandler).toContain("aiDetailOpenTokenRef.current += 1");
  });
});

describe("mail detail complete body", () => {
  it("loads a THREAD_REF fallback message automatically instead of exposing a manual full-body prompt", () => {
    const drawerSource = readFileSync(
      new URL("../mail-detail/MailDetailDrawer.tsx", import.meta.url),
      "utf8",
    );
    expect(drawerSource).toContain("|| visiblePage.messages.find((item) => item.id === visiblePage.latest_message_id)");
    expect(drawerSource).not.toContain("This message is too large to display completely.");
    expect(drawerSource).not.toContain("Load full message");
  });

  it("renders the IndexedDB thread page before waiting for the network refresh", () => {
    const drawerSource = readFileSync(
      new URL("../mail-detail/MailDetailDrawer.tsx", import.meta.url),
      "utf8",
    );
    const cacheRead = drawerSource.indexOf("const cachedPage = await cachedPagePromise");
    const cacheBranch = drawerSource.indexOf("if (cachedPage)", cacheRead);
    const cacheRender = drawerSource.indexOf("setLoading(false);", cacheRead);
    const backgroundRefresh = drawerSource.search(/void refreshNetworkPage\([^)]*\)/);

    expect(cacheRead).toBeGreaterThan(-1);
    expect(cacheRead).toBeLessThan(cacheBranch);
    expect(cacheRender).toBeGreaterThan(cacheRead);
    expect(cacheRender).toBeLessThan(backgroundRefresh);
  });

  it("renders the cached reply draft before the APS refresh and ignores late responses after editing", () => {
    const drawerSource = readFileSync(
      new URL("../mail-detail/MailDetailDrawer.tsx", import.meta.url),
      "utf8",
    );
    expect(drawerSource).toContain("getCachedInboxThreadDraft(mailbox, threadId)");
    expect(drawerSource).toContain("editSequence !== draftEditSequenceRef.current");
    expect(drawerSource).toContain("setCachedInboxThreadDraft(mailbox, threadId");
  });

  it("keeps the cached mailbox snapshot when the active mailbox changes", () => {
    const rangeEffect = homeViewSource.match(/const appliedDisplayRangeRef[\s\S]*?const lastSyncedLabel/)?.[0] || "";
    expect(rangeEffect).toContain("void syncInbox(configuredDays);");
    expect(rangeEffect).not.toContain("void syncInbox(configuredDays, true);");
  });
});

describe("AI user message rendering", () => {
  it("renders submitted line breaks as explicit break elements", () => {
    const nodes = renderAiUserMessageContent("first line\nsecond line\n\nthird line");

    expect(nodes).toHaveLength(4);
    expect(nodes[1].props.children[0].type).toBe("br");
    expect(nodes[2].props.children[0].type).toBe("br");
    expect(nodes[3].props.children[0].type).toBe("br");
  });
});

describe("workflow transitions", () => {
  const base = { todos: ["todo-1", "unrelated"], snoozed: ["snooze-1"], done: ["done-1"], snoozedUntil: { "snooze-1": "2026-01-01T00:00:00Z" } };

  it("moves affected ids without overwriting unrelated ids", () => {
    const next = transitionInboxWorkflow(base, "done", ["todo-1"], true);
    expect(next.todos).toEqual(["unrelated"]);
    expect(next.done).toEqual(["done-1", "todo-1"]);
    expect(next.snoozed).toEqual(["snooze-1"]);
  });

  it("restores only affected ids and preserves newer unrelated changes", () => {
    const changed = transitionInboxWorkflow(base, "snoozed", ["todo-1"], true, "2026-02-01T00:00:00Z");
    const newer = transitionInboxWorkflow(changed, "done", ["unrelated"], true);
    const restored = restoreInboxWorkflow(newer, base, ["todo-1"]);
    expect(restored.todos).toEqual(["todo-1"]);
    expect(restored.done).toEqual(["done-1", "unrelated"]);
    expect(restored.snoozed).toEqual(["snooze-1"]);
    expect(restored.snoozedUntil).toEqual(base.snoozedUntil);
  });
});

describe("legacy AI draft preview", () => {
  it("only renders structured draft artifacts after confirmation", () => {
    expect(homeViewSource).not.toContain("parseLegacyDraftPreview(message)");
    expect(homeViewSource).toContain('message.artifact?.type === "draft_reply"');
  });

  it("keeps the first reply step as a confirmation request", () => {
    const promptSource = readFileSync(
      new URL("../../app/aiAgentSystemPrompt.ts", import.meta.url),
      "utf8",
    );
    expect(promptSource).toContain("ask whether to generate a reply draft");
    expect(promptSource).toContain("Do not output the draft body");
  });
});

describe("compose draft synchronization feedback", () => {
  it("renders the local draft mirror before refreshing APS in the background", () => {
    expect(homeViewSource).toContain("getCachedComposeDraftDirectory(mailbox)");
    expect(homeViewSource).toContain("if (!cancelled) void refreshStoredDrafts();");
    expect(homeViewSource).toContain("setCachedComposeDraftDirectory(mailbox");
  });

  it("shows an APS network retry message instead of a Gmail draft-sync error", () => {
    expect(homeViewSource).toContain("const [draftSyncError, setDraftSyncError] = useState(false);");
    expect(homeViewSource).toContain("setDraftSyncError(true);");
    expect(homeViewSource).toContain("draftSyncError &&");
    expect(homeViewSource).toContain('mailboxView === "drafts"');
    expect(homeViewSource).toContain('t("mail.draftsApsSyncFailed")');
    expect(homeViewSource).toContain('t("mail.draftsApsSyncHint")');
  });

  it("keeps APS compose-draft loading independent from inbox-thread draft loading", () => {
    const refreshStart = homeViewSource.indexOf("const refreshStoredDrafts = useCallback");
    const refreshEnd = homeViewSource.indexOf("const syncInbox = useCallback", refreshStart);
    const refreshSource = homeViewSource.slice(refreshStart, refreshEnd);
    expect(refreshSource).toContain("void composeDraftActionsRef.current");
    expect(refreshSource).toContain(".listInboxThreadDrafts(mailbox, 100)");
    expect(refreshSource).toContain("const payload = await composeDraftActionsRef.current.listComposeDrafts(mailbox)");
    expect(refreshSource).not.toContain("Promise.allSettled([");
  });

  it("does not render the bottom refresh action beside the empty-state retry", () => {
    expect(homeViewSource).toContain("const hasGroupedMessages = grouped.some");
    expect(homeViewSource).toContain(
      'mailboxView === "drafts" && !state.inboxLoading && (!draftSyncError || hasGroupedMessages)',
    );
  });

  it("shows progress for the empty-state retry and coalesces duplicate draft refreshes", () => {
    expect(homeViewSource).toContain("const [draftSyncing, setDraftSyncing] = useState(false)");
    expect(homeViewSource).toContain("composeDraftRefreshInFlightRef");
    expect(homeViewSource).toContain('draftSyncing ? t("mail.refreshingDrafts") : t("mail.tryAgain")');
  });

  it("does not repeatedly reload drafts when the controller action object rerenders", () => {
    expect(homeViewSource).toContain("const composeDraftActionsRef = useRef({");
    expect(homeViewSource).toContain("composeDraftActionsRef.current.listComposeDrafts(mailbox)");
    expect(homeViewSource).toContain("}, [mailbox]);");
  });

  it("loads only the selected draft body after the lightweight directory row is clicked", () => {
    expect(homeViewSource).toContain("getComposeDraft: actions.getComposeDraft");
    expect(homeViewSource).toContain("await composeDraftActionsRef.current.getComposeDraft(mailbox, draftId)");
    expect(homeViewSource).toContain("composeDraftBodyPreview(draft)");
  });

  it("mounts a Drafts directory detail in its closed state before opening the drawer", () => {
    const openHelperStart = homeViewSource.indexOf("const openComposeDraftDrawer = useCallback");
    const openHelperEnd = homeViewSource.indexOf("const openMessageDetail = useCallback", openHelperStart);
    const openHelper = homeViewSource.slice(openHelperStart, openHelperEnd);
    expect(openHelper).toContain("setComposeOpening(true);");
    expect(openHelper).toContain("window.requestAnimationFrame");
    expect(openHelper).toContain("setComposeOpen(true);");
    expect(openHelper.indexOf("setComposeOpening(true);")).toBeLessThan(
      openHelper.indexOf("setComposeOpen(true);"),
    );
    expect(homeViewSource).toContain("openComposeDraftDrawer(draft);");
    expect(homeViewSource).toContain("composeOpen || composeOpening || composeClosing");
  });

  it("clears stale search terms before rendering the Drafts folder", () => {
    const selectViewStart = homeViewSource.indexOf("const selectMailboxView = (next: MailboxView) => {");
    const selectViewEnd = homeViewSource.indexOf("const updateFlag", selectViewStart);
    const selectViewSource = homeViewSource.slice(selectViewStart, selectViewEnd);
    expect(selectViewSource).toContain('if (next === "drafts")');
    expect(selectViewSource).toContain('setSearch("");');
    expect(selectViewSource).toContain('setActiveSearch("");');
  });
});

describe("compose close persistence", () => {
  it("closes the composer before its remote draft save completes", () => {
    const composeSource = readFileSync(
      new URL("./ComposeView.tsx", import.meta.url),
      "utf8",
    );
    const closeStart = composeSource.indexOf("const close = () => {");
    const closeEnd = composeSource.indexOf("const saveToDrafts", closeStart);
    const closeSource = composeSource.slice(closeStart, closeEnd);
    expect(closeSource).toContain("onCloseWithSave(draftInput, etag || undefined);");
    expect(closeSource).not.toContain("await save()");
    expect(closeSource.indexOf("onCloseWithSave")).toBeLessThan(
      closeSource.indexOf("onClose();"),
    );
  });

  it("keeps a failed background save available through an explicit restore action", () => {
    expect(homeViewSource).toContain("const saveComposeDraftAfterClose = useCallback(");
    expect(homeViewSource).toContain('t("compose.backgroundSaveFailed")');
    expect(homeViewSource).toContain('t("compose.restoreDraft")');
    expect(homeViewSource).toContain("restoreBackgroundComposeDraft(recovery)");
  });

  it("does not queue an APS save when the compose draft has not changed", () => {
    const composeSource = readFileSync(
      new URL("./ComposeView.tsx", import.meta.url),
      "utf8",
    );
    expect(composeSource).toContain("composeDraftFingerprint");
    expect(composeSource).toContain(
      "composeDraftFingerprint(draftInput) !== persistedDraftFingerprintRef.current",
    );
  });
});

describe("compose draft deletion", () => {
  it("optimistically hides a draft and waits for the undo toast to expire before deleting remotely", () => {
    const deleteStart = homeViewSource.indexOf("const requestComposeDraftDelete = useCallback(");
    const deleteEnd = homeViewSource.indexOf("const scheduleComposeBatch", deleteStart);
    const deleteSource = homeViewSource.slice(deleteStart, deleteEnd);
    expect(deleteSource).toContain("pendingComposeDraftDeletesRef.current.add(draft.id);");
    expect(deleteSource).toContain("const nextDrafts = current.filter((item) => item.id !== draft.id);");
    expect(deleteSource).toContain("return nextDrafts;");
    expect(deleteSource).toContain('actionLabel: t("toast.undo")');
    expect(deleteSource).toContain("onAction: restore");
    expect(deleteSource).toContain("onExpire: async () => {");
    expect(deleteSource.indexOf("onExpire: async () => {")).toBeLessThan(
      deleteSource.indexOf("await actions.deleteComposeDraft(mailbox, draft.id);"),
    );
  });

  it("uses the same deferred deletion flow from the compose discard control", () => {
    const composeSource = readFileSync(
      new URL("./ComposeView.tsx", import.meta.url),
      "utf8",
    );
    expect(composeSource).toContain("onDiscardDraft: (draft: ComposeDraft) => void;");
    expect(composeSource).toContain("onDiscardDraft({");
    expect(composeSource).not.toContain("await actions.deleteComposeDraft(mailbox, draftId)");
    expect(homeViewSource).toContain("onDiscardDraft={requestComposeDraftDelete}");
  });
});

describe("AI draft card layout", () => {
  it("renders completed batch cards while the assistant is still generating", () => {
    expect(homeViewSource).toContain("const pendingDraftArtifacts = message.artifacts || [];");
    expect(homeViewSource).toContain("DraftReplyArtifactCard artifact={item} onUse={onUseArtifact}");
  });

  it("places draft cards before generation status and completed assistant text", () => {
    const pendingStart = homeViewSource.indexOf("if (message.pending) {");
    const pendingEnd = homeViewSource.indexOf("const result = message.result;", pendingStart);
    const pendingBlock = homeViewSource.slice(pendingStart, pendingEnd);
    expect(pendingBlock.indexOf("pendingComposeArtifacts.map")).toBeLessThan(pendingBlock.indexOf('t("ai.thinking")'));

    const draftStart = homeViewSource.indexOf("const hasDraftArtifacts = Boolean(");
    const draftEnd = homeViewSource.indexOf('<div className="ai-message-footer">', draftStart);
    const draftBlock = homeViewSource.slice(draftStart, draftEnd);
    expect(draftBlock.indexOf("BatchComposeDraftArtifacts")).toBeLessThan(draftBlock.lastIndexOf("<AnimatedAssistantText"));
  });

  it("does not offer retry after a response already produced draft artifacts", () => {
    expect(homeViewSource).toContain('message.kind === "error" && !hasDraftArtifacts');
  });

  it("renders every batch compose card with a scrollable message body", () => {
    expect(homeViewSource).toContain("message.composeArtifacts || []");
    expect(homeViewSource).toContain("className=\"ai-draft-artifact-body\"");
  });

  it("localizes batch drafting prompts and draft artifact labels", () => {
    expect(homeViewSource).toContain('t("mail.bulk.batchAiDraftPrompt")');
    expect(homeViewSource).not.toContain("Draft short replies for each selected email");
    expect(homeViewSource).toContain('t("ai.goToEmail")');
    expect(homeViewSource).toContain('t("ai.draftFallbackTitle", { number: index + 1 })');
    expect(homeViewSource).toContain('t("ai.replyDraft")');
    expect(homeViewSource).toContain('t("ai.draftTo")');
    expect(homeViewSource).toContain('t("ai.draftSubject")');
    expect(homeViewSource).toContain('t("ai.draftMessage")');
  });

  it("keeps batch draft saves idempotent while streaming adds artifacts", () => {
    expect(homeViewSource).toContain("const [savedIds, setSavedIds] = useState<Set<string>>(new Set())");
    expect(homeViewSource).toContain("actions.saveComposeDraftBatch");
    expect(homeViewSource).not.toContain("const saved = await Promise.all(valid.map");
  });

  it("auto-expands the message editor and omits the second assistant follow-up", () => {
    expect(homeViewSource).toContain("textarea.style.height = \"auto\";");
    expect(homeViewSource).toContain("textarea.style.height = `${textarea.scrollHeight}px`;");
    expect(homeViewSource).toContain("rows={1}");
    expect(homeViewSource).not.toContain("message.assistantFollowupText");
  });

  it("scrolls the AI conversation to the actual bottom while card layout settles", () => {
    expect(homeViewSource).toContain("scroller.scrollHeight - scroller.clientHeight");
    expect(homeViewSource).toContain("new ResizeObserver");
    expect(homeViewSource).toContain('querySelector<HTMLElement>(".ai-message-stack")');
    expect(homeViewSource).toContain("observer.observe(content)");
    expect(homeViewSource).toContain("draftArtifactSignature");
    expect(homeViewSource).toContain("pinnedToBottomRef.current = true;");
    expect(homeViewSource).toContain("forceDraftArtifactScroll");
  });

  it("invalidates a pending detail close before async draft persistence finishes", () => {
    const drawerSource = readFileSync(
      new URL("../mail-detail/MailDetailDrawer.tsx", import.meta.url),
      "utf8",
    );
    expect(drawerSource).toContain("onRequestClose");
    expect(drawerSource).toContain("onRequestClose();");
  });
});

describe("formatInboxTabCount", () => {
  it("caps tab badges at 99+", () => {
    expect(formatInboxTabCount(0)).toBe("0");
    expect(formatInboxTabCount(99)).toBe("99");
    expect(formatInboxTabCount(100)).toBe("99+");
    expect(formatInboxTabCount(150)).toBe("99+");
  });
});

describe("list pagination", () => {
  it("uses fixed page size and auto-loads more on feed scroll bottom", () => {
    expect(homeViewSource).toContain("const INBOX_FEED_PAGE_SIZE = 100");
    expect(homeViewSource).toContain("feedWindow.localLimit + INBOX_FEED_PAGE_SIZE");
    expect(homeViewSource).toContain("tryAutoLoadMoreEmails");
    expect(homeViewSource).toContain("onScroll={() => tryAutoLoadMoreEmails()}");
    expect(homeViewSource).toContain("formatInboxTabCount(count)");
    expect(homeViewSource).not.toContain("initial_list_size");
    expect(homeViewSource).not.toContain("moreInPeriodButtonLabel");
  });
});

describe("isMailFeedNearBottom", () => {
  it("detects when the mail feed is near the bottom edge", () => {
    expect(isMailFeedNearBottom({ scrollHeight: 1000, scrollTop: 930, clientHeight: 600 }, 80)).toBe(true);
    expect(isMailFeedNearBottom({ scrollHeight: 1000, scrollTop: 200, clientHeight: 600 }, 80)).toBe(false);
  });
});

describe("nextFeedRangeDays", () => {
  it("advances along the fixed range ladder", () => {
    expect(nextFeedRangeDays(7)).toBe(30);
    expect(nextFeedRangeDays(30)).toBe(60);
    expect(nextFeedRangeDays(60)).toBe(0);
    expect(nextFeedRangeDays(0)).toBeNull();
  });

  it("jumps to the next larger step for irregular windows", () => {
    expect(nextFeedRangeDays(14)).toBe(30);
    expect(nextFeedRangeDays(45)).toBe(60);
  });
});

describe("inboxLastSyncedLabel", () => {
  it("formats last synced time down to seconds", () => {
    const label = inboxLastSyncedLabel("2026-07-17T08:09:10.000Z");
    expect(label.startsWith("Last synced:")).toBe(true);
    // 允许本地时区；至少包含秒位分隔
    expect(label).toMatch(/\d{1,2}:\d{2}:\d{2}/);
  });

  it("returns empty for invalid timestamps", () => {
    expect(inboxLastSyncedLabel("")).toBe("");
    expect(inboxLastSyncedLabel("not-a-date")).toBe("");
  });
});

describe("PlusIcon", () => {
  it("renders an accessible-hidden SVG plus glyph", () => {
    const icon = PlusIcon();
    const svg = icon.type(icon.props);

    expect(svg.type).toBe("svg");
    expect(svg.props["aria-hidden"]).toBe("true");
    expect(svg.props.children.type).toBe("path");
    expect(svg.props.children.props.d).toBe("M12 5v14M5 12h14");
  });
});

describe("gmailAuthorizationError", () => {
  it("includes the source returned by the authorization check", () => {
    expect(gmailAuthorizationError("none")).toBe(
      "No authorized mailbox detected (source: none).",
    );
    expect(gmailAuthorizationError("runtime")).toBe(
      "No authorized mailbox detected (source: runtime).",
    );
  });
});

describe("isGmailAuthorizationRequired", () => {
  it("distinguishes missing authorization from an auth-check error", () => {
    expect(isGmailAuthorizationRequired({ checked: true, authorized: false, source: "none" })).toBe(true);
    expect(isGmailAuthorizationRequired({ checked: true, authorized: false, source: "error" })).toBe(false);
    expect(isGmailAuthorizationRequired({ checked: true, authorized: true, source: "runtime" })).toBe(false);
  });
});

describe("isUnreadMessage", () => {
  it("uses Gmail's UNREAD label when the DTO boolean is absent", () => {
    expect(isUnreadMessage({ id: "label-only", label_ids: ["INBOX", "UNREAD"] })).toBe(true);
    expect(isUnreadMessage({ id: "read", label_ids: ["INBOX"] })).toBe(false);
  });
});

describe("gmailTrashUrl", () => {
  it("targets Trash for the selected Gmail account", () => {
    expect(gmailTrashUrl(" user+work@example.com ")).toBe(
      "https://mail.google.com/mail/?authuser=user%2Bwork%40example.com#trash",
    );
  });
});

describe("isAiConversationNearBottom", () => {
  it("allows a small layout tolerance at the bottom", () => {
    expect(isAiConversationNearBottom({ scrollHeight: 1000, scrollTop: 376, clientHeight: 600 })).toBe(true);
  });

  it("detects when the user has scrolled away from new messages", () => {
    expect(isAiConversationNearBottom({ scrollHeight: 1000, scrollTop: 300, clientHeight: 600 })).toBe(false);
  });
});

describe("aiSearchStatus", () => {
  it("uses Chinese status copy for Chinese scan results", () => {
    expect(aiSearchStatus({ title: "收件箱整理", sections: [{ items: [{ subject: "Update" }] }] }))
      .toBe("找到 1 封相关邮件。");
  });

  it("keeps English status copy for English scan results", () => {
    expect(aiSearchStatus({ title: "Inbox summary", sections: [{ items: [{ subject: "Update" }] }] }))
      .toBe("Found 1 relevant email.");
  });

  it("uses the English request language when a result title is malformed", () => {
    expect(aiSearchStatus(
      { title: "查找紧急邮件", sections: [{ items: [{ subject: "Update" }] }] },
      "Find urgent emails",
    )).toBe("Found 1 relevant email.");
  });
});

describe("aiThinkingElapsedLabel", () => {
  it("formats the elapsed Thinking duration from the pending-message start time", () => {
    expect(aiThinkingElapsedLabel("2026-07-16T12:00:00.000Z", Date.parse("2026-07-16T12:01:05.000Z"))).toBe("1m 5s");
  });

  it("ignores an invalid Thinking start time", () => {
    expect(aiThinkingElapsedLabel("invalid", Date.now())).toBe("");
  });
});

describe("senderParts", () => {
  it("accepts null sender values from Gmail Trash", () => {
    expect(senderParts(null)).toEqual({ name: "Unknown sender", email: "" });
  });

  it("parses display names and addresses", () => {
    expect(senderParts("Jane Doe <jane@example.com>")).toEqual({ name: "Jane Doe", email: "jane@example.com" });
  });
});

describe("accountDisplayName", () => {
  it("prefers Google profile names for account labels", () => {
    expect(accountDisplayName({ email: "kateq@anna.partners", display_name: "KateQ Zhou" })).toBe("KateQ Zhou");
  });

  it("falls back to the mailbox local-part when no profile name is available", () => {
    expect(accountDisplayName({ email: "kateq@anna.partners" })).toBe("kateq");
  });
});

describe("messageParticipant", () => {
  it("shows me and recipients for sent messages in All mail", () => {
    const participant = messageParticipant({
      id: "sent-1", from: "Owner <owner@example.com>", to: '"Doe, Jane" <jane@example.com>, Team <team@example.com>', label_ids: ["SENT"],
    }, "all", "owner@example.com");
    expect(participant.name).toBe("me, Doe, Jane, Team");
    expect(participant.email).toBe("jane@example.com");
  });

  it("uses a stable fallback for messages without a From header", () => {
    expect(messageParticipant({ id: "broken", from: null, to: "owner@example.com" }, "all", "owner@example.com").name).toBe("No sender");
  });

  it("labels recipient-less drafts without showing Unknown sender", () => {
    expect(messageParticipant({ id: "draft", from: null, to: null, draft_local: true }, "all", "owner@example.com").name).toBe("me");
  });

  it("shows drafts as authored by me instead of listing recipients", () => {
    const participant = messageParticipant({
      id: "draft-with-recipient", from: "Owner <owner@example.com>", to: "Helena <helena@example.com>", draft_local: true,
    }, "drafts", "owner@example.com");
    expect(participant.name).toBe("me, Helena");
    expect(participant.title).toBe("Helena <helena@example.com>");
  });

  it("uses the same me-plus-recipient format outside All mail for sent items", () => {
    const participant = messageParticipant({
      id: "sent-folder-1", from: "Owner <owner@example.com>", to: "Mail <mail@example.com>", label_ids: ["SENT"],
    }, "inbox", "owner@example.com");
    expect(participant.name).toBe("me, Mail");
    expect(participant.email).toBe("mail@example.com");
  });
});

describe("cached message label fallbacks", () => {
  it("recognizes important and starred labels without DTO booleans", () => {
    const cachedMessage = { id: "cached", label_ids: ["INBOX", "IMPORTANT", "STARRED"] };
    expect(isImportantMessage(cachedMessage)).toBe(true);
    expect(isStarredMessage(cachedMessage)).toBe(true);
    expect(isDraftMessage({ id: "gmail-draft", label_ids: ["draft"] })).toBe(false);
    expect(isDraftMessage({ id: "local-draft", draft_local: true })).toBe(true);
  });

  it("uses the original sender as the reply recipient for inbound local drafts", () => {
    const participant = messageParticipant({
      id: "draft-inbound", from: "Sahra <sahra@example.com>", to: "owner@example.com", draft_local: true,
    }, "drafts", "owner@example.com");
    expect(participant.name).toBe("me, Sahra");
    expect(participant.email).toBe("sahra@example.com");
  });

  it("keeps the important icon visible on outgoing draft rows", () => {
    expect(shouldShowImportantIcon(true, true, true)).toBe(true);
    expect(shouldShowImportantIcon(true, true, false)).toBe(false);
  });
});

describe("mergeInboxSearchSourceMessages", () => {
  it("includes unread todos that are excluded from the plain inbox projection", () => {
    const inboxMail = {
      id: "inbox-1",
      label_ids: ["INBOX", "UNREAD"],
      unread: true,
      internal_date: "200",
    };
    const todoOnly = {
      id: "todo-1",
      label_ids: ["UNREAD"],
      unread: true,
      internal_date: "300",
    };
    const flags = {
      todos: [todoOnly.id],
      snoozed: [],
      done: [],
      doneRemoved: [],
      drafts: [],
      saved: { [todoOnly.id]: todoOnly },
    };

    expect(resolveSourceMessages("inbox", [inboxMail], [inboxMail, todoOnly], flags, workflowOf(flags)).map((m) => m.id)).toEqual([
      "inbox-1",
    ]);
    expect(
      mergeInboxSearchSourceMessages([inboxMail], [inboxMail, todoOnly], flags, workflowOf(flags)).map((m) => m.id),
    ).toEqual(["todo-1", "inbox-1"]);
  });

  it("prefers live Gmail unread state over a stale saved todo copy", () => {
    const saved = {
      id: "todo-1",
      label_ids: ["INBOX", "UNREAD"],
      unread: true,
      internal_date: "100",
    };
    const live = {
      ...saved,
      label_ids: ["INBOX"],
      unread: false,
    };
    const flags = {
      todos: [saved.id],
      snoozed: [],
      done: [],
      doneRemoved: [],
      drafts: [],
      saved: { [saved.id]: saved },
    };
    const merged = mergeInboxSearchSourceMessages([live], [live], flags, workflowOf(flags));
    expect(merged).toHaveLength(1);
    expect(isUnreadMessage(merged[0])).toBe(false);
  });
});

describe("resolveSourceMessages", () => {
  it("projects trashed mail only into Trash while preserving its Gmail labels", () => {
    const trashed = {
      id: "trashed-1",
      label_ids: ["TRASH", "STARRED", "IMPORTANT", "SENT"],
      internal_date: "300",
    };
    const flags = {
      todos: [trashed.id],
      snoozed: [trashed.id],
      done: [trashed.id],
      doneRemoved: [],
      drafts: [],
      saved: { [trashed.id]: trashed },
    };

    expect(isTrashMessage(trashed)).toBe(true);
    expect(resolveSourceMessages("trash", [], [trashed], flags, workflowOf(flags))).toEqual([trashed]);
    for (const view of ["inbox", "todos", "starred", "snoozed", "done", "drafts", "sent", "all"] as const) {
      expect(resolveSourceMessages(view, [], [trashed], flags, workflowOf(flags))).toEqual([]);
    }
    expect(trashed.label_ids).toEqual(["TRASH", "STARRED", "IMPORTANT", "SENT"]);
  });

  it("excludes Gmail and local drafts from Trash", () => {
    const gmailDraft = { id: "gmail-draft", label_ids: ["TRASH", "DRAFT"], internal_date: "300" };
    const localDraft = { id: "local-draft", label_ids: ["TRASH"], internal_date: "200", draft_local: true };
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: {} };

    expect(resolveSourceMessages("trash", [], [gmailDraft, localDraft], flags, workflowOf(flags))).toEqual([]);
  });

  it("uses current Gmail state instead of a stale saved workflow copy", () => {
    const saved = { id: "message-1", label_ids: ["INBOX"], internal_date: "100" };
    const current = { ...saved, label_ids: ["TRASH", "STARRED"] };
    const flags = { todos: [saved.id], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: { [saved.id]: saved } };

    expect(resolveSourceMessages("todos", [], [current], flags, workflowOf(flags))).toEqual([]);
  });

  it("keeps a drafted starred message in both category projections", () => {
    const base = [{ id: "message-1", thread_id: "thread-1", label_ids: ["INBOX", "STARRED"], subject: "Hello" }];
    const drafts = [{ id: "message-1", thread_id: "thread-1", label_ids: ["INBOX", "STARRED", "DRAFT"], draft_local: true, draft_body: "Reply" }];
    const merged = mergeDraftOverlayMessages(base, drafts);
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: {} };

    expect(resolveSourceMessages("starred", [], merged, flags).map((message) => message.id)).toEqual(["message-1"]);
    expect(resolveSourceMessages("drafts", [], merged, flags).map((message) => message.id)).toEqual(["message-1"]);
  });

  it("overlays an APS Compose draft onto its source inbox thread", () => {
    const inboxMessage = {
      id: "received-1",
      thread_id: "thread-1",
      mailbox: "owner@example.com",
      label_ids: ["INBOX"],
    };
    const composeDrafts = [{
      id: "compose-1",
      mailbox: "owner@example.com",
      source_thread_id: "thread-1",
      recipients: ["julian@example.com"],
      subject: "Re: Meeting",
      body: "Thanks for the update.",
      updated_at: "2026-08-06T07:48:43+00:00",
    }];

    const [merged] = mergeDraftOverlayMessages(
      [inboxMessage],
      composeDraftOverlayMessages(composeDrafts),
    );

    expect(merged.id).toBe("received-1");
    expect(merged.draft_local).toBe(true);
    expect(merged.draft_body).toBe("Thanks for the update.");
    expect(merged.label_ids).toContain("DRAFT");
  });

  it("preserves the inbox message direction when applying a draft from the same thread", () => {
    const inboxMessage = {
      id: "received-1",
      thread_id: "thread-1",
      mailbox: "owner@example.com",
      from: "World of AI <team@worldofai.example>",
      to: "Owner <owner@example.com>",
      label_ids: ["INBOX"],
      internal_date: "100",
    };
    const draft = {
      id: "sent-1",
      thread_id: "thread-1",
      mailbox: "owner@example.com",
      from: "Owner <owner@example.com>",
      to: "World of AI <team@worldofai.example>",
      draft_body: "Thanks for the update.",
      draft_local: true,
      label_ids: ["DRAFT", "SENT"],
      internal_date: "200",
    };

    const [merged] = mergeDraftOverlayMessages([inboxMessage], [draft]);

    expect(merged.from).toBe(inboxMessage.from);
    expect(merged.to).toBe(inboxMessage.to);
    expect(merged.draft_body).toBe(draft.draft_body);
    expect(isSentMessage(merged)).toBe(false);
  });

  it("prefers live inbox messages over stale snapshot data in inbox view", () => {
    const liveInbox = [
      { id: "live-1", label_ids: ["INBOX", "IMPORTANT"], internal_date: "200" },
      { id: "live-2", label_ids: ["INBOX"], internal_date: "100" },
    ];
    const staleSnapshot = [
      { id: "stale-1", label_ids: ["INBOX", "IMPORTANT"], internal_date: "300" },
    ];
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: {} };

    expect(resolveSourceMessages("inbox", liveInbox, staleSnapshot, flags).map((message) => message.id)).toEqual(["live-1", "live-2"]);
  });

  it("keeps all-mail results in strict reverse chronological order", () => {
    const snapshot = [
      { id: "older", label_ids: ["INBOX"], internal_date: "100" },
      { id: "newest", label_ids: ["INBOX"], internal_date: "300" },
      { id: "middle", label_ids: ["INBOX"], internal_date: "200" },
    ];
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: {} };

    expect(resolveSourceMessages("all", [], snapshot, flags).map((message) => message.id)).toEqual(["newest", "middle", "older"]);
  });

  it("keeps only the latest message for a thread projection", () => {
    const inbox = [
      { id: "thread-1-old", thread_id: "thread-1", label_ids: ["INBOX"], internal_date: "1719360000000", subject: "Re: Feature Invite" },
      { id: "thread-1-new", thread_id: "thread-1", label_ids: ["INBOX"], internal_date: "1720051200000", subject: "Re: Feature Invite" },
      { id: "thread-2", thread_id: "thread-2", label_ids: ["INBOX"], internal_date: "1719446400000", subject: "Other" },
    ];
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: {} };

    expect(resolveSourceMessages("inbox", inbox, [], flags).map((message) => message.id)).toEqual(["thread-1-new", "thread-2"]);
  });

  it("collapses starred older messages with the latest thread message", async () => {
    const { uniqueLatestInboxThreads } = await import("./HomeView");
    const mixed = [
      {
        id: "old-starred",
        thread_id: "thread-1",
        label_ids: ["INBOX", "STARRED"],
        internal_date: "1719360000000",
        subject: "Feature Invite",
        snippet: "Hi Kate",
      },
      {
        id: "new-reply",
        thread_id: "thread-1",
        label_ids: ["INBOX"],
        internal_date: "1720051200000",
        subject: "Feature Invite",
        snippet: "No worries",
      },
      {
        id: "other",
        thread_id: "thread-2",
        label_ids: ["INBOX"],
        internal_date: "1719446400000",
        subject: "Other",
      },
    ];
    expect(uniqueLatestInboxThreads(mixed).map((message) => message.id)).toEqual([
      "new-reply",
      "other",
    ]);
  });

  it("propagates attachment flags from older thread messages to the latest row", async () => {
    const { uniqueLatestInboxThreads } = await import("./HomeView");
    const mixed = [
      {
        id: "old-with-file",
        thread_id: "thread-1",
        label_ids: ["INBOX"],
        internal_date: "1719360000000",
        subject: "Invoice",
        has_attachment: true,
        attachment_count: 2,
      },
      {
        id: "new-reply",
        thread_id: "thread-1",
        label_ids: ["INBOX"],
        internal_date: "1720051200000",
        subject: "Invoice",
        has_attachment: false,
        attachment_count: 0,
      },
      {
        id: "other",
        thread_id: "thread-2",
        label_ids: ["INBOX"],
        internal_date: "1719446400000",
        subject: "Other",
      },
    ];
    const projected = uniqueLatestInboxThreads(mixed);
    expect(projected.map((message) => message.id)).toEqual(["new-reply", "other"]);
    expect(projected[0].has_attachment).toBe(true);
    expect(projected[0].attachment_count).toBe(2);
    expect(projected[1].has_attachment).toBeFalsy();
  });

  it("treats sent mail as done by default", () => {
    const snapshot = [
      { id: "sent-1", label_ids: ["SENT"], internal_date: "300" },
      { id: "done-1", label_ids: ["INBOX"], internal_date: "200" },
    ];
    const flags = { todos: [], snoozed: [], done: ["done-1"], doneRemoved: [], drafts: [], saved: {} };

    expect(resolveSourceMessages("done", [], snapshot, flags, workflowOf(flags)).map((message) => message.id)).toEqual(["sent-1", "done-1"]);
    expect(isSentMessage(snapshot[0])).toBe(true);
    expect(isDoneMessage(snapshot[0], workflowOf(flags))).toBe(true);
  });

  it("treats mailbox-authored mail as sent even without an explicit SENT label", () => {
    const sentMessage = {
      id: "sent-implicit",
      mailbox: "owner@example.com",
      from: "Owner <owner@example.com>",
      label_ids: ["INBOX"],
      internal_date: "300",
    };
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: [], drafts: [], saved: {} };

    expect(isSentMessage(sentMessage)).toBe(true);
    expect(isDoneMessage(sentMessage, workflowOf(flags))).toBe(true);
  });

  it("lets sent mail be moved back out of done", () => {
    const sentMessage = { id: "sent-1", label_ids: ["SENT"], internal_date: "300" };
    const flags = { todos: [], snoozed: [], done: [], doneRemoved: ["sent-1"], drafts: [], saved: {} };

    expect(resolveSourceMessages("done", [], [sentMessage], flags, workflowOf(flags))).toEqual([]);
    expect(resolveSourceMessages("all", [], [sentMessage], flags, workflowOf(flags))).toEqual([sentMessage]);
    expect(isDoneMessage(sentMessage, workflowOf(flags))).toBe(false);
  });
});

describe("hasMailboxScanError", () => {
  it("treats explicit scan errors as failed state", () => {
    expect(hasMailboxScanError(undefined, undefined, "scan crashed")).toBe(true);
  });

  it("treats mailbox status failure markers as failed state", () => {
    expect(hasMailboxScanError("failed")).toBe(true);
    expect(hasMailboxScanError("error")).toBe(true);
    expect(hasMailboxScanError("done")).toBe(false);
  });
});
