import { afterEach, describe, expect, it } from "vitest";
import {
  AI_SIDEBAR_MODE_STORAGE_KEY,
  normalizeAiSidebarMode,
  resolveAiSidebarMode,
} from "./aiSidebarMode";

function installLocalStorage(values: Record<string, string> = {}) {
  const store = new Map(Object.entries(values));
  const localStorage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => {
      store.set(key, value);
    },
    removeItem: (key: string) => {
      store.delete(key);
    },
    clear: () => store.clear(),
  };
  Object.defineProperty(globalThis, "localStorage", { value: localStorage, configurable: true });
  Object.defineProperty(globalThis, "window", { value: { localStorage }, configurable: true });
}

afterEach(() => {
  Reflect.deleteProperty(globalThis, "localStorage");
  Reflect.deleteProperty(globalThis, "window");
});

describe("aiSidebarMode", () => {
  it("normalizes host and local only", () => {
    expect(normalizeAiSidebarMode("host")).toBe("host");
    expect(normalizeAiSidebarMode("LOCAL")).toBe("local");
    expect(normalizeAiSidebarMode("session")).toBeNull();
    expect(normalizeAiSidebarMode("")).toBeNull();
  });

  it("prefers localStorage over backend default", () => {
    installLocalStorage({ [AI_SIDEBAR_MODE_STORAGE_KEY]: "local" });
    expect(resolveAiSidebarMode("host")).toBe("local");
    window.localStorage.setItem(AI_SIDEBAR_MODE_STORAGE_KEY, "host");
    expect(resolveAiSidebarMode("local")).toBe("host");
  });

  it("defaults to local regardless of the backend-reported mode", () => {
    installLocalStorage();
    expect(resolveAiSidebarMode("local")).toBe("local");
    expect(resolveAiSidebarMode("host")).toBe("local");
    expect(resolveAiSidebarMode("weird")).toBe("local");
    expect(resolveAiSidebarMode(undefined)).toBe("local");
  });

  it("keeps host available only through the hidden storage override", () => {
    installLocalStorage({ [AI_SIDEBAR_MODE_STORAGE_KEY]: "host" });
    expect(resolveAiSidebarMode("local")).toBe("host");
    expect(resolveAiSidebarMode("host")).toBe("host");
  });
});
