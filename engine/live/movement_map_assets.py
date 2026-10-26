from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .dungeon_map_catalog import resolve_dungeon_map_id


@dataclass(frozen=True, slots=True)
class MovementMapAsset:
    map_id: int
    file_name: str
    grid_directory: str
    image_width: int
    image_height: int
    world_width: float
    world_height: float
    cell_size: float
    coordinate_width: float
    coordinate_height: float
    coordinate_min_x: float
    coordinate_min_y: float
    swap_xy: bool
    invert_x: bool
    invert_y: bool

    @property
    def path(self) -> Path:
        return movement_map_asset_directory() / self.file_name


def movement_map_asset_directory() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "movement_maps"
    )


def _coordinate_transform_for_grid(
    grid_directory: str,
    *,
    world_width: float,
    world_height: float,
) -> tuple[float, float, float, float, bool, bool, bool]:
    """Return the projection shared by client grid DDS minimaps.

    Grid world coordinates are centred on zero. The DDS horizontal axis is
    world X and its downward image axis is the inverse of world Z. Older ACT
    builds treated the signed packet coordinates as unsigned and compensated
    with per-room rotations; those transforms distorted every negative axis.
    """

    del grid_directory
    return (
        world_width,
        world_height,
        -world_width / 2.0,
        -world_height / 2.0,
        False,
        False,
        True,
    )


@lru_cache(maxsize=1)
def load_movement_map_asset_manifest() -> dict[int, MovementMapAsset]:
    manifest_path = movement_map_asset_directory() / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = payload.get("maps", {}) if isinstance(payload, dict) else {}
    if not isinstance(rows, dict):
        return {}
    result: dict[int, MovementMapAsset] = {}
    for raw_map_id, row in rows.items():
        if not isinstance(row, dict):
            continue
        try:
            map_id = int(raw_map_id)
            file_name = Path(str(row.get("file") or "")).name
            image_width = max(1, int(row.get("image_width") or 256))
            image_height = max(1, int(row.get("image_height") or 256))
            world_width = max(0.0, float(row.get("world_width") or 0.0))
            world_height = max(0.0, float(row.get("world_height") or 0.0))
            cell_size = max(0.0, float(row.get("cell_size") or 0.0))
        except (TypeError, ValueError):
            continue
        if map_id <= 0 or not file_name or world_width <= 0 or world_height <= 0:
            continue
        grid_directory = str(row.get("grid_directory") or "").strip()
        (
            coordinate_width,
            coordinate_height,
            coordinate_min_x,
            coordinate_min_y,
            swap_xy,
            invert_x,
            invert_y,
        ) = _coordinate_transform_for_grid(
            grid_directory,
            world_width=world_width,
            world_height=world_height,
        )
        result[map_id] = MovementMapAsset(
            map_id=map_id,
            file_name=file_name,
            grid_directory=grid_directory,
            image_width=image_width,
            image_height=image_height,
            world_width=world_width,
            world_height=world_height,
            cell_size=cell_size,
            coordinate_width=coordinate_width,
            coordinate_height=coordinate_height,
            coordinate_min_x=coordinate_min_x,
            coordinate_min_y=coordinate_min_y,
            swap_xy=swap_xy,
            invert_x=invert_x,
            invert_y=invert_y,
        )
    return result


@lru_cache(maxsize=1)
def _movement_map_assets_by_grid() -> dict[str, MovementMapAsset]:
    return {
        row.grid_directory.casefold(): row
        for row in load_movement_map_asset_manifest().values()
        if row.grid_directory
    }


def resolve_movement_map_asset(map_id: object) -> MovementMapAsset | None:
    try:
        parsed = int(map_id)
    except (TypeError, ValueError):
        return None
    manifest = load_movement_map_asset_manifest()
    asset = manifest.get(parsed)
    if asset is None:
        # Some map IDs reuse an identical client grid directory. Resolve that
        # relationship through the unpacked catalog instead of guessing from
        # a dungeon name or choosing an adjacent room ID.
        entry = resolve_dungeon_map_id(parsed)
        for grid_directory in entry.grid_directories if entry else ():
            source = _movement_map_assets_by_grid().get(
                grid_directory.casefold()
            )
            if source is None:
                continue
            (
                coordinate_width,
                coordinate_height,
                coordinate_min_x,
                coordinate_min_y,
                swap_xy,
                invert_x,
                invert_y,
            ) = _coordinate_transform_for_grid(
                grid_directory,
                world_width=source.world_width,
                world_height=source.world_height,
            )
            asset = MovementMapAsset(
                map_id=parsed,
                file_name=source.file_name,
                grid_directory=grid_directory,
                image_width=source.image_width,
                image_height=source.image_height,
                world_width=source.world_width,
                world_height=source.world_height,
                cell_size=source.cell_size,
                coordinate_width=coordinate_width,
                coordinate_height=coordinate_height,
                coordinate_min_x=coordinate_min_x,
                coordinate_min_y=coordinate_min_y,
                swap_xy=swap_xy,
                invert_x=invert_x,
                invert_y=invert_y,
            )
            break
    if asset is None or not asset.path.is_file():
        return None
    return asset


def movement_map_asset_path(map_id: object) -> Path | None:
    asset = resolve_movement_map_asset(map_id)
    return asset.path if asset is not None else None
