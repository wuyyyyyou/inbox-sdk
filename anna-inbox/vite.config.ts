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
