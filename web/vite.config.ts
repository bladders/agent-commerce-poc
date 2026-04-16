import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget =
    process.env.VITE_PROXY_TARGET || env.VITE_PROXY_TARGET || "http://127.0.0.1:8000";
  const agentTarget =
    process.env.VITE_AGENT_PROXY_TARGET || env.VITE_AGENT_PROXY_TARGET || "http://127.0.0.1:8080";

  return {
    plugins: [react()],
    server: {
      host: true,
      port: 5173,
      proxy: {
        "^/(api|health|webhooks|checkout_sessions)": {
          target: apiTarget,
          changeOrigin: true,
        },
        "^/agent": {
          target: agentTarget,
          changeOrigin: true,
          rewrite: (path: string) => path.replace(/^\/agent/, ""),
        },
      },
    },
  };
});
