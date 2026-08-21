import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev proxy mirrors production nginx, keeping browser traffic same-origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3001,
    proxy: {
      "/api": { target: "http://localhost:8015", changeOrigin: true, rewrite: (path) => path.replace(/^\/api/, "") },
      "/live": { target: "ws://localhost:8015", ws: true, changeOrigin: true },
    },
  },
});
