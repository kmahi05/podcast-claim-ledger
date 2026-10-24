import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "VITE_");
  return {
    plugins: [react(), tailwindcss()],
    server: {
      port: 3002,
      strictPort: true,
      // same origin for the browser, so the refresh cookie (path /api/v1/auth) just works
      proxy: { "/api": env.VITE_API_PROXY_TARGET || "http://localhost:8002" },
    },
  };
});
