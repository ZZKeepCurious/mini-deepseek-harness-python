import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// MiniHarness webui — independent frontend project.
// Development: proxies /api (RPC) and /api/remote.mux (WS mux) to the Python
// web transport backend. This is the ONLY coupling surface with the backend:
// the wire contract (two-envelope RPC + remote.mux + `$events`/`$events/result`).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: process.env.MINIHARNESS_WEBUI_PROXY ?? "http://127.0.0.1:8899",
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
