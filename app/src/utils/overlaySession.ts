// Window visibility is session state, not a persistent display preference.
// Keep layout/size/content settings untouched, including on upgrades/crashes.
export const OVERLAY_VISIBLE_KEY = "dragon-nest-v420-buff-overlay-enabled";
let ending = false;
type VisibilityStorage = Pick<Storage, "setItem">;

export function beginOverlaySession(storage: VisibilityStorage, auxiliaryWindow: boolean) {
  if (auxiliaryWindow) return; // Other WebViews share storage with the main one.
  ending = false;
  storage.setItem(OVERLAY_VISIBLE_KEY, "0");
}

export function endOverlaySession(storage: VisibilityStorage) {
  ending = true;
  storage.setItem(OVERLAY_VISIBLE_KEY, "0");
}

export function overlaySessionEnding() { return ending; }
