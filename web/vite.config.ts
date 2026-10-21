import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The built app is served by Flask from web/dist (same origin as the API, so the session
// cookie just works). In `vite dev`, everything server-side is proxied to a local Shorthop.
const target = process.env.SHORTHOP_URL || "http://localhost:8080";
const proxied = ["/api", "/login", "/logout", "/auth", "/docs", "/openapi.json", "/mcp"];

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist" },
  server: {
    port: 5173,
    proxy: Object.fromEntries(proxied.map((p) => [p, { target, changeOrigin: false }])),
  },
});
