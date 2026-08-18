import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BACKEND = process.env.JARVIS_BACKEND || "http://127.0.0.1:8787";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true },
      "/ws": { target: BACKEND, ws: true, changeOrigin: true },
    },
  },
});
