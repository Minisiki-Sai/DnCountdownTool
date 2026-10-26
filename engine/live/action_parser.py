from __future__ import annotations

import json
import os
from pathlib import Path


_VALID_MODES = frozenset({"legacy", "structured"})
# Increment whenever the decoded action event stream changes. Diagnostic
# caches include this value so parser improvements cannot be hidden by an old
# capture snapshot.
ACTION_PARSER_SCHEMA_VERSION = 6


def _settings_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    return root / "DragonNestBuildSimulator" / "live_action_parser.json"


def action_parser_mode() -> str:
    override = os.environ.get("DRAGON_NEST_ACTION_PARSER_MODE", "").strip().casefold()
    if override in _VALID_MODES:
        return override
    try:
        payload = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return "structured"
    mode = str(payload.get("mode", "")).strip().casefold()
    return mode if mode in _VALID_MODES else "structured"


def set_action_parser_mode(mode: str) -> str:
    normalized = str(mode or "").strip().casefold()
    if normalized not in _VALID_MODES:
        raise ValueError(f"Unsupported action parser mode: {mode}")
    destination = _settings_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"mode": normalized}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(destination)
    return normalized
