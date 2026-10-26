"""把 ``.dpscap`` 离线重放成证据行，用于替代实战验证机制播报。

    python tools/capture_replay.py <dpscap|目录> [--out DIR]

实现放在 ``engine.live.capture_replay``，因为开发者模式里的
「回放实战录像」也要用它；这里只是命令行入口。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.live.capture_replay import (  # noqa: E402
    collect_paths,
    describe,
    replay,
)

DATA_ROOT = ROOT / "engine" / "data"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="+")
    parser.add_argument("--out", default="", help="导出目录（每包一个 .json）")
    args = parser.parse_args()

    paths = collect_paths(args.targets)
    if not paths:
        print("没有找到 .dpscap 文件", file=sys.stderr)
        return 2

    out_dir = Path(args.out) if args.out else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for path in paths:
        result = replay(path, DATA_ROOT)
        print(describe(result))
        if result.drops:
            failures += 1
        if out_dir is not None:
            (out_dir / f"{path.stem}.json").write_text(
                json.dumps(result.to_json(), ensure_ascii=False),
                encoding="utf-8",
            )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
