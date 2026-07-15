import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  root: "src",
  base: "./",
  plugins: [react()],
  build: {
    outDir: "../bundle",
    emptyOutDir: true,
    sourcemap: false,
    // 单入口 app.js 体量超过默认 500kB 属预期，不拆包以免影响 Anna App 加载路径
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: (assetInfo) => {
          if (assetInfo.names?.some((name) => name.endsWith(".css"))) return "style.css";
          return "assets/[name][extname]";
        },
      },
    },
  },
  test: {
    environment: "node",
    include: ["**/*.test.ts"],
  },
});
