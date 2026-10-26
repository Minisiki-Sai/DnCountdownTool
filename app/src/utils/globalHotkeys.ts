export const DEFAULT_CAPTURE_HOTKEY = "Alt+F12";
export const DEFAULT_OVERLAY_HOTKEY = "Alt+F11";
export const DEFAULT_DISCONNECT_GAME_HOTKEY = "F4";
export const DEFAULT_STONE_WASHING_HOTKEY = "F2";

export const HOTKEY_STORAGE_KEYS = {
  capture: "dragon-nest-v420-capture-hotkey",
  overlay: "dragon-nest-overlay-hotkey",
  disconnectGame: "dragon-nest-v421-disconnect-game-hotkey",
  disconnectGameEnabled: "dragon-nest-v465-disconnect-game-enabled",
  stoneWashing: "dragon-nest-v425-stone-washing-hotkey",
} as const;

export function readGlobalHotkeyConfiguration() {
  return {
    startCaptureHotkey: localStorage.getItem(HOTKEY_STORAGE_KEYS.capture) || DEFAULT_CAPTURE_HOTKEY,
    toggleOverlayHotkey: localStorage.getItem(HOTKEY_STORAGE_KEYS.overlay) || DEFAULT_OVERLAY_HOTKEY,
    disconnectGameHotkey: localStorage.getItem(HOTKEY_STORAGE_KEYS.disconnectGame) || DEFAULT_DISCONNECT_GAME_HOTKEY,
    // This is intentionally opt-in. Existing installations that predate the
    // switch must not keep F4 registered as a destructive global shortcut.
    disconnectGameEnabled: localStorage.getItem(HOTKEY_STORAGE_KEYS.disconnectGameEnabled) === "1",
    stoneWashingHotkey: localStorage.getItem(HOTKEY_STORAGE_KEYS.stoneWashing) || DEFAULT_STONE_WASHING_HOTKEY,
  };
}
