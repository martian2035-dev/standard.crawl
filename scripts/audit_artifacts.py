#!/usr/bin/env python3
"""自动发现、恢复、打包和拉取 CNB 核验附件，凭证只驻留内存。"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests

ARCHIVE = "standard-audit.tar.gz"
RESULT = Path("local_audit/cnb")
BRANCH = "codex/standard-table-audit"
MATCH_KEYS = ("原表SHA256", "查询清单SHA256", "历史数据库SHA256")


class CNBClient:
    def __init__(self, slug, token, endpoint="https://api.cnb.cool"):
        if endpoint.rstrip("/") != "https://api.cnb.cool":
            raise ValueError("核验附件仅从 api.cnb.cool 获取")
        if not re.fullmatch(r"[\w./-]+", slug) or ".." in slug:
            raise ValueError("CNB 仓库路径无效")
        self.base = endpoint.rstrip("/") + "/" + slug + "/-"
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.cnb.api+json"}

    def get_json(self, path, **params):
        response = requests.get(self.base + path, params=params, headers=self.headers, timeout=30)
        if response.status_code != 200:
            raise ValueError(f"读取 CNB 失败（HTTP {response.status_code}），停止以免丢失进度")
        return response.json()

    def commits(self, branch, current=None, explicit=None):
        if explicit:
            if not re.fullmatch(r"[a-fA-F0-9]{40}", explicit):
                raise ValueError("指定恢复提交必须是完整的 40 位 SHA")
            return [explicit]
        commits = ([current] if current else []) + [c["sha"] for c in self.get_json("/git/commits", sha=branch, page_size=50)]
        return list(dict.fromkeys(commits))

    def download(self, commit, asset, archive):
        response = requests.get(self.base + f"/commit-assets/download/{commit}/{ARCHIVE}",
                                headers=self.headers, timeout=30, allow_redirects=False)
        location = response.headers.get("Location", "")
        if response.status_code != 302 or urlparse(location).scheme != "https":
            raise ValueError(f"附件下载地址异常（HTTP {response.status_code}）")
        with requests.get(location, timeout=60, stream=True) as download:
            if download.status_code != 200:
                raise ValueError(f"下载附件失败（HTTP {download.status_code}）")
            with archive.open("wb") as f:
                for chunk in download.iter_content(1024 * 1024):
                    f.write(chunk)
        if asset.get("hash_algo", "").lower() in ("sha256", "sha-256") and asset.get("hash_value"):
            with archive.open("rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != asset["hash_value"]:
                    raise ValueError("CNB 附件哈希不一致")


def extract(archive, directory):
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] != "audit_result" or ".." in parts or not (member.isfile() or member.isdir()):
                raise ValueError("附件包含非核验目录或不安全路径")
        tar.extractall(directory, filter="data")
    return Path(directory) / "audit_result"


def find_result(client, commits, expected, destination, explicit=False):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    for commit in commits:
        assets = client.get_json(f"/git/commit-assets/{commit}")
        asset = next((a for a in assets if a["name"] == ARCHIVE), None)
        if not asset:
            continue
        with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
            archive = Path(directory) / ARCHIVE
            client.download(commit, asset, archive)
            restored = extract(archive, directory)
            metadata = json.loads((restored / "输入映射.json").read_text())
            if any(metadata[k] != expected[k] for k in MATCH_KEYS):
                if explicit:
                    raise ValueError("指定附件与当前原表、查询清单或历史库不匹配")
                continue
            shutil.copytree(restored, destination, dirs_exist_ok=True)
        print(f"已自动获取提交 {commit[:8]} 的核验进度和证据。", flush=True)
        return commit
    if explicit:
        raise ValueError("指定恢复提交没有核验附件，请检查 SHA 或附件有效期")
    return None


def cloud_client():
    return CNBClient(os.environ["CNB_REPO_SLUG"], os.environ["CNB_TOKEN"], os.environ.get("CNB_API_ENDPOINT", "https://api.cnb.cool"))


def restore():
    client = cloud_client()
    explicit = os.environ.get("AUDIT_RESTORE_COMMIT")
    commits = client.commits(os.environ.get("CNB_BRANCH", BRANCH), os.environ["CNB_COMMIT"], explicit)
    expected = json.loads(Path("audit_inputs/manifest.json").read_text())
    restored = find_result(client, commits, expected, RESULT, bool(explicit))
    (RESULT / "本片完成.flag").unlink(missing_ok=True)
    if not restored:
        if os.environ.get("CNB_EVENT") == "api_trigger_audit_continue":
            raise ValueError("自动续跑没有找到前片成果，停止以免重复采集")
        print("当前输入没有已有核验附件，从历史筛查开始。", flush=True)


def pack():
    if not (RESULT / "核验数据.sqlite3").exists():
        print("尚未生成核验数据，不打包空附件。", flush=True)
        return
    from audit_excel import export_excel
    shutil.copyfile("audit_inputs/manifest.json", RESULT / "输入映射.json")
    summary_path = RESULT / "运行摘要.json"
    if not (RESULT / "本片完成.flag").exists() and summary_path.exists():
        summary = json.loads(summary_path.read_text())
        summary.update({"自动续跑": False, "任务状态": "本片异常中止，已保留已有结果"})
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    export_excel(RESULT)
    archive = RESULT.parent / ARCHIVE
    temporary = archive.with_suffix(".tmp")
    with tarfile.open(temporary, "w:gz") as tar:
        tar.add(RESULT, arcname="audit_result")
    temporary.replace(archive)
    print(f"成果附件：{archive}（{archive.stat().st_size:,} 字节，包含 Excel）", flush=True)


def next_action():
    summary_path = RESULT / "运行摘要.json"
    should_continue = (RESULT / "本片完成.flag").exists() and summary_path.exists()
    if should_continue:
        should_continue = bool(json.loads(summary_path.read_text()).get("自动续跑"))
    if should_continue:
        current = cloud_client().get_json("/git/commits", sha=os.environ.get("CNB_BRANCH", BRANCH), page_size=1)[0]["sha"]
        if current != os.environ["CNB_COMMIT"]:
            should_continue = False
            print("分支已有新提交，停止旧提交续跑；新流水线会自动恢复成果。", flush=True)
    print("##[set-output audit_next=" + ("continue" if should_continue else "stop") + "]", flush=True)


def local_client():
    remote = subprocess.check_output(["git", "remote", "get-url", "origin"], text=True).strip()
    parsed = urlparse(remote)
    if parsed.scheme != "https" or parsed.hostname != "cnb.cool":
        raise ValueError("当前 origin 必须为 cnb.cool 的 HTTPS 仓库")
    slug = parsed.path.strip("/").removesuffix(".git")
    credential = subprocess.run(["git", "credential", "fill"],
        input=f"protocol=https\nhost=cnb.cool\npath={slug}.git\n\n", capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, check=True)
    fields = dict(s.split("=", 1) for s in credential.stdout.splitlines() if "=" in s)
    if not fields.get("password"):
        raise ValueError("没有可用的 CNB Git 凭证，请先配置该仓库的 Git 访问")
    return CNBClient(slug, fields["password"])


def pull_result(branch, destination):
    client = local_client()
    expected = json.loads(Path("audit_inputs/manifest.json").read_text())
    commit = find_result(client, client.commits(branch), expected, destination)
    if not commit:
        raise ValueError("当前输入暂时没有可下载的成果，请等待 CNB 完成本片后再拉取")
    return commit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("restore", "pack", "next"))
    args = parser.parse_args()
    try:
        {"restore": restore, "pack": pack, "next": next_action}[args.operation]()
    except requests.RequestException as exc:
        print("附件请求失败：" + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError, KeyError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print("附件处理失败：" + str(exc), file=sys.stderr)
        sys.exit(1)
