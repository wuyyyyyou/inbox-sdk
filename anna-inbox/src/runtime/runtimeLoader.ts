import type { RuntimeState } from "../types/mail";
import { AnnaAppRuntimeCompat } from "./runtimeCompat";

declare global {
  interface Window {
    AnnaAppRuntime?: { connect: () => Promise<RuntimeState["client"]> };
  }
}

async function loadRuntimeFactory() {
  if (typeof window.AnnaAppRuntime !== "undefined") {
    return window.AnnaAppRuntime;
  }

  for (const sdkPath of ["/static/anna-apps/_sdk/latest/index.js", "/static/anna-apps/_sdk/0.5.0/index.js"]) {
    try {
      const mod = await import(/* @vite-ignore */ sdkPath);
      const runtime = mod.AnnaAppRuntime || window.AnnaAppRuntime;
      if (runtime) return runtime;
    } catch {
    }
  }

  return AnnaAppRuntimeCompat;
}

export async function connectRuntime(): Promise<RuntimeState> {
  try {
    const runtimeFactory = await loadRuntimeFactory();
    const client = await runtimeFactory.connect();
    try {
      await client?.window?.set_title?.({ title: "Anna Inbox" });
    } catch {
    }
    return { connected: true, mode: "live", client };
  } catch (error) {
    return { connected: false, mode: "mock", error: error instanceof Error ? error.message : String(error) };
  }
}
