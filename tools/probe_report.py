"""把 .dev/probe/*.jsonl 汇总成一份可读报告。

用法：
    .venv\\Scripts\\python.exe tools\\probe_report.py                 # 最新一份
    .venv\\Scripts\\python.exe tools\\probe_report.py <file.jsonl>
    .venv\\Scripts\\python.exe tools\\probe_report.py --all           # 全部文件概览

它回答四个问题：
  1. 副本是怎么识别的（切图包 / Boss 模板）
  2. 这个副本名会不会被前端当成「黑龙经典」
  3. Boss 实体绑上了没有，绑的是哪个模板
  4. Boss 实际发的技能号有哪些，其中多少不在 boss_skills.json 里
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE_DIR = ROOT / ".dev" / "probe"
DEFAULT_RECORD_DIR = Path.home() / "Documents" / "DragonNestDpsCapture" / "debug_records"


def _configured_record_dir() -> Path | None:
    """读取设置页「调试记录」里的保存目录（不导入后端，避免副作用）。"""

    try:
        payload = json.loads(
            (ROOT / "data" / "debug_recording.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    raw = str(payload.get("outputDir") or "").strip() if isinstance(payload, dict) else ""
    return Path(raw).expanduser() if raw else None


def _probe_dirs() -> list[Path]:
    candidates = [PROBE_DIR]
    configured = _configured_record_dir()
    if configured is not None:
        candidates.append(configured / "probe")
    candidates.append(DEFAULT_RECORD_DIR / "probe")
    seen: set[str] = set()
    ordered: list[Path] = []
    for directory in candidates:
        key = str(directory).casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(directory)
    return ordered


def _probe_files() -> list[Path]:
    files: list[Path] = []
    for directory in _probe_dirs():
        try:
            files.extend(directory.glob("*.jsonl"))
        except OSError:
            continue
    return sorted(files, key=lambda path: path.stat().st_mtime if path.exists() else 0.0)


def _describe_search_dirs() -> str:
    return "\n  ".join(str(directory) for directory in _probe_dirs())


def load(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def summarize(path: Path) -> None:
    rows = load(path)
    print("=" * 74)
    print(f"文件：{path}")
    print(f"行数：{len(rows):,}")
    kinds = collections.Counter(row.get("kind") for row in rows)
    print("事件：" + ", ".join(f"{k}={v}" for k, v in kinds.most_common()))

    header = next((r for r in rows if r.get("kind") == "header"), {})
    if header:
        print(f"头部：wall={header.get('wall')} pid={header.get('pid')} "
              f"mode={header.get('actionParserMode')}")

    nests = [r for r in rows if r.get("kind") == "nest_identity"]
    if nests:
        print("\n-- 副本身份 --")
        for row in nests:
            print(f"  t={row.get('t')}s route={row.get('route')} "
                  f"map={row.get('map_id', '-')} nest={row.get('nest_name')!r} "
                  f"classic={row.get('classic_regex_match')}")
    else:
        print("\n-- 副本身份 --\n  没有 nest_identity 记录：切图包和 Boss 模板两条路都没有建立副本。")

    maps = [r for r in rows if r.get("kind") == "map_hit"]
    if maps:
        print("\n-- 地图包命中（前 10）--")
        for row in maps[:10]:
            print(f"  t={row.get('t')}s map={row.get('map_id')} "
                  f"{row.get('name')!r} battle={row.get('battle')} "
                  f"active={row.get('active')} layout={row.get('layout')}")
    else:
        print("\n-- 地图包命中 --\n  0 次。说明本次抓包没有解出任何地图身份包。")

    binds = [r for r in rows if r.get("kind") == "boss_bind"]
    print("\n-- Boss 实体 --")
    if binds:
        for row in binds[:20]:
            print(f"  t={row.get('t')}s actor={row.get('actor')} "
                  f"template={row.get('template_id')} boss={row.get('boss')} "
                  f"count={row.get('bossActorCount')} phase={row.get('phase')!r}")
    else:
        print("  0 条 boss_bind：没有任何怪物被判定为 Boss，三段技能解码都不会执行。")

    spawns = [r for r in rows if r.get("kind") == "spawn_template"]
    if not spawns:
        spawns = [r for r in rows if r.get("kind") == "spawn_hit"]
    if spawns:
        print("\n-- 怪物出生（按模板去重，前 20）--")
        seen = set()
        shown = 0
        for row in spawns:
            key = row.get("template_id")
            if key in seen:
                continue
            seen.add(key)
            shown += 1
            if shown > 20:
                break
            print(f"  t={row.get('t')}s actor={row.get('actor')} "
                  f"template={key} known={row.get('known')} boss={row.get('boss')} "
                  f"name={row.get('name')!r}")
        print(f"  （共 {len(seen)} 个不同模板）")
    else:
        print("\n-- 怪物出生 --\n  0 条 spawn_hit：抓包里没有可解出的出生包（88 字节 / 06275500）。")

    candidates = [r for r in rows if r.get("kind") == "skill_candidate"]
    if candidates:
        inside = [r for r in candidates if r.get("in_catalog")]
        outside = [r for r in candidates if not r.get("in_catalog")]
        print(f"\n-- Boss 技能号 -- 共 {len(candidates)} 个（去重后）")
        print(f"   在目录内 {len(inside)}：{sorted({r['skill'] for r in inside})}")
        print(f"   目录外 {len(outside)}：{sorted({r['skill'] for r in outside})}")
    else:
        print("\n-- Boss 技能号 --\n  0 条 skill_candidate：白名单扫描没有命中任何技能起始包。")

    shapes = [r for r in rows if r.get("kind") == "shape_stats"]
    if shapes:
        merged: collections.Counter[str] = collections.Counter()
        for row in shapes:
            for item in row.get("top") or ():
                merged[item.get("shape", "")] += int(item.get("count") or 0)
        print("\n-- 服务端报文形状 Top 15（长度:签名 → 次数）--")
        for shape, count in merged.most_common(15):
            print(f"  {shape:>24}  {count}")

    skipped = [r for r in rows if r.get("kind") == "evidence_skip"]
    if skipped:
        print("\n-- 证据被拦下的原因 Top 10 --")
        counter = collections.Counter(
            (r.get("skill"), r.get("kind"), r.get("template_id")) for r in skipped
        )
        for key, count in counter.most_common(10):
            print(f"  skill={key[0]} kind={key[1]} template={key[2]} × {count}")


def main(argv: list[str]) -> int:
    if "--all" in argv:
        files = _probe_files()
        if not files:
            print("没有找到观测日志：\n  " + _describe_search_dirs())
            return 1
        for path in files:
            rows = load(path)
            kinds = collections.Counter(r.get("kind") for r in rows)
            print(f"{path}  rows={len(rows):>6}  "
                  f"boss_bind={kinds.get('boss_bind', 0)} "
                  f"skill_candidate={kinds.get('skill_candidate', 0)} "
                  f"nest={kinds.get('nest_identity', 0)}")
        return 0

    explicit = next((Path(arg) for arg in argv[1:] if not arg.startswith("-")), None)
    if explicit is not None:
        target = explicit
    else:
        files = _probe_files()
        if not files:
            print("没有找到观测日志：\n  " + _describe_search_dirs())
            print("先在「设置 → 调试记录」打开开关并打一次副本，或用 DRAGON_NEST_NEST_PROBE_PATH 指定路径。")
            return 1
        target = files[-1]
    if not target.exists():
        print(f"文件不存在：{target}")
        return 1
    summarize(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
