let AnnaAppRuntime = null;

// 生产环境：Anna 平台在 iframe 中已注入 <script> SDK，直接可用。
if (typeof window.AnnaAppRuntime !== "undefined") {
  AnnaAppRuntime = window.AnnaAppRuntime;
}

// 本地 dev / 降级：尝试 ES module 动态导入官方 SDK。
if (!AnnaAppRuntime) {
  for (const sdkPath of ["/static/anna-apps/_sdk/latest/index.js", "/static/anna-apps/_sdk/0.5.0/index.js"]) {
    try {
      const mod = await import(sdkPath);
      AnnaAppRuntime = mod.AnnaAppRuntime || window.AnnaAppRuntime;
      if (AnnaAppRuntime) break;
    } catch {
    }
  }
}

// 最终降级：bundle 内 compat（postMessage 协议，本地 anna-app dev 依赖）。
if (!AnnaAppRuntime) {
  try {
    const compat = await import("./anna-app-runtime-compat.js");
    AnnaAppRuntime = compat.AnnaAppRuntime;
  } catch {
  }
}

export { AnnaAppRuntime };
