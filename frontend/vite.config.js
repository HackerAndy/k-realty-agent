import { defineConfig } from "vite";

// Builds to ../interfaces/web/dist, which rest_server.py serves as the app's
// static bundle — the FastAPI backend and the built frontend ship from the
// same repo, same as before the modularization, just via a build step now.
// In dev, `npm run dev` proxies /api and /agent-web-only paths to the Python
// backend (poetry run agent-web, port 8765) so callTool()'s relative
// fetch('/api/tool/...') keeps working unchanged with hot reload.
export default defineConfig({
  root: __dirname,
  build: {
    outDir: "../interfaces/web/dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765",
    },
  },
});
