#!/usr/bin/env python3
"""导出当前工作树为 GitHub 首次发布目录，不复制旧 Git 历史和数据库。"""
import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 显式列出公开文件，避免新出现的调试脚本、个人笔记或配置被自动带出。
PUBLIC_FILES = {
    ".gitignore", ".python-version", "README.md", "pyproject.toml", "uv.lock",
    "requirements.txt", "requirements-audit.txt", "requirements-local.txt",
    "standard_crawl.py", "standard_audit.py", "audit_excel.py", "audit_pipeline.py", "build_db.py",
    "国家公开标准信息.csv", "行业公开标准信息.csv", "地方公开标准信息.csv", "工程建设标准信息.csv",
    "公开标准查询清单.csv", "docs/统计表本地核验.md", "docs/GitHub迁移与月度任务.md",
    "scripts/run_local.sh", "scripts/export_github.py", "scripts/github_monthly.py",
    "scripts/github_commit.sh", "scripts/public_input.py",
    ".github/workflows/monthly-crawl.yml", ".github/workflows/monthly-audit.yml", ".github/workflows/tests.yml",
    "tests/test_audit_delivery.py", "tests/test_audit_pipeline.py", "tests/test_build_relations.py",
    "tests/test_crawl_dataset.py", "tests/test_standard_audit.py", "tests/test_github_monthly.py",
    "tests/test_public_input.py",
}


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
        if name not in PUBLIC_FILES:
            continue
        source = ROOT / relative
        if source.is_symlink() or not source.is_file():
            continue
        if source.stat().st_size >= 100 * 1024 * 1024:
            raise ValueError("文件超过 GitHub 普通 Git 限制：" + name)
        selected.append((source, relative))
    if not any(str(relative) == "公开标准查询清单.csv" for _, relative in selected):
        raise ValueError("缺少公开标准查询清单.csv，请先在本地运行 public_input.py prepare")
    # 只用标准库检查清单，导出命令无需安装 Excel 依赖。
    import csv
    with (ROOT / "公开标准查询清单.csv").open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["标准类型", "标准号"] or any(None in row for row in reader):
            raise ValueError("公开查询清单含额外字段，拒绝导出")
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
