"""把 ``.dpscap`` 离线重放成「语音证据行」。

这是回放功能的唯一实现：CLI（``tools/capture_replay.py``）、单元测试
（``tests/test_capture_replay.py``）和开发者模式里的「回放实战录像」
都走这里，避免出现第二套解码逻辑。

**解码全部交给正式回放源**（``V576DiagnosticReplaySource``）——就是 DPS 面板
回放录像用的那个类：抓包分段、元数据 adapter（health / profile / prebattle /
identity）、完整性校验全按正式实现走。这里只做三件事：开语音馈线、等它跑完、
把馈线真正放行的行取出来。规则在 ``frontend/src/utils`` 里，与实战共用同一份代码。

行上的 ``observedAt`` 用**事件时间**，等价于「零延迟投递」；实战的投递抖动由前端
回放的 ``deliveryDelayMs`` 参数模拟，不靠这里伪造墙钟。
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .capture_sources import V576DiagnosticReplaySource
from .profile import ClientProfile


@dataclass
class ReplayResult:
    capture: Path
    nest_key: str
    nest_name: str
    frames: int
    duration_ms: int
    rows: list[dict] = field(default_factory=list)
    drops: Counter = field(default_factory=Counter)
    drop_samples: list[dict] = field(default_factory=list)
    kind_counts: Counter = field(default_factory=Counter)
    sources: Counter = field(default_factory=Counter)
    templates: Counter = field(default_factory=Counter)
    boss_actor_count: int = 0
    # 整场出现过的 Boss：模板号 → 名字。回放时悬浮窗的 Boss 行要用它，
    # 不然只能显示「未识别」——证据行里只有模板号，没有名字。
    bosses: list[dict] = field(default_factory=list)
    # 正式源内部统计，便于对照「到底多做了哪些活」。
    pipeline_stats: dict = field(default_factory=dict)

    @property
    def skill_rows(self) -> list[dict]:
        return [
            row
            for row in self.rows
            if int(row.get("skill") or 0) > 0
            and (row.get("kind") or row.get("evidenceKind"))
            not in {"boss_hp_state", "boss_hp_threshold"}
        ]

    def to_json(self) -> dict:
        return {
            "capture": self.capture.name,
            "nestKey": self.nest_key,
            "nestName": self.nest_name,
            "frames": self.frames,
            "durationMs": self.duration_ms,
            "bossActorCount": self.boss_actor_count,
            "bosses": self.bosses,
            "drops": dict(self.drops),
            "dropSamples": self.drop_samples,
            "kindCounts": dict(self.kind_counts),
            "pipelineStats": self.pipeline_stats,
            "rows": self.rows,
        }


def _boss_roster(decoder: object | None) -> list[dict]:
    """整场绑定过的 Boss 名单（模板号去重，按出场先后）。

    证据行里只有 ``templateId``，没有名字。回放时悬浮窗的 Boss 行要显示名字，
    只能在这里一次性取出来，前端按游标处的模板号去查。
    """

    if decoder is None:
        return []
    entities = getattr(decoder, "monster_entities", None) or {}
    actors = list(getattr(decoder, "boss_actor_ids", None) or ())
    birth = getattr(decoder, "_monster_birth_phase", None) or {}
    if not entities or not actors:
        return []

    ordered = sorted(actors, key=lambda actor: float(birth.get(int(actor), 0) or 0))
    out: list[dict] = []
    seen: set[int] = set()
    for actor in ordered:
        try:
            actor_id = int(actor)
        except (TypeError, ValueError):
            continue
        entity = entities.get(actor_id)
        if entity is None:
            continue
        try:
            template_id = int(getattr(entity, "template_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if template_id <= 0 or template_id in seen:
            continue
        try:
            name = str(decoder._monster_display_context(actor_id)[0] or "")
        except Exception:
            name = ""
        seen.add(template_id)
        out.append(
            {
                "templateId": template_id,
                "actorId": f"0x{actor_id:08X}",
                "targetId": f"0x{actor_id:08X}@{template_id}",
                "name": name,
                "phase": int(birth.get(actor_id, 0) or 0),
            }
        )
    return out


def replay(
    path: Path,
    data_root: Path,
    progress: Callable[[float, str], None] | None = None,
) -> ReplayResult:
    """解码一个抓包，返回正式馈线放行的证据行（相对毫秒 + 秒）。"""

    path = Path(path).resolve()
    report = progress or (lambda _ratio, _text: None)
    report(0.02, "正在准备...")

    profile = ClientProfile.load(Path(data_root) / "profiles" / "client_profile.json")
    source = V576DiagnosticReplaySource(
        path,
        Path(data_root),
        profile.known_players,
        progress_callback=lambda done, total: report(
            0.05 + 0.9 * (done / max(1, total)), f"正在解码 {done}/{total} 帧"
        ),
        voice_feed=True,
    )
    feed = source.voice_signal_feed
    if feed is None:  # 防御：正式源签名变了就直接报，不要静默退回自建馈线
        raise RuntimeError("正式回放源没有接上语音馈线，回放无法继续")

    rows: list[dict] = []
    publish = feed.publish

    def collect(row):
        # 只在正式馈线真的放行时记账：被 10 秒去重挡掉的行，
        # 实战前端根本收不到，回放也不该算。入库那条才有 sequence。
        stored = publish(row)
        if stored is not None:
            rows.append(dict(stored))
        return stored

    feed.publish = collect
    started = time.monotonic()
    try:
        source.poll()  # 全量解码；语音证据在解码过程中经 feed 发布
    finally:
        feed.publish = publish
        source.close()
    elapsed = time.monotonic() - started

    for row in rows:
        row["t"] = round(float(row.get("timestamp") or 0) / 1000.0, 3)
        # 回放的「发布时间」= 事件时间（零延迟投递）；抖动由前端参数模拟。
        row["observedAt"] = row["t"]

    origin = source.capture_origin_timestamp_seconds
    end = source.capture_end_timestamp_seconds
    duration_ms = int(round((end - origin) * 1000)) if origin is not None and end is not None else 0
    decoder = getattr(source, "_decoder", None)

    result = ReplayResult(
        capture=path,
        nest_key=str(getattr(decoder, "detected_nest_key", "") or ""),
        nest_name=str(getattr(decoder, "detected_nest_name", "") or ""),
        frames=int(getattr(source, "_observed_physical_frames", 0) or 0),
        duration_ms=duration_ms,
        rows=rows,
        drops=Counter(getattr(decoder, "skill_drop_counts", {}) or {}),
        drop_samples=list(getattr(decoder, "skill_drop_samples", []) or []),
        boss_actor_count=len(getattr(decoder, "boss_actor_ids", ()) or ()),
        bosses=_boss_roster(decoder),
        pipeline_stats={
            **{str(k): v for k, v in (getattr(source, "replay_pipeline_stats", {}) or {}).items()},
            "decodeSeconds": round(elapsed, 2),
            "feedRows": len(rows),
        },
    )
    for row in rows:
        kind = row.get("kind") or row.get("evidenceKind") or "?"
        result.kind_counts[kind] += 1
        if row.get("source"):
            result.sources[str(row["source"])] += 1
        if row.get("templateId"):
            result.templates[int(row["templateId"])] += 1
    report(1.0, f"解析完成，{len(rows)} 行")
    return result


def collect_paths(targets: list[str]) -> list[Path]:
    found: list[Path] = []
    for target in targets:
        path = Path(target).expanduser()
        if path.is_dir():
            found.extend(sorted(path.rglob("*.dpscap")))
        elif path.is_file():
            found.append(path)
    return found


def describe(result: ReplayResult) -> str:
    lines = [
        f"{result.capture.name}",
        f"  时长 {result.duration_ms/1000:.0f}s  帧 {result.frames}  行 {len(result.rows)}"
        f"  技能行 {len(result.skill_rows)}  BossActor {result.boss_actor_count}",
        f"  副本识别 {result.nest_name or '(未识别)'} [{result.nest_key or '-'}]",
        f"  解码 {result.pipeline_stats.get('decodeSeconds', '-')}s",
    ]
    if result.drops:
        lines.append(f"  !! 丢弃 {sum(result.drops.values())} 条：{dict(result.drops)}")
        for sample in result.drop_samples[:5]:
            lines.append(f"     {sample}")
    else:
        lines.append("  丢弃 0 条（守恒）")
    top = result.kind_counts.most_common(8)
    lines.append(f"  行类型 {dict(top)}")
    return "\n".join(lines)


def discover_captures(*roots: Path, limit: int = 40) -> list[dict[str, object]]:
    """列出可回放的抓包，最新的在前面。"""

    found: list[Path] = []
    for root in roots:
        if not Path(root).is_dir():
            continue
        found.extend(Path(root).rglob("*.dpscap"))
    found.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    rows: list[dict[str, object]] = []
    for path in found[:limit]:
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append(
            {
                "path": str(path),
                "name": path.name,
                "sizeBytes": int(stat.st_size),
                "modifiedAt": int(stat.st_mtime),
            }
        )
    return rows
