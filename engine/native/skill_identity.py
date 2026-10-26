"""技能号反查怪物模板：中途开抓也能认出 Boss 和副本。

**为什么需要它。**

实体表 ``decoder.monster_entities`` 只由 88 字节的怪物出场包
（``decode_v576_monster_spawn``）填充。进本之后才点「开始统计」时，Boss 的
出场包早就过去了，于是：

* ``decode_v576_boss_actions`` 拿不到 ``boss_actor_ids`` → 技能倒计时为空；
* ``packet_boss_health_events`` 查不到模板 → 血量/语音全空；
* 悬浮窗只显示「未识别」。

**为什么技能号够用。**

服务端的 Boss 技能起始记录（``0e 16 00`` + skill u32）里的技能号来自怪物技能
表，**全局唯一**：同一个技能号永远属于同一个怪物模板。它和实体表无关，所以
中途开抓也能读到。本模块维护一张 ``技能号 -> 模板号`` 的索引：

1. **自学习**：只要实体表是健康的（进本前就开统计），每来一条技能起始记录就能
   拿到 ``(技能号, actor->模板)`` 的确定配对，写进索引文件；
2. **反查**：实体表为空时，用技能号查出模板，把 ``actor -> 模板`` 注进解码器
   （``seed_monster_entities``），后续血量/技能/语音全部回到老逻辑。

同一个技能号如果先后配到不同模板，按命中次数投票取最多的那个，索引会自愈。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_INDEX_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "profiles"
    / "skill_template_index.json"
)


def _user_index_path() -> Path:
    """随包目录不可写时的落盘位置（便携版装在 Program Files 的场景）。"""

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path.home()
    return root / "DragonNestDpsCapture" / "skill_template_index.json"

# 文件里只留命中次数够的条目；1 次的可能是巧合，下一次学习会补上。
_MIN_HITS_TO_RESOLVE = 1
_SAVE_DEBOUNCE_SECONDS = 8.0
_MAX_SKILLS = 20_000
_MAX_VOTES_PER_SKILL = 8

_LOCK = threading.Lock()
_skills: dict[int, dict[int, int]] = {}
_loaded_mtime: tuple[float, float] = (-1.0, -1.0)
_dirty = False
_last_save = 0.0


def _read_file(path: Path) -> dict[int, dict[int, int]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = payload.get("skills") if isinstance(payload, dict) else None
    if not isinstance(rows, dict):
        return {}
    out: dict[int, dict[int, int]] = {}
    for raw_skill, entry in rows.items():
        try:
            skill = int(raw_skill)
        except (TypeError, ValueError):
            continue
        votes: dict[int, int] = {}
        if isinstance(entry, dict):
            for raw_template, raw_hits in entry.items():
                try:
                    template_id = int(raw_template)
                    hits = int(raw_hits)
                except (TypeError, ValueError):
                    continue
                if template_id > 0 and hits > 0:
                    votes[template_id] = hits
        else:
            # 兼容 {skill: template} 的最简形态
            try:
                template_id = int(entry)
            except (TypeError, ValueError):
                template_id = 0
            if template_id > 0:
                votes[template_id] = 1
        if votes:
            out[skill] = votes
    return out


def _ensure_loaded() -> None:
    global _skills, _loaded_mtime
    try:
        mtime = _INDEX_PATH.stat().st_mtime
    except OSError:
        mtime = -1.0
    user_path = _user_index_path()
    try:
        user_mtime = user_path.stat().st_mtime
    except OSError:
        user_mtime = -1.0
    stamp = (mtime, user_mtime)
    if stamp == _loaded_mtime:
        return
    _skills = _read_file(_INDEX_PATH) if mtime >= 0 else {}
    if user_mtime >= 0:
        # 用户目录里的增量与随包基线合并（票数相加），两边都生效。
        for skill, votes in _read_file(user_path).items():
            merged = _skills.setdefault(skill, {})
            for template_id, hits in votes.items():
                merged[template_id] = merged.get(template_id, 0) + hits
    _loaded_mtime = stamp


def index_path() -> Path:
    return _INDEX_PATH


def index_snapshot() -> dict[int, dict[int, int]]:
    with _LOCK:
        _ensure_loaded()
        return {skill: dict(votes) for skill, votes in _skills.items()}


def stats() -> dict[str, Any]:
    """给诊断接口用：索引有多大、多新。"""

    with _LOCK:
        _ensure_loaded()
        skills = len(_skills)
        pairs = sum(len(votes) for votes in _skills.values())
    mtimes = []
    for path in (_INDEX_PATH, _user_index_path()):
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    mtime = max(mtimes) if mtimes else 0.0
    return {
        "path": str(_INDEX_PATH),
        "skills": skills,
        "pairs": pairs,
        "updatedAt": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)) if mtime else ""
        ),
    }


def learn_skill_template(skill_id: Any, template_id: Any) -> bool:
    """记一次 ``技能号 -> 模板`` 配对。返回是否在内存里发生更新。"""

    global _dirty
    try:
        skill = int(skill_id)
        template = int(template_id)
    except (TypeError, ValueError):
        return False
    if skill <= 0 or template <= 0:
        return False
    with _LOCK:
        _ensure_loaded()
        votes = _skills.setdefault(skill, {})
        votes[template] = votes.get(template, 0) + 1
        if len(votes) > _MAX_VOTES_PER_SKILL:
            # 保留票数最高的若干项，并且**永远保留刚命中的那个**：否则新出现的
            # 模板每次都被当成最低票丢掉，永远投不出反超。
            keep = {template}
            for candidate in sorted(votes, key=lambda key: votes[key], reverse=True):
                if len(keep) >= _MAX_VOTES_PER_SKILL:
                    break
                keep.add(candidate)
            for drop in set(votes) - keep:
                votes.pop(drop, None)
        if len(_skills) > _MAX_SKILLS:
            for drop in list(_skills)[: len(_skills) - _MAX_SKILLS]:
                _skills.pop(drop, None)
        _dirty = True
        return True


def resolve_skill_template(skill_id: Any) -> int:
    """技能号 -> 模板号；索引里没有就返回 0。"""

    try:
        skill = int(skill_id)
    except (TypeError, ValueError):
        return 0
    if skill <= 0:
        return 0
    with _LOCK:
        _ensure_loaded()
        votes = _skills.get(skill)
        if not votes:
            return 0
        template, hits = max(votes.items(), key=lambda item: item[1])
        return template if hits >= _MIN_HITS_TO_RESOLVE else 0


def save(force: bool = False) -> bool:
    """写回索引文件（带防抖）。返回是否真的写了。"""

    global _dirty, _last_save
    now = time.time()
    with _LOCK:
        if not _dirty:
            return False
        if not force and now - _last_save < _SAVE_DEBOUNCE_SECONDS:
            return False
        _last_save = now
        payload = {
            "version": "v576",
            "source": "learned: 0e16 skill-start x monster spawn",
            "description": (
                "技能号 -> 模板号 的投票表。同一技能号来自怪物技能表，全局唯一；"
                "用于进本后才开统计时反推 Boss。"
            ),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "skills": {
                str(skill): {str(template): int(hits) for template, hits in votes.items()}
                for skill, votes in sorted(_skills.items())
            },
        }
        body = json.dumps(payload, ensure_ascii=False, indent=0)
        for target in (_INDEX_PATH, _user_index_path()):
            temp = target.with_suffix(".json.tmp")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                temp.write_text(body, encoding="utf-8")
                temp.replace(target)
            except OSError:
                continue
            _dirty = False
            return True
        return False


def reset_cache() -> None:
    """测试用：丢掉内存缓存，强制下次重新读文件。"""

    global _skills, _loaded_mtime, _dirty, _last_save
    with _LOCK:
        _skills = {}
        _loaded_mtime = (-1.0, -1.0)
        _dirty = False
        _last_save = 0.0
    _TOKEN_CACHE.clear()
    _CATALOG_CACHE.clear()


# ---------------------------------------------------------------------------
# 兜底：用 boss_skills.json 的目录名反推模板
#
# 自学习索引要靠「曾经完整抓过一次」才有数据。新号第一次进本（或者某关还没
# 打过）时索引是空的，但技能目录里已经写着「食人魔 · 砸地」这种 Boss 词干，
# 而模板表里也有「百夫长食人魔巴欧帕」。两边一对就能拿到模板号。
#
# 只在**唯一**候选时才认：同一个词干命中多个模板、且其中恰好一个是 boss 标记
# 的，取 boss 那个（红龙有 3 个部件模板 + 1 个本体）；仍不唯一就放弃，绝不猜。
# "硬核·xxx" 这类名字只表示副本难度，不指名 Boss，直接跳过。
# ---------------------------------------------------------------------------

_CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "profiles"
    / "boss_skills.json"
)

_TOKEN_CACHE: dict[tuple[int, int, str], int] = {}
_CATALOG_CACHE: dict[int, str] = {}


def _catalog_tokens() -> dict[int, str]:
    if _CATALOG_CACHE:
        return _CATALOG_CACHE
    try:
        payload = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _CATALOG_CACHE
    rows = payload.get("skills") if isinstance(payload, dict) else None
    if not isinstance(rows, dict):
        return _CATALOG_CACHE
    for raw_skill, entry in rows.items():
        try:
            skill = int(raw_skill)
        except (TypeError, ValueError):
            continue
        name = str((entry or {}).get("name") or "").strip()
        if not name or "·" not in name or name.startswith("硬核·"):
            continue
        token = name.split("·")[0].strip()
        if len(token) >= 2:
            _CATALOG_CACHE[skill] = token
    return _CATALOG_CACHE


def catalog_token(skill_id: Any) -> str:
    """技能号在目录里的 Boss 词干（"食人魔"）；没有就返回空串。"""

    try:
        skill = int(skill_id)
    except (TypeError, ValueError):
        return ""
    return _catalog_tokens().get(skill, "")


def catalog_template(skill_id: Any, templates: Any, nest_templates: Any = None) -> int:
    """技能号 -> 模板号（靠目录里的 Boss 词干匹配模板名）。

    ``templates`` 是解码器已经加载好的 ``{template_id: template}``，这里不再
    重复读 18k 条模板文件。只返回唯一候选，宁缺勿猜。
    """

    token = catalog_token(skill_id)
    if not token or not isinstance(templates, dict):
        return 0
    cache_key = (id(templates), len(templates), token)
    cached = _TOKEN_CACHE.get(cache_key)
    if cached is not None:
        return cached
    nests = nest_templates if isinstance(nest_templates, dict) else {}
    matches: list[tuple[int, bool]] = []
    for raw_id, template in templates.items():
        try:
            template_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if nests and template_id not in nests:
            continue
        name = str(getattr(template, "name", "") or "")
        if token in name:
            matches.append((template_id, bool(getattr(template, "boss", False))))
    resolved = 0
    bosses = [template_id for template_id, is_boss in matches if is_boss]
    if len(bosses) == 1:
        resolved = bosses[0]
    elif len(matches) == 1:
        resolved = matches[0][0]
    _TOKEN_CACHE[cache_key] = resolved
    return resolved

