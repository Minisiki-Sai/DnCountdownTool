#!/usr/bin/env python3
"""把 ``version.json`` 的 product 铺到 4 个打包清单里。

版本号唯一真源是仓库根的 ``version.json``；这个脚本是唯一的传播器。

    python tools/sync_version.py            # 写入
    python tools/sync_version.py --check    # 只比对，有漂移退出码 1

设计取舍：
* **不做整文件反序列化重写**。TOML / JSON 一旦被 parse 再 dump，引号风格、
  空行、键顺序、注释全都会变，diff 会炸成整文件。这里只替换 ``version``
  字段那一行的值，其余字节原样保留。
* JSON 只认**顶层** ``version``（用文件自身的顶层缩进判定），避免误改
  ``bundle.windows.version`` 之类的嵌套同名键。
* TOML 只认目标 section（``[package]`` / ``[project]``）里的裸
  ``version = "..."`` 行，不碰 ``tauri = { version = "2" }`` 这类内联表。
* 幂等：已经是目标值时不动文件（不重写、不改 mtime）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO_ROOT / "version.json"


@dataclass(frozen=True)
class Target:
    """一个需要跟随 version.json 的清单文件。"""

    path: Path
    # "json": 顶层 "version": "..." 键；"toml": 指定 section 里的裸 version 行
    kind: str
    section: str = ""


TARGETS: tuple[Target, ...] = (
    Target(REPO_ROOT / "app" / "package.json", "json"),
    Target(REPO_ROOT / "app" / "src-tauri" / "Cargo.toml", "toml", "package"),
    Target(REPO_ROOT / "app" / "src-tauri" / "tauri.conf.json", "json"),
    Target(REPO_ROOT / "pyproject.toml", "toml", "project"),
)


def read_product() -> str:
    """读 version.json 的 product 字段。"""

    try:
        payload = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except OSError as error:
        raise SystemExit(f"[sync-version] 读不到 {VERSION_FILE}: {error}") from error
    except ValueError as error:
        raise SystemExit(f"[sync-version] {VERSION_FILE} 不是合法 JSON: {error}") from error

    product = str(payload.get("product") or "").strip()
    if not product:
        raise SystemExit(f"[sync-version] {VERSION_FILE} 缺 product 字段")
    return product


def top_level_indent(lines: list[str]) -> str:
    """取 JSON 文件里第一个键的缩进，用它代表顶层缩进。"""

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped in ("{", "}", "[", "]"):
            continue
        if stripped.startswith('"'):
            return line[: len(line) - len(line.lstrip())]
    return ""


def replace_json_version(text: str, new_version: str) -> tuple[str, str] | None:
    """替换 JSON 顶层 version 值，返回 (新文本, 旧值)；找不到返回 None。"""

    lines = text.splitlines(keepends=True)
    indent = top_level_indent(lines)
    pattern = re.compile(
        r'^(?P<head>'
        + re.escape(indent)
        + r'"version"\s*:\s*")(?P<value>[^"]*)(?P<tail>"\s*,?\s*)$'
    )
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        match = pattern.match(body)
        if not match:
            continue
        old = match.group("value")
        lines[index] = f"{match.group('head')}{new_version}{match.group('tail')}{ending}"
        return "".join(lines), old
    return None


def replace_toml_version(
    text: str, new_version: str, section: str
) -> tuple[str, str] | None:
    """替换 TOML 指定 section 里的裸 version，返回 (新文本, 旧值)。"""

    lines = text.splitlines(keepends=True)
    section_pattern = re.compile(r"^\[(?P<name>[^\[\]]+)\]\s*$")
    version_pattern = re.compile(r'^(?P<head>version\s*=\s*")(?P<value>[^"]*)(?P<tail>"\s*)$')

    current = ""
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        ending = line[len(body):]

        header = section_pattern.match(body)
        if header:
            current = header.group("name").strip()
            continue
        if current != section:
            continue

        match = version_pattern.match(body)
        if not match:
            continue
        old = match.group("value")
        lines[index] = f"{match.group('head')}{new_version}{match.group('tail')}{ending}"
        return "".join(lines), old
    return None


def sync_one(target: Target, new_version: str, check_only: bool) -> tuple[str, str]:
    """处理单个文件。返回 (status, 可读描述)，status ∈ {"ok", "drift", "error"}。"""

    rel = target.path.relative_to(REPO_ROOT).as_posix()
    try:
        text = target.path.read_text(encoding="utf-8")
    except OSError as error:
        return "error", f"{rel}: 读不到 ({error})"

    if target.kind == "json":
        result = replace_json_version(text, new_version)
        missing = "顶层 version 字段"
    else:
        result = replace_toml_version(text, new_version, target.section)
        missing = f"[{target.section}] 里的裸 version 行"

    if result is None:
        return "error", f"{rel}: 找不到{missing}"

    new_text, old = result
    if old == new_version:
        return "ok", f"{rel}: {old}（已是目标值）"

    if not check_only:
        target.path.write_text(new_text, encoding="utf-8")
        return "drift", f"{rel}: {old} -> {new_version}"
    return "drift", f"{rel}: {old} -> {new_version}  [漂移，未写入]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 version.json 的 product 同步到各打包清单",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只比对不写入；有漂移时退出码 1",
    )
    args = parser.parse_args(argv)

    product = read_product()
    mode = "check" if args.check else "write"
    print(f"[sync-version] version.json product = {product}  (mode={mode}, 目标 {len(TARGETS)} 个文件)")

    drifted: list[str] = []
    errors: list[str] = []
    for target in TARGETS:
        status, message = sync_one(target, product, args.check)
        print(f"[sync-version] {message}")
        if status == "error":
            errors.append(message)
        elif status == "drift":
            drifted.append(target.path.relative_to(REPO_ROOT).as_posix())

    if errors:
        print(f"[sync-version] 失败：{len(errors)} 个文件无法处理", file=sys.stderr)
        return 1

    if args.check:
        if drifted:
            print(
                f"[sync-version] 检测到 {len(drifted)} 个文件版本漂移："
                + ", ".join(drifted),
                file=sys.stderr,
            )
            print("[sync-version] 执行 `python tools/sync_version.py` 修正", file=sys.stderr)
            return 1
        print(f"[sync-version] OK：{len(TARGETS)} 个清单全部等于 version.json")
        return 0

    if drifted:
        print(f"[sync-version] 已更新 {len(drifted)} 个文件")
    else:
        print("[sync-version] 无变更")
    return 0


if __name__ == "__main__":
    sys.exit(main())
