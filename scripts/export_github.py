#!/usr/bin/env python3
"""导出当前工作树为 GitHub 首次发布目录，不复制旧 Git 历史和数据库。"""
import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git", "standard_db", ".crawl_state", ".github_crawl_state", ".github_audit_work",
            ".github_release", "local_audit", ".venv", "__pycache__", "github-export", "reports"}
SUFFIXES = {".py", ".sh", ".md", ".yml", ".yaml", ".csv", ".toml", ".lock", ".txt"}
SPECIAL = {".gitignore", ".python-version", "标准信息统计表.xlsx", "audit_inputs/manifest.json"}


def export(destination):
    destination = Path(destination).resolve()
    if destination == ROOT or ROOT in destination.parents:
        raise ValueError("导出目录必须位于当前项目之外")
    if destination.exists():
        raise ValueError("导出目录已存在，请换一个空的新路径，避免覆盖文件")
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    ).decode().split("\0")
    selected = []
    for name in sorted(set(paths)):
        relative = Path(name)
        if not name or any(p in EXCLUDED for p in relative.parts):
            continue
        source = ROOT / relative
        if source.is_symlink() or not source.is_file():
            continue
        if relative.suffix not in SUFFIXES and name not in SPECIAL:
            continue
        if source.stat().st_size >= 100 * 1024 * 1024:
            raise ValueError("文件超过 GitHub 普通 Git 限制：" + name)
        selected.append((source, relative))
    if not any(str(relative) == "标准信息统计表.xlsx" for _, relative in selected):
        raise ValueError("缺少标准信息统计表.xlsx，无法配置月度核验")
    destination.mkdir(parents=True)
    for source, relative in selected:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    print(f"已导出 {len(selected)} 个文件至 {destination}。未建立 Git 仓库，也未发布到远端。")
    return selected


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    try:
        export(parser.parse_args().destination)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, str(exc) + "\n")
