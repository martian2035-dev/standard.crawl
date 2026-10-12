#!/usr/bin/env python3
"""Export allowlisted public files without old Git history, databases or private input."""
import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Use an explicit allowlist so new debug scripts, notes and private configuration stay local.
PUBLIC_FILES = {
    ".gitignore", ".python-version", "README.md", "pyproject.toml", "uv.lock",
    "requirements.txt", "requirements-audit.txt", "requirements-local.txt",
    "standard_crawl.py", "standard_audit.py", "audit_excel.py", "audit_pipeline.py", "build_db.py",
    "国家公开标准信息.csv", "行业公开标准信息.csv", "地方公开标准信息.csv", "工程建设标准信息.csv",
    "public_standards.csv", "docs/local-audit.md", "docs/github-actions.md",
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
        raise ValueError("Export destination must be outside this project")
    if destination.exists():
        raise ValueError("Export destination already exists; choose a new path to avoid overwriting files")
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
            raise ValueError("File exceeds the GitHub regular Git size limit: " + name)
        selected.append((source, relative))
    if not any(str(relative) == "public_standards.csv" for _, relative in selected):
        raise ValueError("Missing public_standards.csv; run public_input.py prepare locally first")
    # Validate with the standard library; export does not require Excel dependencies.
    import csv
    with (ROOT / "public_standards.csv").open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != ["标准类型", "标准号"] or any(None in row for row in reader):
            raise ValueError("Public query list contains extra fields; refusing to export")
    destination.mkdir(parents=True)
    for source, relative in selected:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    print(f"Exported {len(selected)} files to {destination}. No Git repository created or remote publication performed.")
    return selected


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    try:
        export(parser.parse_args().destination)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, str(exc) + "\n")
