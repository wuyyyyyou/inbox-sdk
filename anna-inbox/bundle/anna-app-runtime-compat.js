const RPC_TIMEOUT_MS = 30000;
const HEARTBEAT_MS = 10000;
const wid = new URLSearchParams(window.location.search).get("wid") || "";
const pending = new Map();
const eventHandlers = new Map();
let requestSeq = 0;

class AnnaAppRpcError extends Error {
  constructor(error) {
    super(error?.message || "Anna App RPC failed");
    this.name = "AnnaAppRpcError";
    this.code = error?.code || "rpc_error";
    this.details = error?.details;
  }
}

function emit(eventName, payload) {
  for (const handler of eventHandlers.get(eventName) || []) {
    try {
      handler(payload);
    } catch {
    }
  }
}

window.addEventListener("message", (event) => {
  const message = event.data;
  if (!message || typeof message !== "object") return;
  if (message.wid && wid && message.wid !== wid) return;

  if (message.kind === "res" && pending.has(message.id)) {
    const slot = pending.get(message.id);
    pending.delete(message.id);
    window.clearTimeout(slot.timer);
    if (message.error) slot.reject(new AnnaAppRpcError(message.error));
    else slot.resolve(message.result);
    return;
  }

  if (message.kind === "event") {
    emit(message.event, message.payload);
  }
});

function call(ns, method, args = {}, options = {}) {
  const id = `anna-app-${Date.now()}-${++requestSeq}`;
  const timeout = Number(options.timeout || options.timeoutMs || RPC_TIMEOUT_MS);
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      pending.delete(id);
      reject(new AnnaAppRpcError({ code: "timeout", message: `${ns}.${method} timed out` }));
    }, timeout);
    pending.set(id, { resolve, reject, timer });
    window.parent.postMessage({ kind: "req", id, wid, ns, method, args }, "*");
  });
}

function namespaceProxy(ns) {
  return new Proxy({}, {
    get(_target, method) {
      return (args = {}) => call(ns, String(method), args);
    },
  });
}

function subscribe(eventName, handler) {
  const handlers = eventHandlers.get(eventName) || new Set();
  handlers.add(handler);
  eventHandlers.set(eventName, handlers);
  return () => handlers.delete(handler);
}

export const AnnaAppRuntime = {
  async connect() {
    if (!wid) {
      throw new AnnaAppRpcError({ code: "invalid_arg", message: "Missing Anna App window id." });
    }

    const hello = await call("window", "hello", {
      client_info: { sdk: "bundle-compat", version: "0.1.0" },
    });
    const runtime = {
      windowUuid: hello.window_uuid,
      appId: hello.app_id,
      versionId: hello.version_id,
      viewMeta: hello.view_meta,
      capabilities: hello.capabilities,
      entryPayload: hello.entry_payload,
      runtimeState: hello.runtime_state,
      geometry: hello.geometry,
      tools: namespaceProxy("tools"),
      chat: namespaceProxy("chat"),
      storage: namespaceProxy("storage"),
      artifact: namespaceProxy("artifact"),
      llm: namespaceProxy("llm"),
      fs: namespaceProxy("fs"),
      prefs: namespaceProxy("prefs"),
      window: namespaceProxy("window"),
      call,
      on: subscribe,
    };

    await call("window", "ready", {});
    window.setInterval(() => {
      call("window", "heartbeat", {}).catch(() => {});
    }, HEARTBEAT_MS);
    return runtime;
  },
};
