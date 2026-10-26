from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .data_loader import MODULAR_DIR


USER_CONFIG_DIR = (
    Path(os.getenv("APPDATA") or Path.home()) / "DragonNestBuildSimulator"
)
SKILL_BUILD_DEFAULTS_PATH = USER_CONFIG_DIR / "skill_build_defaults.json"
BUILTIN_SKILL_BUILD_DEFAULTS_PATH = (
    MODULAR_DIR / "tables" / "skills" / "skill_build_defaults_builtin.json"
)


def read_skill_build_defaults_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_user_skill_build_defaults() -> dict[str, Any]:
    return read_skill_build_defaults_file(SKILL_BUILD_DEFAULTS_PATH)


def load_builtin_skill_build_defaults() -> dict[str, Any]:
    return read_skill_build_defaults_file(BUILTIN_SKILL_BUILD_DEFAULTS_PATH)


_SKILL_BUILD_DEFAULTS_CACHE_SIGNATURE: tuple[
    tuple[int, int], tuple[int, int]
] | None = None
_SKILL_BUILD_DEFAULTS_CACHE: dict[str, Any] = {}


def _skill_build_defaults_file_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0


def load_skill_build_defaults() -> dict[str, Any]:
    global _SKILL_BUILD_DEFAULTS_CACHE_SIGNATURE, _SKILL_BUILD_DEFAULTS_CACHE
    signature = (
        _skill_build_defaults_file_signature(BUILTIN_SKILL_BUILD_DEFAULTS_PATH),
        _skill_build_defaults_file_signature(SKILL_BUILD_DEFAULTS_PATH),
    )
    if signature == _SKILL_BUILD_DEFAULTS_CACHE_SIGNATURE:
        return _SKILL_BUILD_DEFAULTS_CACHE
    defaults = load_builtin_skill_build_defaults()
    defaults.update(load_user_skill_build_defaults())
    _SKILL_BUILD_DEFAULTS_CACHE_SIGNATURE = signature
    _SKILL_BUILD_DEFAULTS_CACHE = defaults
    return _SKILL_BUILD_DEFAULTS_CACHE
