"""进本之后才开统计时，用「本机正在打的 Boss」反推副本。

待机时不抓包，所以进图那一刻的场景包已经错过了。此时能用的最可靠信号是
**本机角色正在攻击的实体**：它的 ``targetId`` 带模板号（``0x59@4150058``），
而 ``boss_hp_profiles.json`` 里每个副本都有明确的 Boss 模板集合。

数据上是干净的：当前 5 个副本、58 个 Boss 模板，**没有任何模板同时属于两个
副本**，所以模板号能唯一确定副本。同名不同副本（比如两只「红龙」）用的是
不同模板（经典 503696 / 硬核 4181001），不会串。

限制：只有打到已收录的 Boss 才能反推。打小怪、或副本本身不在 profile 里时
返回 ``None``，界面保持「未识别」，绝不瞎猜。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_PROFILE_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "profiles"
    / "boss_hp_profiles.json"
)

INFERRED_CONFIDENCE = "inferred"


@lru_cache(maxsize=1)
def template_nest_index() -> dict[int, dict[str, Any]]:
    """模板号 -> 副本描述。文件改动后需要重启进程才会重读（与其它 profile 一致）。"""

    try:
        payload = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, list):
        return {}

    index: dict[int, dict[str, Any]] = {}
    ambiguous: set[int] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        key = str(profile.get("key") or "").strip()
        if not key:
            continue
        label = str(profile.get("label") or key).strip()
        entry = {"nestKey": key, "nestLabel": label, "nestName": label}
        for raw in profile.get("template_ids") or ():
            try:
                template_id = int(raw)
            except (TypeError, ValueError):
                continue
            if template_id <= 0:
                continue
            if template_id in index and index[template_id]["nestKey"] != key:
                # 真的出现跨副本模板时不猜，交给场景识别。
                ambiguous.add(template_id)
            index[template_id] = entry
    for template_id in ambiguous:
        index.pop(template_id, None)
    return index


def _normalize_name(value: Any) -> str:
    return str(value or "").replace(" ", "").replace("　", "").strip()


@lru_cache(maxsize=1)
def boss_name_index() -> dict[str, dict[str, Any]]:
    """Boss 名字 -> 副本。模板号拿不到时的兜底（``targetId`` 没有 ``@模板``）。

    同名跨副本的条目（真的重名）会被剔除，不猜。
    """

    try:
        payload = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, list):
        return {}

    index: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        key = str(profile.get("key") or "").strip()
        if not key:
            continue
        label = str(profile.get("label") or key).strip()
        entry = {"nestKey": key, "nestLabel": label, "nestName": label}
        for boss in profile.get("bosses") or ():
            if not isinstance(boss, dict):
                continue
            names = [boss.get("name"), *(boss.get("aliases") or ())]
            for raw in names:
                name = _normalize_name(raw)
                if len(name) < 2:
                    continue
                if name in index and index[name]["nestKey"] != key:
                    ambiguous.add(name)
                index[name] = entry
    for name in ambiguous:
        index.pop(name, None)
    return index


def nest_boss_templates(nest_key: str) -> frozenset[int]:
    """某个副本登记在册的 Boss 模板集合（空 key 返回空集）。"""

    key = str(nest_key or "").strip()
    if not key:
        return frozenset()
    return frozenset(
        template_id
        for template_id, entry in template_nest_index().items()
        if entry.get("nestKey") == key
    )


def known_nest_keys() -> frozenset[str]:
    """受支持、且有 Boss 模板表的副本 key 集合。"""

    return frozenset(entry["nestKey"] for entry in template_nest_index().values())


@lru_cache(maxsize=1)
def _scene_templates_by_nest() -> dict[str, frozenset[int]]:
    """副本 -> 场景 Boss 模板。

    「场景 Boss」指同时在场、**任何玩家都选不中**、但一直在放技能的 Boss
    （梦魇海龙 P2 的空中海龙 4237034）。它不能靠攻击目标反推，只能由副本
    配置静态声明；它也不参与「当前目标」竞争，而是与目标 Boss 并列播报。
    """

    try:
        payload = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, list):
        return {}
    index: dict[str, frozenset[int]] = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        key = str(profile.get("key") or "").strip()
        if not key:
            continue
        templates: set[int] = set()
        for raw in profile.get("scene_template_ids") or ():
            try:
                template_id = int(raw)
            except (TypeError, ValueError):
                continue
            if template_id > 0:
                templates.add(template_id)
        if templates:
            index[key] = frozenset(templates)
    return index


def scene_boss_templates(nest_key: str) -> frozenset[int]:
    """某个副本「场景 Boss」的模板集合（空 key 返回空集）。"""

    key = str(nest_key or "").strip()
    if not key:
        return frozenset()
    return _scene_templates_by_nest().get(key, frozenset())


def template_id_of(target_id: Any) -> int:
    """从 ``0x...@4150058`` 取模板号；取不到返回 0。"""

    text = str(target_id or "").strip()
    if not text:
        return 0
    tail = text.rsplit("@", 1)[-1]
    try:
        template_id = int(tail)
    except (TypeError, ValueError):
        return 0
    return template_id if template_id > 0 else 0


def infer_nest_from_target(
    target_id: Any,
    target_name: Any = "",
) -> dict[str, Any] | None:
    template_id = template_id_of(target_id)
    if template_id:
        entry = template_nest_index().get(template_id)
        if entry:
            return {
                **entry,
                "nestTemplateId": template_id,
                "nestConfidence": INFERRED_CONFIDENCE,
            }
    # 目标 id 没有 @模板（实体模板未识别）时按名字兜底。
    name = _normalize_name(target_name)
    if len(name) >= 2:
        entry = boss_name_index().get(name)
        if entry:
            return {
                **entry,
                "nestTemplateId": template_id,
                "nestConfidence": INFERRED_CONFIDENCE,
            }
    return None


def infer_nest_from_attack_targets(
    attack_targets: Any,
    self_source_id: Any = "",
    target_names: Any = None,
) -> dict[str, Any] | None:
    """优先用本机目标；本机认不出来时用队伍里票数最多的目标。"""

    if not isinstance(attack_targets, dict) or not attack_targets:
        return None
    names = target_names if isinstance(target_names, dict) else {}

    def target_of(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("targetId") or value.get("target_id") or "").strip()
        return str(value or "").strip()

    def infer(target_id: str) -> dict[str, Any] | None:
        return infer_nest_from_target(target_id, names.get(target_id, ""))

    mine = target_of(attack_targets.get(str(self_source_id or "")))
    inferred = infer(mine)
    if inferred is not None:
        return inferred

    votes: dict[str, int] = {}
    for value in attack_targets.values():
        target_id = target_of(value)
        if target_id:
            votes[target_id] = votes.get(target_id, 0) + 1
    for target_id in sorted(votes, key=lambda key: (-votes[key], key)):
        inferred = infer(target_id)
        if inferred is not None:
            return inferred
    return None


def infer_nest_from_snapshot(snapshot: Any) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    # 优先用带时效的「当前目标」：副本推断不该被一只早已离场的 Boss 带偏。
    attacks = snapshot.get("recentAttackTargets")
    if not isinstance(attacks, dict):
        attacks = snapshot.get("lastAttackTargets")
    return infer_nest_from_attack_targets(
        attacks,
        snapshot.get("selfSourceId"),
        snapshot.get("targetNames"),
    )
