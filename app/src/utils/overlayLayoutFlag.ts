/**
 * Layout-mode flag shared by the main window and every overlay window.
 *
 * Overlay windows are created on demand, so an event sent while the window is
 * still booting is lost - the window then shows only the live rows, keeps its
 * previous size and has no drag panel. WebView2 windows of the same app share
 * one origin and one local-storage profile, so a flag written before the window
 * is shown is readable by the fresh WebView on its first frame. That makes the
 * hand-off independent of boot timing; the event bus is still used to update
 * windows that are already open.
 */
export const OVERLAY_LAYOUT_MODE_KEY = "dragon-nest-v420-overlay-layout-mode";

export function readOverlayLayoutMode(): boolean {
  try {
    return localStorage.getItem(OVERLAY_LAYOUT_MODE_KEY) === "1";
  } catch {
    return false;
  }
}

export function writeOverlayLayoutMode(enabled: boolean): void {
  try {
    localStorage.setItem(OVERLAY_LAYOUT_MODE_KEY, enabled ? "1" : "0");
  } catch {
    /* Private mode or a locked profile: the event bus still covers open windows. */
  }
}

/** Subscribe to cross-window flag changes; returns an unsubscribe function. */
export function onOverlayLayoutModeChange(handler: (enabled: boolean) => void): () => void {
  const listener = (event: StorageEvent) => {
    if (event.key !== OVERLAY_LAYOUT_MODE_KEY) return;
    handler(event.newValue === "1");
  };
  window.addEventListener("storage", listener);
  return () => window.removeEventListener("storage", listener);
}
