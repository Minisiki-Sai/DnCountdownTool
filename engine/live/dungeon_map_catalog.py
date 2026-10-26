from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DungeonMapEntry:
    map_id: int
    official_name: str
    display_name: str
    map_type: int
    category: str
    content_kind: str
    map_subtype: int
    include_build_flag: bool
    enter_condition_table_id: int
    allow_map_type: int
    map_sid: int
    gate_map_ids: tuple[int, ...]
    tool_names: tuple[str, ...]
    grid_directories: tuple[str, ...]
    grid_resource_found: bool
    # Optional, corpus/game-route verified destination after this terminal
    # battle map.  Zero means that the destination must still be proven by
    # an exact map packet or the generic multi-signal scene chain.
    return_map_id: int


def _bundled_catalog_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "data"
        / "profiles"
        / "dungeon_map_catalog.json"
    )


def _terminal_return_overrides_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "data"
        / "profiles"
        / "terminal_return_maps.json"
    )


def _load_terminal_return_overrides() -> dict[int, int]:
    """Load capture-verified terminal destinations separately from MapTable.

    MapTable exposes ``_ReturnVillage`` and gate links, but neither field
    identifies the actual town chosen by the server after a completed run.
    Keeping audited destinations in a small data file avoids embedding one
    nest's route in the decoder and prevents a catalog rebuild from silently
    deleting the evidence.
    """

    try:
        payload = json.loads(
            _terminal_return_overrides_path().read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    raw_maps = payload.get("maps", {}) if isinstance(payload, dict) else {}
    if not isinstance(raw_maps, dict):
        return {}
    result: dict[int, int] = {}
    for raw_source, raw_destination in raw_maps.items():
        try:
            source = int(raw_source)
            destination = int(raw_destination)
        except (TypeError, ValueError):
            continue
        if source > 0 and destination > 0:
            result[source] = destination
    return result


@lru_cache(maxsize=1)
def load_dungeon_map_catalog() -> dict[int, DungeonMapEntry]:
    try:
        payload = json.loads(_bundled_catalog_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw_maps = payload.get("maps", ()) if isinstance(payload, dict) else ()
    terminal_returns = _load_terminal_return_overrides()
    result: dict[int, DungeonMapEntry] = {}
    for row in raw_maps:
        if not isinstance(row, dict):
            continue
        try:
            map_id = int(row.get("map_id") or 0)
        except (TypeError, ValueError):
            continue
        if map_id <= 0:
            continue
        result[map_id] = DungeonMapEntry(
            map_id=map_id,
            official_name=str(row.get("official_name") or "").strip(),
            display_name=str(row.get("display_name") or "").strip(),
            # Schema v1 contained battle maps only, so a missing map_type is
            # safely interpreted as the old MapType=3 behavior.
            map_type=int(row.get("map_type") or 3),
            category=str(row.get("category") or "").strip(),
            content_kind=str(row.get("content_kind") or "").strip(),
            map_subtype=int(row.get("map_subtype") or 0),
            include_build_flag=bool(row.get("include_build_flag")),
            enter_condition_table_id=int(
                row.get("enter_condition_table_id") or 0
            ),
            allow_map_type=int(row.get("allow_map_type") or 0),
            map_sid=int(row.get("map_sid") or 0),
            gate_map_ids=tuple(
                sorted(
                    {
                        int(value)
                        for value in row.get("gate_map_ids", ())
                        if int(value) > 0
                    }
                )
            ),
            tool_names=tuple(
                str(value).strip()
                for value in row.get("tool_names", ())
                if str(value).strip()
            ),
            grid_directories=tuple(
                str(value).strip()
                for value in row.get("grid_directories", ())
                if str(value).strip()
            ),
            grid_resource_found=bool(row.get("grid_resource_found")),
            return_map_id=int(
                terminal_returns.get(
                    map_id,
                    int(row.get("return_map_id") or 0),
                )
            ),
        )
    return result


def resolve_dungeon_map_id(map_id: object) -> DungeonMapEntry | None:
    try:
        parsed = int(map_id)
    except (TypeError, ValueError):
        return None
    return load_dungeon_map_catalog().get(parsed)


def is_battle_map(entry: DungeonMapEntry | None) -> bool:
    """Return whether an entry is a combat/dungeon map rather than a hub."""

    return entry is not None and entry.map_type == 3


def has_configured_battle_continuation(map_id: object) -> bool:
    """Return whether a known later room belongs to the same dungeon run."""

    current = resolve_dungeon_map_id(map_id)
    if current is None or not is_battle_map(current):
        return False
    root_id = int(current.enter_condition_table_id or 0)
    if root_id <= 0:
        return False
    current_name = str(current.display_name or current.official_name).strip()
    return any(
        is_battle_map(candidate)
        and int(candidate.enter_condition_table_id or 0) == root_id
        and int(candidate.map_id) > int(current.map_id)
        and str(candidate.display_name or candidate.official_name).strip()
        == current_name
        for candidate in load_dungeon_map_catalog().values()
    )


@lru_cache(maxsize=1)
def _map_entries_by_name() -> dict[str, tuple[DungeonMapEntry, ...]]:
    rows: dict[str, list[DungeonMapEntry]] = {}
    for entry in load_dungeon_map_catalog().values():
        for value in (entry.display_name, entry.official_name):
            key = str(value or "").strip().casefold()
            if key and entry not in rows.setdefault(key, []):
                rows[key].append(entry)
    return {key: tuple(values) for key, values in rows.items()}


def is_battle_map_name(value: object) -> bool:
    """Reject known town/hub names while preserving unknown legacy hints."""

    key = str(value or "").strip().casefold()
    if not key or key in {"场景返回", "庭院/天堂（场景返回）"}:
        return False
    matches = _map_entries_by_name().get(key, ())
    return not matches or any(is_battle_map(entry) for entry in matches)
