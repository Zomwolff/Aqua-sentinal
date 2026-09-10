import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev-mode proxy mirrors the production nginx.conf so relative /api, /live
// and /artifacts URLs work identically in both environments.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/fusion": {
        target: "http://localhost:8017",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/fusion/, ""),
      },
      "/api": {
        target: "http://localhost:8015",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      "/artifacts": {
        target: "http://localhost:8015",
        changeOrigin: true,
      },
      "/live": {
        target: "ws://localhost:8015",
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
