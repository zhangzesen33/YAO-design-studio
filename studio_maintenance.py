#!/usr/bin/env python3
"""Build GitHub release assets and update a packaged Studio installation."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent
CHANNEL = "studio-channel.json"
STATE_DIR = ".studio-update"
ACTIVE_VERSION = "current-version"
MANIFEST_ASSET = "studio-update.json"
UPDATE_ASSET = "studio-update.zip"
SHARE_ASSET = "YAO-design-studio-macOS.zip"
APP_FILES = {"server.py", "static/index.html", "vendor/tokenhub_h3_client.py"}
STATIC_SUFFIXES = {".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2"}
BOOTSTRAP = ("start.command", "更新工作台.command", "studio_maintenance.py", "README.md")
DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*")
MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_UNPACKED = 128 * 1024 * 1024


def version_parts(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or len(value) > 32 or not VERSION_RE.fullmatch(value):
        raise ValueError("版本号格式无效，应为 1.2.3。")
    return tuple(int(part) for part in value.split("."))


def releases_url(repository: str) -> str:
    if not isinstance(repository, str) or not REPO_RE.fullmatch(repository):
        raise ValueError("GitHub 仓库格式应为 owner/repo。")
    return f"https://github.com/{repository}/releases"


def is_app_file(path: PurePosixPath) -> bool:
    if any(part in {"..", "__pycache__"} or part.startswith(".") for part in path.parts):
        return False
    return path.as_posix() in APP_FILES or (
        path.parts[:1] == ("static",) and path.suffix.lower() in STATIC_SUFFIXES
    )


def build_release(root: Path, repository: str, version: str, output: Path) -> None:
    releases_url(repository)
    version_parts(version)
    files = {name: root / name for name in APP_FILES}
    for source in (root / "static").rglob("*"):
        relative = source.relative_to(root).as_posix()
        if source.is_file() and is_app_file(PurePosixPath(relative)):
            files[relative] = source
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / UPDATE_ASSET
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, source in sorted(files.items()):
            archive.write(source, name)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (output / MANIFEST_ASSET).write_text(json.dumps({"version": version, "sha256": digest}) + "\n")

    prefix = "YAO-design-studio/"
    with zipfile.ZipFile(output / SHARE_ASSET, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in BOOTSTRAP:
            info = zipfile.ZipInfo(prefix + name)
            info.create_system = 3
            mode = 0o755 if name.endswith(".command") else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, (root / name).read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr(prefix + CHANNEL, json.dumps({"repository": repository, "version": version}) + "\n")
        for name, source in sorted(files.items()):
            archive.write(source, prefix + "app/" + name)


def channel(root: Path) -> tuple[str, str]:
    path = root / CHANNEL
    if not path.is_file():
        raise ValueError("请在从 GitHub Release 下载并解压的工作台目录里运行更新。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("更新配置格式无效。")
    repository, version = data.get("repository"), data.get("version")
    releases_url(repository)
    version_parts(version)
    return repository, version


def active_app(root: Path, initial_version: str) -> tuple[str, Path]:
    pointer = root / STATE_DIR / ACTIVE_VERSION
    if not pointer.exists():
        return initial_version, root / "app"
    version = pointer.read_text(encoding="utf-8").strip()
    version_parts(version)
    return version, root / STATE_DIR / version


class GitHubRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urlsplit(new_url)
        if parsed.scheme != "https" or parsed.hostname not in DOWNLOAD_HOSTS:
            raise ValueError("GitHub 下载链接重定向到了无法验证的地址。")
        return super().redirect_request(request, response, code, message, headers, new_url)


def fetch(url: str, max_bytes: int) -> bytes:
    request = Request(url, headers={"User-Agent": "YAO-design-studio-Updater/1.0"})
    with build_opener(GitHubRedirect()).open(request, timeout=30) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("更新文件超出大小限制。")
    return data


def extract_update(data: bytes, target: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        names = set()
        folded_names = set()
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            file_type = stat.S_IFMT(entry.external_attr >> 16)
            if (
                path.as_posix() != name or "\\" in name or not is_app_file(path)
                or entry.is_dir() or file_type not in (0, stat.S_IFREG)
                or name.casefold() in folded_names
            ):
                raise ValueError("更新包含有无效或重复的文件路径。")
            names.add(name)
            folded_names.add(name.casefold())
        if not APP_FILES <= names or sum(entry.file_size for entry in entries) > MAX_UNPACKED:
            raise ValueError("更新包不完整或解压后大小超出限制。")
        for entry in entries:
            destination = target / entry.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output)


def update(root: Path) -> None:
    repository, initial_version = channel(root)
    releases = releases_url(repository)
    state = root / STATE_DIR
    state.mkdir(exist_ok=True)
    with (state / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("已有一个更新程序在运行。") from None

        current, _ = active_app(root, initial_version)
        print(f"当前版本 {current}，正在检查 GitHub Releases…", flush=True)
        manifest = json.loads(fetch(f"{releases}/latest/download/{MANIFEST_ASSET}", 64 * 1024))
        if not isinstance(manifest, dict):
            raise ValueError("发布清单格式无效。")
        version, digest = manifest.get("version"), manifest.get("sha256")
        if version_parts(version) <= version_parts(current):
            print(f"已是最新版本 {current}。")
            return
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("发布清单缺少有效的 SHA-256 校验值。")

        print(f"正在下载 {version}…", flush=True)
        data = fetch(f"{releases}/download/v{version}/{UPDATE_ASSET}", MAX_DOWNLOAD)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("更新包校验失败。")
        # The active app keeps its own directory for the lifetime of its process.
        # Publish the new directory first and switch future launches atomically.
        with tempfile.TemporaryDirectory(prefix="download-", dir=state) as directory:
            stage = Path(directory)
            extracted = stage / "app"
            extracted.mkdir()
            extract_update(data, extracted)
            destination = state / version
            if destination.exists():
                shutil.rmtree(destination)
            extracted.rename(destination)
            next_pointer = stage / ACTIVE_VERSION
            next_pointer.write_text(version + "\n", encoding="utf-8")
            next_pointer.replace(state / ACTIVE_VERSION)
        print(f"已更新到 {version}。重新打开 start.command 即可使用。")


def launch(root: Path, port: int) -> None:
    _, initial_version = channel(root)
    _, app = active_app(root, initial_version)
    server = app / "server.py"
    if not server.is_file():
        raise ValueError("当前版本文件不完整，请重新解压工作台。")
    environment = dict(os.environ, YAO_DESIGN_STUDIO_DATA_ROOT=str(root))
    os.execve(sys.executable, [sys.executable, "-B", str(server), "--port", str(port)], environment)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    package = commands.add_parser("package")
    package.add_argument("--repo", required=True)
    package.add_argument("--version", required=True)
    package.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    commands.add_parser("update")
    start = commands.add_parser("launch")
    start.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    try:
        if args.command == "package":
            build_release(ROOT, args.repo, args.version, args.output_dir)
        elif args.command == "update":
            update(ROOT)
        else:
            launch(ROOT, args.port)
    except HTTPError as error:
        print(f"更新失败，GitHub 返回 HTTP {error.code}。请检查仓库的公开 Release。", file=sys.stderr)
        return 1
    except (ValueError, OSError, URLError, zipfile.BadZipFile) as error:
        print(f"操作失败。{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
