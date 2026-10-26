import { createApp } from "vue";
import { invoke } from "@tauri-apps/api/core";
import App from "./App.vue";
import "./styles.css";
import "./styles/frost.css";

const tauriAvailable = "__TAURI_INTERNALS__" in window;
const desktopView = new URLSearchParams(window.location.search).get("view") || "main";
const isMainDesktopView = desktopView === "main";
const diagnosticBreadcrumbs: Array<Record<string, string | number>> = [];

function publishFrontendDiagnosticState() {
  if (!tauriAvailable || !isMainDesktopView) return;
  void invoke("update_frontend_diagnostic_state", {
    state: {
      route: window.location.hash || "#live",
      view: desktopView,
      visibility: document.visibilityState,
      viewport: { width: window.innerWidth, height: window.innerHeight },
      recentActions: diagnosticBreadcrumbs.slice(-30),
    },
  }).catch(() => undefined);
}

window.addEventListener("error", event => {
  if (!tauriAvailable) return;
  void invoke("write_frontend_diagnostic", {
    kind: "window-error",
    message: String(event.message || "Unknown frontend error").slice(0, 16_000),
    stack: (event.error instanceof Error ? event.error.stack || "" : "").slice(0, 32_000),
    location: [event.filename, event.lineno, event.colno].filter(Boolean).join(":"),
  }).catch(() => undefined);
});

if (tauriAvailable) {
  publishFrontendDiagnosticState();
  window.addEventListener("hashchange", publishFrontendDiagnosticState);
  window.addEventListener("resize", publishFrontendDiagnosticState);
}

createApp(App).mount("#app");


