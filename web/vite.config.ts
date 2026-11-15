import { defineConfig } from "vitest/config";
import { loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// VITE_API_MOCK=1 swaps the network for an in-browser mock of docs/API.md.
// The flag becomes a compile-time constant, so a build without it drops the mock chunk entirely.
export default defineConfig(({ mode }) => {
  // loadEnv reads .env files and the shell environment (the shell wins).
  const env = loadEnv(mode, ".", "VITE_");
  const mock = env.VITE_API_MOCK === "1";
  return {
    plugins: [react()],
    define: { __API_MOCK__: JSON.stringify(mock) },
    server: {
      port: 5173,
      proxy: mock ? undefined : { "/api": { target: "http://localhost:8000", changeOrigin: true } },
    },
    preview: { port: 4173 },
    build: { target: "es2022", sourcemap: false },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      css: false,
      include: ["src/**/*.test.{ts,tsx}"],
    },
  };
});
