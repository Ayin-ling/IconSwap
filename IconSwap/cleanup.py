#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IconSwap 上传 GitHub 前的清理脚本。

删除的都是**可再生成的产物**（打包目录、缓存、*.spec、*_original.exe 备份），
永远不会碰 src/ 、bin/ 、.git/ 以及 README.md / LICENSE / requirements.txt /
.gitignore。

用法（放在项目根目录）：

    python cleanup.py             # 直接清理
    python cleanup.py -n          # 只预览，不删除（dry-run）
    python cleanup.py --exe       # 额外删除根目录的 IconSwap.exe（会二次确认）
    python cleanup.py -y          # 配合 --exe，跳过确认
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

#: 整个目录都不许删（相对项目根的第一段路径）
PROTECTED_DIRS = {"src", "bin", ".git", ".github", "assets", "docs"}

#: 根目录里这些文件不许删
PROTECTED_FILES = {
    "readme.md",
    "license",
    "requirements.txt",
    "requirements-desktop.txt",
    ".gitignore",
    "pyproject.toml",
    "third-party-notices.md",
    "build_exe.bat",
    "cleanup.py",
    "cleanup.bat",
}

#: 需要整目录删除的缓存 / 产物（相对项目根）
TARGET_DIRS = [
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "tests/_tmp",
    "tests/_tmp_modes",
    "tests/_e2e",
    "IconsLibrary",
]

#: 按通配符删除的文件
TARGET_GLOBS = ["*.spec", "*_original.exe"]

#: 大文件提醒阈值（MB）
BIG_FILE_MB = 20


class Report:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.removed = 0
        self.bytes_freed = 0
        self.failed: list[Path] = []
        self.skipped: list[Path] = []

    def note(self, action: str, path: Path, size: int = 0) -> None:
        shown = self._display(path)
        if action == "del":
            print(f"  [删除] {shown}" + (f"  ({size / 1048576:.1f} MB)" if size >= 1048576 else ""))
            self.removed += 1
            self.bytes_freed += size
        elif action == "dry":
            print(f"  [预演] 将删除 {shown}")
            self.removed += 1
            self.bytes_freed += size
        elif action == "skip":
            print(f"  [跳过] {shown}  (受保护)")
            self.skipped.append(path)
        elif action == "fail":
            print(f"  [失败] {shown}  (被占用或权限不足)")
            self.failed.append(path)

    def _display(self, path: Path) -> str:
        try:
            rel = path.resolve().relative_to(ROOT.resolve())
            text = str(rel).replace("\\", "/")
        except ValueError:
            text = str(path)
        return text + ("/" if path.is_dir() else "")


def is_protected(path: Path) -> bool:
    """判断路径是否属于“绝不删除”范围。

    唯一例外：``__pycache__`` 目录即使位于 ``src/``、``bin/`` 里也允许删除，
    因为它只是编译缓存（源码文件一个都不会动）。
    """
    try:
        rel = path.resolve().relative_to(ROOT.resolve())
    except ValueError:  # 不在项目内，保守起见一律不动
        return True

    parts = rel.parts
    if not parts:
        return True
    # .git / .github 整棵树都不动（哪怕里面出现 __pycache__）
    if parts[0].lower() in {".git", ".github"}:
        return True
    # 唯一例外：__pycache__ 只是编译缓存，位于 src/、bin/ 里也可删
    if path.name.lower() == "__pycache__" and path.is_dir():
        return False
    if parts[0].lower() in PROTECTED_DIRS:
        return True
    if len(parts) == 1 and parts[0].lower() in PROTECTED_FILES:
        return True
    return False


def dir_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def remove_dir(path: Path, report: Report) -> None:
    if not path.is_dir():
        return
    if is_protected(path):
        report.note("skip", path)
        return
    size = dir_size(path)
    if report.dry_run:
        report.note("dry", path, size)
        return
    try:
        shutil.rmtree(path)
    except OSError:
        report.note("fail", path)
        return
    report.note("del", path, size)


def remove_file(path: Path, report: Report) -> None:
    if not path.is_file():
        return
    if is_protected(path):
        report.note("skip", path)
        return
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if report.dry_run:
        report.note("dry", path, size)
        return
    try:
        path.unlink()
    except OSError:
        report.note("fail", path)
        return
    report.note("del", path, size)


def find_dirs(pattern: str) -> list[Path]:
    """收集匹配的目录，深层的排在前面（先删子目录再删父目录）。"""
    found = [p for p in ROOT.rglob(pattern) if p.is_dir()]
    found.sort(key=lambda p: len(p.parts), reverse=True)
    return found


def find_files(pattern: str) -> list[Path]:
    return sorted(p for p in ROOT.rglob(pattern) if p.is_file())


def check_project_root() -> bool:
    """安全闸：必须在 IconSwap 项目根目录运行。"""
    if not (ROOT / "src" / "main.py").is_file() or not (ROOT / "requirements.txt").is_file():
        print()
        print("  [停止] 这里看起来不是 IconSwap 的项目根目录。")
        print("         请把 cleanup.py 放到包含 src/main.py 和 requirements.txt 的目录再运行。")
        print()
        return False
    return True


def human(mb_bytes: int) -> str:
    if mb_bytes >= 1073741824:
        return f"{mb_bytes / 1073741824:.2f} GB"
    return f"{mb_bytes / 1048576:.1f} MB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="清理 IconSwap 项目中会导致 GitHub 上传失败的缓存与打包产物",
    )
    parser.add_argument("-n", "--dry-run", action="store_true", help="只预览，不删除")
    parser.add_argument("--exe", action="store_true", help="额外删除根目录的 IconSwap.exe")
    parser.add_argument("-y", "--yes", action="store_true", help="配合 --exe 跳过确认")
    args = parser.parse_args(argv)

    if not check_project_root():
        return 1

    report = Report(dry_run=args.dry_run)
    mode = "预演模式（不会删除任何文件）" if args.dry_run else "开始清理"
    print()
    print("=" * 60)
    print(f"  IconSwap 清理脚本 · {mode}")
    print(f"  目录：{ROOT}")
    print("=" * 60)

    # 1) 打包 / 缓存目录
    print("\n[1/4] 打包目录与缓存")
    for name in TARGET_DIRS:
        remove_dir(ROOT / name, report)

    # 2) 所有 __pycache__
    print("\n[2/4] __pycache__")
    for path in find_dirs("__pycache__"):
        remove_dir(path, report)

    # 3) *.spec 与 *_original.exe
    print("\n[3/4] *.spec / *_original.exe")
    for pattern in TARGET_GLOBS:
        for path in find_files(pattern):
            remove_file(path, report)

    # 4) 根目录打包出的 exe
    print("\n[4/4] 根目录打包 exe")
    exe = ROOT / "IconSwap.exe"
    if exe.is_file():
        size_mb = exe.stat().st_size / 1048576
        print(f"  发现 IconSwap.exe（{size_mb:.1f} MB）")
        if not args.exe:
            print("  它已被 .gitignore 忽略：用 git 提交不会带上它（网页拖拽上传才会）。")
            print("  确实要删掉它（桌面快捷方式会失效）请加 --exe 参数。")
        elif args.dry_run:
            report.note("dry", exe, exe.stat().st_size)
        else:
            confirmed = args.yes
            if not confirmed:
                try:
                    answer = input("  确认删除 IconSwap.exe 吗？(y/N) ").strip().lower()
                except EOFError:
                    answer = ""
                confirmed = answer in {"y", "yes"}
            if confirmed:
                remove_file(exe, report)
            else:
                print("  [保留] IconSwap.exe")
    else:
        print("  没有根目录打包产物，跳过。")

    # 汇总
    print()
    print("-" * 60)
    if args.dry_run:
        print(f"  预计处理 {report.removed} 项，可释放约 {human(report.bytes_freed)}")
        print("  （这是预演，未删除任何文件；去掉 -n 再运行一次即可真正清理）")
    else:
        print(f"  已处理 {report.removed} 项，释放约 {human(report.bytes_freed)}")
    if report.failed:
        print(f"  失败 {len(report.failed)} 项（通常是被占用，关掉资源管理器/杀软后重试）：")
        for path in report.failed[:10]:
            print(f"    - {report._display(path)}")
    print("-" * 60)
    print()

    print("清理完成！现在你可以把剩余的文件上传到 GitHub 了。")
    print()

    if not args.dry_run:
        print("  提示：")
        print("    1) 用 git 提交才会遵守 .gitignore（网页拖拽上传不会）。")
        print("    2) 提交前可执行 git status，确认没有 build/、dist/、IconSwap.exe 被加入。")
        print("    3) 图标库目录（IconsLibrary/）已废弃，删掉不会影响功能。")
        print()
    return 0 if not report.failed else 2


if __name__ == "__main__":
    sys.exit(main())
