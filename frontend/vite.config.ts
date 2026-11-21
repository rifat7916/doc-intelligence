import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev server: /api is proxied to the backend and the API key (if any) is attached server-side,
// so it is never shipped to the browser.  In Docker the same job is done by nginx (see nginx.conf.template).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.API_URL ?? "http://localhost:8000",
        headers: process.env.API_KEY ? { "X-API-Key": process.env.API_KEY } : {},
      },
    },
  },
});
