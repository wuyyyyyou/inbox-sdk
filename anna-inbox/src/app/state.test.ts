import { afterEach, describe, expect, it } from "vitest";
import { MAILBOX_STORAGE_KEY } from "./constants";
import { createInitialState, removeAskHistoryEntry } from "./state";

const AI_ASK_HISTORY_STORAGE_KEY = "anna-inbox:ai-ask-history:v1";

function installLocalStorage(values: Record<string, string>) {
  const store = new Map(Object.entries(values));
  const localStorage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => store.set(key, value),
    removeItem: (key: string) => store.delete(key),
    clear: () => store.clear(),
  };
  Object.defineProperty(globalThis, "localStorage", { value: localStorage, configurable: true });
  Object.defineProperty(globalThis, "window", { value: { localStorage }, configurable: true });
}

afterEach(() => {
  Reflect.deleteProperty(globalThis, "localStorage");
  Reflect.deleteProperty(globalThis, "window");
});

describe("createInitialState", () => {
  it("discards legacy local Ask history because it has no mailbox scope", () => {
    const recentTs = new Date().toISOString();
    const savedHistory = [{
      conversationId: "chat_previous",
      kind: "chat",
      query: "hello",
      timestamp: recentTs,
      result: { title: "hello", summary: "hi", sections: [] },
      pendingRun: { runId: "at_pending123", question: "hello" },
      messages: [
        { id: "msg_user", role: "user", content: "hello", timestamp: recentTs },
        { id: "msg_assistant", role: "assistant", content: "hi", timestamp: recentTs },
      ],
    }];
    installLocalStorage({
      [MAILBOX_STORAGE_KEY]: "owner@example.com",
      [AI_ASK_HISTORY_STORAGE_KEY]: JSON.stringify(savedHistory),
    });

    const state = createInitialState();

    expect(state.mailbox).toBe("owner@example.com");
    expect(state.askHistory).toEqual([]);
    expect(window.localStorage.getItem(AI_ASK_HISTORY_STORAGE_KEY)).toBeNull();
    expect(state.aiChatMessages).toEqual([]);
    expect(state.aiChatConversationId).toBe("");
  });

  it("does not load any legacy local history, including recent entries", () => {
    const oldTs = new Date(Date.now() - 10 * 24 * 60 * 60 * 1000).toISOString();
    const recentTs = new Date().toISOString();
    installLocalStorage({
      [MAILBOX_STORAGE_KEY]: "owner@example.com",
      [AI_ASK_HISTORY_STORAGE_KEY]: JSON.stringify([
        { conversationId: "old", query: "old", timestamp: oldTs, result: { title: "old", sections: [] } },
        { conversationId: "new", query: "new", timestamp: recentTs, result: { title: "new", sections: [] } },
      ]),
    });
    const state = createInitialState();
    expect(state.askHistory).toEqual([]);
  });
});

describe("removeAskHistoryEntry", () => {
  it("removes only the selected conversation", () => {
    const history = [
      { query: "first", timestamp: "2026-07-08T01:00:00.000Z", result: { title: "first", sections: [] } },
      { query: "second", timestamp: "2026-07-08T02:00:00.000Z", result: { title: "second", sections: [] } },
      { query: "third", timestamp: "2026-07-08T03:00:00.000Z", result: { title: "third", sections: [] } },
    ];

    expect(removeAskHistoryEntry(history, 1).map((entry) => entry.query)).toEqual(["first", "third"]);
  });

  it("keeps the existing history when the index is invalid", () => {
    const history = [
      { query: "first", timestamp: "2026-07-08T01:00:00.000Z", result: { title: "first", sections: [] } },
    ];

    expect(removeAskHistoryEntry(history, 4)).toBe(history);
  });
});
