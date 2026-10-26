"""技能身份归一化：实时链路与评分链路共用的最小公共层。

这一层刻意**不依赖** `scoring`，因此抓包、解析、悬浮窗快照这些实时路径
引用它时，不会把整套评分引擎（`TeamScoreEngine` 及其 mixin）拖进依赖图。

抽取原则：只搬纯函数与常量，逻辑与原来逐字一致。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from ..data_loader import MODULAR_DIR, norm

_JOB_DATA_ALIASES = {
    "打坐曜": "曜", "输出曜": "曜",
    "辅助药剂": "药剂师", "输出药剂": "药剂师",
}

_COMBAT_MODIFIER_RULES: dict[str, tuple[float, float, float | None]] = {
    # skill: (cooldown factor, action-speed factor, normal duration)
    "精神爆发": (0.60, 1.40, 14.0),
    "灼热火焰": (0.60, 1.40, 14.0),
    "时间加速": (0.30, 1.00, 15.0),
    "鸡尾酒": (0.88, 1.00, None),
    "精确瞄准": (1.00, 1.30, 18.0),
    "致残惩罚者": (1.00, 1.30, 10.0),
    "幽灵复仇者": (0.70, 1.30, 18.0),
    "幻影凝视": (1.00, 1.15, 10.0),
}


@lru_cache(maxsize=1)
def _silver_hunter_skill_keys() -> frozenset[str]:
    """Return normal Silver Hunter skill names used for combat-event aliases.

    The live game can report some Silver Hunter skills with a ``神圣`` prefix,
    while the simulator profiles and skill tables use the ordinary name.  Read
    the authoritative job skill table here instead of stripping that prefix
    globally: other classes have real, independent skills whose names begin
    with ``神圣``.
    """
    path = (
        MODULAR_DIR
        / "tables"
        / "skills"
        / "dngamer_skill_sim_reborn_split"
        / "jobs"
        / "job_81.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()

    skills = payload.get("skills", {})
    if not isinstance(skills, dict):
        return frozenset()

    result: set[str] = set()
    for item in skills.values():
        if not isinstance(item, dict):
            continue
        name = norm(item.get("name"))
        if not name:
            continue
        key = re.sub(r"\s+", "", name).upper()
        result.add(re.sub(r"EX$", "", key))
    return frozenset(result)


@lru_cache(maxsize=16384)
def _canonical_skill_text(text: str) -> str:
    """Canonicalize one string while reusing the scoring hot-path result."""

    text = re.sub(r"\s+", "", text.strip()).upper()
    text = re.sub(r"EX$", "", text)
    if text=='漫天过海':text='瞒天过海'
    if text.startswith("神圣"):
        candidate = text[2:].lstrip("·•:_-")
        if candidate in _silver_hunter_skill_keys():
            text = candidate
    return text


def canonical_skill(value: Any) -> str:
    text = value if isinstance(value, str) else norm(value)
    return _canonical_skill_text(text)


@lru_cache(maxsize=128)
def data_job_for(job: str) -> str:
    job_name = norm(job)
    return _JOB_DATA_ALIASES.get(job_name, job_name)


__all__ = [
    "canonical_skill",
    "data_job_for",
    "_COMBAT_MODIFIER_RULES",
]
