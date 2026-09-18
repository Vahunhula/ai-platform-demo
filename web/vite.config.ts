import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Development-only proxy: the browser calls same-origin /api and Vite forwards it
// to the FastAPI interface. Override with AI_PLATFORM_API_PROXY (shell or .env).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const apiTarget = env.AI_PLATFORM_API_PROXY || "http://127.0.0.1:8765";

  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        // Keep the browser's Host header: the API accepts cookie-authenticated
        // mutations only when Origin matches Host (same-origin), so the dev proxy
        // must not rewrite Host to the backend's address.
        "/api": { target: apiTarget, changeOrigin: false },
      },
    },
  };
});
