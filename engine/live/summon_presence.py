from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .models import SkillCastEvent, SummonRemovalEvent


@dataclass(frozen=True, slots=True)
class SummonOverlaySpec:
    """One reviewed summon that is useful in the live overlay.

    This catalogue is deliberately smaller than the decoder's owner-attribution
    catalogue.  The latter also contains pets or helper objects that the user
    explicitly does not want to monitor (for example 奇迹之手 and 阿兹那之约).
    """

    key: str
    skill_name: str
    duration_seconds: float
    max_instances: int
    provider: str
    aliases: tuple[str, ...] = ()
    shared_group: str = ""
    shared_group_max: int = 0
    duration_by_job: tuple[tuple[str, float], ...] = ()

    def duration_for_job(self, job: str) -> float:
        duration_by_job = dict(self.duration_by_job)
        return float(duration_by_job.get(str(job or "").strip(), self.duration_seconds))


# Only the reviewed, user-approved summon list belongs here.  In particular,
# do not add 偷袭点、召唤猎鹰、奇迹之手、掌握之手、召唤摄魂怪、怨恨之像、
# 召唤傀儡、阿兹那之约、阿兹那治愈术 or 阿兹那之息.
SUMMON_OVERLAY_SPECS: tuple[SummonOverlaySpec, ...] = (
    SummonOverlaySpec("energy-reflection", "能量反射", 12.0, 1, "时空领主 / 黑暗女王"),
    SummonOverlaySpec("healing-relic", "治愈之手", 8.0, 99, "圣徒 / 雷神"),
    SummonOverlaySpec("lightning-relic", "雷电之手", 16.2, 3, "圣徒 / 雷神"),
    SummonOverlaySpec("cure-relic", "痊愈之手", 18.0, 99, "圣徒 / 雷神"),
    SummonOverlaySpec(
        "cannon-tower",
        "炮塔",
        30.0,
        2,
        "重炮手 / 机械大师",
        shared_group="engineer-basic-towers",
        shared_group_max=2,
        duration_by_job=(("机械大师", 60.0),),
    ),
    SummonOverlaySpec(
        "gatling-tower",
        "加特林机枪塔",
        30.0,
        2,
        "重炮手 / 机械大师",
        shared_group="engineer-basic-towers",
        shared_group_max=2,
        duration_by_job=(("机械大师", 60.0),),
    ),
    SummonOverlaySpec(
        "ice-pump-tower", "寒冰喷射塔", 30.0, 1, "重炮手 / 机械大师",
        duration_by_job=(("机械大师", 60.0),),
    ),
    SummonOverlaySpec("chainsaw-tower", "链锯之塔", 60.0, 1, "机械大师"),
    SummonOverlaySpec(
        "heavy-duck",
        "重磅鸭",
        45.0,
        3,
        "重炮手 / 机械大师",
        duration_by_job=(("重炮手", 45.0), ("机械大师", 60.0)),
    ),
    SummonOverlaySpec(
        "magic-grass",
        "召唤魔法草",
        60.0,
        2,
        "炼金圣士 / 药剂师",
        aliases=("魔法草",),
    ),
    SummonOverlaySpec("hide-and-seek", "瞒天过海", 13.0, 1, "炼金圣士 / 药剂师"),
    SummonOverlaySpec("spirit-gate", "灵魂之门", 11.0, 1, "噬魂者 / 黑暗萨满"),
    # User-confirmed value.  Keep this independent from any cooldown table.
    SummonOverlaySpec("shadow-summon", "暗影召唤术", 15.0, 1, "曜"),
)


def _summon_name_key(value: object) -> str:
    text = str(value or "").strip().upper().replace("ＥＸ", "EX")
    text = re.sub(r"[\s·_\-]+", "", text)
    # EX is a variant of the same summon and must not create another setting.
    return text.replace("EX", "")


_SPEC_BY_NAME: dict[str, SummonOverlaySpec] = {
    _summon_name_key(name): spec
    for spec in SUMMON_OVERLAY_SPECS
    for name in (spec.skill_name, *spec.aliases)
}


def summon_overlay_spec(skill_name: object) -> SummonOverlaySpec | None:
    return _SPEC_BY_NAME.get(_summon_name_key(skill_name))


def summon_overlay_options() -> list[dict[str, object]]:
    return [
        {
            "key": spec.key,
            "skill": spec.skill_name,
            "durationSeconds": spec.duration_seconds,
            "maxInstances": spec.max_instances,
            "provider": spec.provider,
        }
        for spec in SUMMON_OVERLAY_SPECS
    ]


@dataclass(slots=True)
class _SummonInstance:
    instance_id: str
    spec: SummonOverlaySpec
    source_id: str
    source_name: str
    source_job: str
    skill_id: str
    start_ms: int
    end_ms: int
    packet_kind: str


class SummonPresenceLedger:
    """Bounded, incremental active-summon state for the desktop overlay."""

    _DEDUPE_WINDOW_MS = 250

    def __init__(self) -> None:
        self._instances: list[_SummonInstance] = []
        self._sequence = 0
        self._last_cast_ms: dict[tuple[str, str], int] = {}

    def reset(self) -> None:
        self._instances.clear()
        self._last_cast_ms.clear()
        self._sequence = 0

    def _prune(self, current_ms: int) -> None:
        self._instances = [row for row in self._instances if row.end_ms > current_ms]

    def ingest_cast(self, event: SkillCastEvent) -> bool:
        spec = summon_overlay_spec(event.skill_name)
        source_id = str(event.source_id or "").strip()
        if spec is None or not source_id:
            return False
        timestamp_ms = max(0, int(event.timestamp_ms))
        self._prune(timestamp_ms)

        cast_key = (source_id, spec.key)
        previous_ms = self._last_cast_ms.get(cast_key)
        if previous_ms is not None and abs(timestamp_ms - previous_ms) <= self._DEDUPE_WINDOW_MS:
            return False
        self._last_cast_ms[cast_key] = timestamp_ms

        duration = max(0.1, spec.duration_for_job(event.job))
        candidates = [
            row
            for row in self._instances
            if row.source_id == source_id
            and (
                row.spec.key == spec.key
                or bool(spec.shared_group and row.spec.shared_group == spec.shared_group)
            )
        ]
        capacity = spec.shared_group_max if spec.shared_group else spec.max_instances
        capacity = max(1, int(capacity or 1))
        if len(candidates) >= capacity:
            remove_count = len(candidates) - capacity + 1
            oldest_ids = {
                row.instance_id
                for row in sorted(candidates, key=lambda item: (item.start_ms, item.instance_id))[:remove_count]
            }
            self._instances = [row for row in self._instances if row.instance_id not in oldest_ids]

        self._sequence += 1
        self._instances.append(
            _SummonInstance(
                instance_id=f"{source_id}|{spec.key}|{timestamp_ms}|{self._sequence}",
                spec=spec,
                source_id=source_id,
                source_name=str(event.source_name or "").strip(),
                source_job=str(event.job or "").strip(),
                skill_id=str(event.skill_id or "").strip(),
                start_ms=timestamp_ms,
                end_ms=timestamp_ms + round(duration * 1000.0),
                packet_kind=str(event.packet_kind or "skill_cast").strip(),
            )
        )
        return True

    def remove_owner(self, source_id: object) -> int:
        owner = str(source_id or "").strip()
        if not owner:
            return 0
        before = len(self._instances)
        self._instances = [row for row in self._instances if row.source_id != owner]
        return before - len(self._instances)

    def ingest_removal(self, event: SummonRemovalEvent) -> bool:
        """Close one factual summon instance after a lethal 08 6F state.

        Multiple objects of the same skill are created in cast order, so the
        oldest still-active matching instance is the safest runtime-ID-free
        pairing.  Boss Buff clears never call this method.
        """

        spec = summon_overlay_spec(event.skill_name)
        owner_id = str(event.owner_id or "").strip()
        if spec is None or not owner_id:
            return False
        timestamp_ms = max(0, int(event.timestamp_ms))
        self._prune(timestamp_ms)
        candidates = [
            row
            for row in self._instances
            if row.source_id == owner_id
            and row.spec.key == spec.key
            and row.start_ms <= timestamp_ms
        ]
        if not candidates:
            return False
        removed = min(
            candidates,
            key=lambda row: (row.start_ms, row.instance_id),
        )
        self._instances = [
            row
            for row in self._instances
            if row.instance_id != removed.instance_id
        ]
        return True

    def snapshot(
        self,
        current_ms: int,
        actor_context: Mapping[str, tuple[str, str]] | None = None,
    ) -> list[dict[str, object]]:
        now_ms = max(0, int(current_ms))
        self._prune(now_ms)
        context = actor_context or {}
        result: list[dict[str, object]] = []
        for row in sorted(
            self._instances,
            key=lambda item: (item.source_id, item.spec.skill_name, item.end_ms, item.instance_id),
        ):
            context_name, context_job = context.get(row.source_id, ("", ""))
            duration = max(0.1, (row.end_ms - row.start_ms) / 1000.0)
            result.append(
                {
                    "key": row.instance_id,
                    "catalog_key": row.spec.key,
                    "skill_id": row.skill_id,
                    "skill_name": row.spec.skill_name,
                    "source_id": row.source_id,
                    "source_name": context_name or row.source_name,
                    "source_job": context_job or row.source_job,
                    "start_seconds": row.start_ms / 1000.0,
                    "end_seconds": row.end_ms / 1000.0,
                    "duration_seconds": duration,
                    "remaining_seconds": max(0.0, (row.end_ms - now_ms) / 1000.0),
                    "detection_source": "skill_cast",
                    "packet_kind": row.packet_kind,
                }
            )
        return result

    def active_owner_ids(self, current_ms: int) -> set[str]:
        self._prune(max(0, int(current_ms)))
        return {row.source_id for row in self._instances if row.source_id}


def summon_catalog_keys(values: Iterable[object]) -> tuple[str, ...]:
    """Small helper used by tests and future settings migrations."""

    return tuple(
        spec.key
        for value in values
        if (spec := summon_overlay_spec(value)) is not None
    )
