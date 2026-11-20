#!/usr/bin/env python3
"""A localhost-only interface for TokenHub MiniMax H3."""

from __future__ import annotations

import argparse
import base64
import cgi
import getpass
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib import error as urlerror
from urllib import request as urlrequest


APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("YAO_DESIGN_STUDIO_DATA_ROOT") or APP_ROOT).resolve()
STATIC_ROOT = APP_ROOT / "static"
WORK_ROOT = DATA_ROOT / "work"
UPLOAD_ROOT = WORK_ROOT / "uploads"
OUTPUT_ROOT = DATA_ROOT / "outputs"
H3_CLIENT_SCRIPT = APP_ROOT / "vendor" / "tokenhub_h3_client.py"
TOKENHUB_KEYCHAIN_SERVICE = "TOKENHUB_API_KEY"
APIMART_KEYCHAIN_SERVICE = "APIMART_API_KEY"
APIMART_BASE_URL = "https://api.apimart.ai"
QALLM_KEYCHAIN_SERVICE = "com.openai.codex.mechlabs.seedance"
QALLM_BASE_URL = "https://qallm-api.mechlabs.cn/v1"
SEEDANCE_MODELS = {
    "seedance-2.0": {
        "id": "doubao-seedance-2-0",
        "label": "Seedance 2.0",
        "max_duration": 15,
        "resolutions": {"480p", "720p", "1080p", "4k"},
        "reference_limits": (9, 3, 3),
    },
    "seedance-2.5": {
        "id": "doubao-seedance-2-5",
        "label": "Seedance 2.5",
        "max_duration": 30,
        "resolutions": {"480p", "720p", "1080p"},
        "reference_limits": (30, 10, 10),
    },
}
SEEDANCE_TASKS_PATH = "/contents/generations/tasks"
MAX_UPLOAD_BYTES = 52 * 1024 * 1024
TASK_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,180}$")


def json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def keychain_settings(provider: str) -> tuple[str, str, str]:
    if provider == "qallm":
        return "QALLM_API_KEY", QALLM_KEYCHAIN_SERVICE, "QALLM_API_KEY"
    if provider == "tokenhub":
        environment_name, service = "TOKENHUB_API_KEY", TOKENHUB_KEYCHAIN_SERVICE
    elif provider == "apimart":
        environment_name, service = "APIMART_API_KEY", APIMART_KEYCHAIN_SERVICE
    else:
        raise ValueError("未知的密钥类型。")
    return environment_name, service, getpass.getuser()


def secret_for(provider: str) -> str:
    environment_name, service, account = keychain_settings(provider)
    value = os.environ.get(environment_name, "").strip()
    if value:
        return value
    if sys.platform != "darwin" or not shutil.which("security"):
        return ""
    command = ["security", "find-generic-password", "-a", account, "-s", service, "-w"]
    result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=15)
    return result.stdout.strip() if result.returncode == 0 else ""


def key_is_configured(provider: str) -> bool:
    return bool(secret_for(provider))


def save_key(provider: str, key: str) -> None:
    _, service, account = keychain_settings(provider)
    if sys.platform != "darwin" or not shutil.which("security"):
        raise ValueError("此本地工具当前只支持通过 macOS 钥匙串保存密钥。")
    if len(key.strip()) < 8:
        raise ValueError("密钥看起来过短，请检查后重试。")
    # `security -w` without a following value opens an interactive prompt.
    # This server runs without a controlling terminal, so it cannot answer that
    # prompt through stdin. Pass the value only to this short-lived system
    # process; it is never written to the project, browser storage, or logs.
    command = ["security", "add-generic-password", "-a", account, "-s", service, "-U", "-w", key]
    result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
    if result.returncode != 0:
        raise ValueError("未能写入 macOS 钥匙串。请确认登录钥匙串已解锁后重试。")


def field_value(form: cgi.FieldStorage, name: str) -> str:
    value = form.getfirst(name, "")
    return value.strip() if isinstance(value, str) else ""


def field_values(form: cgi.FieldStorage, name: str) -> list[str]:
    return [item.strip() for item in form.getlist(name) if isinstance(item, str) and item.strip()]


def saved_uploads(form: cgi.FieldStorage, field_name: str, target: Path) -> list[str]:
    values = form[field_name] if field_name in form else []
    if not isinstance(values, list):
        values = [values]
    saved: list[str] = []
    for item in values:
        if not getattr(item, "filename", None) or not getattr(item, "file", None):
            continue
        suffix = Path(item.filename).suffix.lower()
        if not suffix or len(suffix) > 10:
            raise ValueError("上传文件缺少可识别的扩展名。")
        destination = target / f"{uuid.uuid4().hex}{suffix}"
        total = 0
        with destination.open("wb") as handle:
            while True:
                chunk = item.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    destination.unlink(missing_ok=True)
                    raise ValueError("单个上传文件不能超过 52 MiB；请改用公开 URL。")
                handle.write(chunk)
        saved.append(str(destination.resolve()))
    return saved


def parse_json_output(stdout: str) -> dict[str, Any]:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": "H3 脚本返回了无法解析的响应。", "type": "local_error"}


def relative_file_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        path = Path(value).resolve()
        relative = path.relative_to(OUTPUT_ROOT.resolve())
    except (ValueError, OSError):
        return None
    return "/files/" + "/".join(relative.parts)


def run_skill(command: list[str], timeout: int = 180) -> tuple[int, dict[str, Any]]:
    result = subprocess.run(
        [sys.executable, str(H3_CLIENT_SCRIPT), *command],
        cwd=APP_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    payload = parse_json_output(result.stdout)
    for name in ("video_path", "metadata_path"):
        url = relative_file_url(payload.get(name))
        if url:
            payload[f"{name}_url"] = url
    return result.returncode, payload


def apimart_request(method: str, path: str, key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urlrequest.Request(
        APIMART_BASE_URL + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "MiniMax-H3-Studio/1.0",
        },
    )
    try:
        with urlrequest.urlopen(request, timeout=90) as response:
            raw = response.read()
    except urlerror.HTTPError as error:
        raw = error.read()
        try:
            details = json.loads(raw.decode("utf-8"))
            message = details.get("error", {}).get("message") if isinstance(details.get("error"), dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = None
        raise ValueError(message or f"APIMart 请求失败（HTTP {error.code}）。") from None
    except urlerror.URLError as error:
        raise ValueError(f"无法连接 APIMart：{error.reason}") from None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("APIMart 返回了无法解析的响应。") from None
    if not isinstance(parsed, dict):
        raise ValueError("APIMart 返回了异常响应。")
    if isinstance(parsed.get("error"), dict):
        raise ValueError(str(parsed["error"].get("message") or "APIMart 请求失败。"))
    return parsed


def seedance_api_request(method: str, path: str, key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urlrequest.Request(
        QALLM_BASE_URL + path,
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MiniMax-H3-Studio/1.0",
        },
    )
    request.add_unredirected_header("Authorization", f"Bearer {key}")
    try:
        with urlrequest.urlopen(request, timeout=90) as response:
            raw = response.read()
    except urlerror.HTTPError as error:
        raw = error.read()
        try:
            details = json.loads(raw.decode("utf-8"))
            error_info = details.get("error") if isinstance(details, dict) else None
            message = error_info.get("message") if isinstance(error_info, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = None
        message = str(message or f"要务网关请求失败（HTTP {error.code}）。").replace(key, "[已隐藏]")
        request_id = (error.headers or {}).get("x-request-id")
        if request_id:
            message += f"（请求 ID {request_id}）"
        raise ValueError(message) from None
    except urlerror.URLError as error:
        raise ValueError(f"无法连接要务网关：{error.reason}") from None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("要务网关返回了无法解析的响应。") from None
    if not isinstance(parsed, dict):
        raise ValueError("要务网关返回了异常响应。")
    if isinstance(parsed.get("error"), dict) and str(parsed.get("status", "")).lower() not in {"failed", "cancelled", "expired"}:
        message = str(parsed["error"].get("message") or "要务网关请求失败。")
        raise ValueError(message.replace(key, "[已隐藏]"))
    return parsed


def image_data_url(path: str) -> str:
    source = Path(path).resolve()
    mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    if not mime_type.startswith("image/"):
        raise ValueError("生成图片只支持图片参考。")
    try:
        content = source.read_bytes()
    except OSError:
        raise ValueError("参考图片无法读取。") from None
    return f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"


def safe_output_stem(task_id: str) -> str:
    stem = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in task_id)
    return stem[:180] or "apimart-image"


def apimart_task_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("APIMart 任务响应缺少 data。")
    return data


def collect_image_urls(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return [value]
    if isinstance(value, list):
        for item in value:
            urls.extend(collect_image_urls(item))
    elif isinstance(value, dict):
        for key in ("url", "image_urls", "images"):
            if key in value:
                urls.extend(collect_image_urls(value[key]))
    return list(dict.fromkeys(urls))


def download_apimart_images(task_id: str, task: dict[str, Any]) -> tuple[list[str], str]:
    urls = collect_image_urls(task.get("result"))
    if not urls:
        urls = collect_image_urls(task)
    if not urls:
        raise ValueError("图片任务已完成，但未返回图片地址。")
    local_urls: list[str] = []
    for index, url in enumerate(urls, start=1):
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            suffix = ".png"
        destination = OUTPUT_ROOT / f"{safe_output_stem(task_id)}-{index}{suffix}"
        temporary = destination.with_suffix(destination.suffix + ".part")
        request = urlrequest.Request(url, headers={"User-Agent": "MiniMax-H3-Studio/1.0"})
        try:
            with urlrequest.urlopen(request, timeout=120) as response, temporary.open("wb") as handle:
                shutil.copyfileobj(response, handle, length=1024 * 1024)
        except (urlerror.URLError, OSError) as error:
            temporary.unlink(missing_ok=True)
            raise ValueError(f"图片下载失败：{error}") from None
        temporary.replace(destination)
        local_url = relative_file_url(str(destination))
        if local_url:
            local_urls.append(local_url)
    metadata_path = OUTPUT_ROOT / f"{safe_output_stem(task_id)}.json"
    metadata_path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return local_urls, relative_file_url(str(metadata_path)) or ""


def download_seedance_video(task_id: str, task: dict[str, Any]) -> str:
    content = task.get("content")
    video_url = content.get("video_url") if isinstance(content, dict) else None
    if not isinstance(video_url, str) or not video_url.startswith(("http://", "https://")):
        raise ValueError("Seedance 任务已完成，但未返回视频下载地址。")
    destination = OUTPUT_ROOT / f"seedance-{safe_output_stem(task_id)}.mp4"
    temporary = destination.with_suffix(".mp4.part")
    request = urlrequest.Request(video_url, headers={"User-Agent": "MiniMax-H3-Studio/1.0"})
    try:
        with urlrequest.urlopen(request, timeout=180) as response, temporary.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
    except (urlerror.URLError, OSError) as error:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"Seedance 视频下载失败：{error}") from None
    temporary.replace(destination)
    return relative_file_url(str(destination)) or ""


class AppHandler(BaseHTTPRequestHandler):
    server_version = "MiniMaxH3Studio/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        """Avoid writing requests or form values (including passwords) to logs."""

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        try:
            content = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if path in {"/", "/index.html"}:
            self.send_file(STATIC_ROOT / "index.html")
            return
        if path.startswith("/figma-assets/"):
            try:
                relative = Path(path.removeprefix("/figma-assets/"))
                candidate = (STATIC_ROOT / "figma-assets" / relative).resolve()
                candidate.relative_to((STATIC_ROOT / "figma-assets").resolve())
                if candidate.suffix.lower() != ".svg":
                    raise ValueError
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_file(candidate)
            return
        if path == "/api/status":
            tokenhub_ready = key_is_configured("tokenhub")
            apimart_ready = key_is_configured("apimart")
            qallm_ready = key_is_configured("qallm")
            self.send_json(HTTPStatus.OK, {
                "key_configured": tokenhub_ready or apimart_ready or qallm_ready,
                "tokenhub_configured": tokenhub_ready,
                "apimart_configured": apimart_ready,
                "qallm_configured": qallm_ready,
            })
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if path.startswith("/files/"):
            try:
                relative = Path(path.removeprefix("/files/"))
                candidate = (OUTPUT_ROOT / relative).resolve()
                candidate.relative_to(OUTPUT_ROOT.resolve())
                if candidate.suffix.lower() not in {".mp4", ".json", ".jpg", ".jpeg", ".png", ".webp"}:
                    raise ValueError
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_file(candidate)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def parse_form(self) -> cgi.FieldStorage:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("请求格式错误。")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 64 * 1024 * 1024:
            raise ValueError("请求过大或为空。")
        body = self.rfile.read(length)
        return cgi.FieldStorage(
            fp=io.BytesIO(body),
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type, "CONTENT_LENGTH": str(length)},
            keep_blank_values=True,
        )

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            form = self.parse_form()
            if path == "/api/key":
                provider = field_value(form, "provider") or "tokenhub"
                save_key(provider, field_value(form, "key"))
                self.send_json(HTTPStatus.OK, {"ok": True, "provider": provider, "key_configured": True})
                return
            if path == "/api/dry-run":
                if field_value(form, "video_model") in SEEDANCE_MODELS:
                    payload = self.seedance_request_payload(form)
                    summary = {name: payload[name] for name in ("model", "resolution", "duration", "ratio")}
                    summary["mode"] = field_value(form, "mode") or "text"
                    self.send_json(HTTPStatus.OK, {"dry_run": True, "request": summary})
                    return
                command = self.generation_command(form, dry_run=True)
                _, payload = run_skill(command)
                self.send_json(HTTPStatus.OK, payload)
                return
            if path == "/api/generate":
                if field_value(form, "video_model") in SEEDANCE_MODELS:
                    key = secret_for("qallm")
                    if not key:
                        self.send_json(HTTPStatus.PRECONDITION_REQUIRED, {"error": "请先配置要务测试网关 QALLM_API_KEY。", "type": "missing_key"})
                        return
                    self.send_json(HTTPStatus.OK, self.seedance_generation(form, key))
                    return
                if not key_is_configured("tokenhub"):
                    self.send_json(HTTPStatus.PRECONDITION_REQUIRED, {"error": "请先在本机钥匙串中配置 TokenHub API Key。", "type": "missing_key"})
                    return
                command = self.generation_command(form, dry_run=False)
                _, payload = run_skill(command)
                status = HTTPStatus.OK if not payload.get("error") else HTTPStatus.BAD_REQUEST
                self.send_json(status, payload)
                return
            if path == "/api/seedance/task":
                task_id = field_value(form, "task_id")
                if not TASK_ID_RE.fullmatch(task_id):
                    raise ValueError("任务 ID 格式不正确。")
                key = secret_for("qallm")
                if not key:
                    self.send_json(HTTPStatus.PRECONDITION_REQUIRED, {"error": "请先配置要务测试网关 QALLM_API_KEY。", "type": "missing_key"})
                    return
                task = seedance_api_request("GET", f"{SEEDANCE_TASKS_PATH}/{task_id}", key)
                status_name = str(task.get("status", "unknown")).lower()
                response: dict[str, Any] = {"task_id": task.get("id") or task_id, "status": status_name}
                if status_name in {"failed", "cancelled", "expired"}:
                    error_info = task.get("error")
                    if isinstance(error_info, dict):
                        response["error_code"] = str(error_info.get("code") or "")
                        response["error_message"] = str(error_info.get("message") or "").replace(key, "[已隐藏]")
                if status_name == "succeeded" and field_value(form, "download") == "true":
                    response["video_path_url"] = download_seedance_video(task_id, task)
                self.send_json(HTTPStatus.OK, response)
                return
            if path == "/api/images/generate":
                if not key_is_configured("apimart"):
                    self.send_json(HTTPStatus.PRECONDITION_REQUIRED, {"error": "请先在本机钥匙串中配置 APIMart API Key。", "type": "missing_key"})
                    return
                payload = self.image_generation(form)
                self.send_json(HTTPStatus.OK, payload)
                return
            if path == "/api/images/task":
                task_id = field_value(form, "task_id")
                if not TASK_ID_RE.fullmatch(task_id):
                    raise ValueError("任务 ID 格式不正确。")
                if not key_is_configured("apimart"):
                    self.send_json(HTTPStatus.PRECONDITION_REQUIRED, {"error": "请先在本机钥匙串中配置 APIMart API Key。", "type": "missing_key"})
                    return
                task_response = apimart_request("GET", f"/v1/tasks/{task_id}", secret_for("apimart"))
                task = apimart_task_data(task_response)
                status_name = str(task.get("status", "unknown")).lower()
                response: dict[str, Any] = {
                    "task_id": task.get("id") or task_id,
                    "status": status_name,
                    "progress": task.get("progress"),
                }
                if status_name == "completed" and field_value(form, "download") == "true":
                    image_urls, metadata_url = download_apimart_images(task_id, task)
                    response.update({"image_paths": image_urls, "metadata_path_url": metadata_url})
                self.send_json(HTTPStatus.OK, response)
                return
            if path == "/api/task":
                task_id = field_value(form, "task_id")
                if not TASK_ID_RE.fullmatch(task_id):
                    raise ValueError("任务 ID 格式不正确。")
                command = ["task", task_id, "--output-dir", str(OUTPUT_ROOT)]
                if field_value(form, "download") == "true":
                    command.append("--download")
                _, payload = run_skill(command)
                status = HTTPStatus.OK if not payload.get("error") else HTTPStatus.BAD_REQUEST
                self.send_json(status, payload)
                return
            if path == "/api/reveal":
                file_url = field_value(form, "file_url")
                if not file_url.startswith("/files/"):
                    raise ValueError("只能在 Finder 中查看本工具生成的视频。")
                relative = Path(file_url.removeprefix("/files/"))
                candidate = (OUTPUT_ROOT / relative).resolve()
                candidate.relative_to(OUTPUT_ROOT.resolve())
                if candidate.suffix.lower() not in {".mp4", ".jpg", ".jpeg", ".png", ".webp"} or not candidate.is_file():
                    raise ValueError("生成文件不存在或无法在 Finder 中显示。")
                if sys.platform != "darwin":
                    raise ValueError("前往查看仅支持 macOS Finder。")
                subprocess.run(["open", "-R", str(candidate)], check=True, timeout=15)
                self.send_json(HTTPStatus.OK, {"ok": True})
                return
            if path == "/api/open-output-folder":
                if sys.platform != "darwin":
                    raise ValueError("前往视频文件夹仅支持 macOS Finder。")
                subprocess.run(["open", str(OUTPUT_ROOT)], check=True, timeout=15)
                self.send_json(HTTPStatus.OK, {"ok": True})
                return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "未知接口。"})
        except subprocess.TimeoutExpired:
            self.send_json(HTTPStatus.GATEWAY_TIMEOUT, {"error": "本地 H3 脚本超时，请稍后通过任务 ID 查询。"})
        except (ValueError, OSError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error), "type": "validation_error"})

    def generation_command(self, form: cgi.FieldStorage, *, dry_run: bool) -> list[str]:
        prompt = field_value(form, "prompt")
        if not prompt:
            raise ValueError("请填写视频提示词。")
        selected_mode = field_value(form, "mode") or "text"
        if selected_mode not in {"text", "frames", "reference"}:
            raise ValueError("生成模式不正确。")
        job_uploads = UPLOAD_ROOT / uuid.uuid4().hex
        job_uploads.mkdir(parents=True, exist_ok=True)
        first_frame = field_value(form, "first_frame_url")
        last_frame = field_value(form, "last_frame_url")
        first_files = saved_uploads(form, "first_frame_file", job_uploads)
        last_files = saved_uploads(form, "last_frame_file", job_uploads)
        if len(first_files) > 1 or len(last_files) > 1:
            raise ValueError("首帧和尾帧各只能上传一张图片。")
        first_frame = first_files[0] if first_files else first_frame
        last_frame = last_files[0] if last_files else last_frame
        if selected_mode == "frames" and not (first_frame or last_frame):
            raise ValueError("首尾帧模式至少需要一张首帧或尾帧图片。")
        command = [
            "generate", "--prompt", prompt,
            "--resolution", field_value(form, "resolution") or "768P",
            "--duration", field_value(form, "duration") or "4",
            "--ratio", field_value(form, "ratio") or "16:9",
            "--output-dir", str(OUTPUT_ROOT),
        ]
        if first_frame:
            command += ["--first-frame", first_frame]
        if last_frame:
            command += ["--last-frame", last_frame]
        reference_images = field_values(form, "reference_image_urls") + saved_uploads(form, "reference_image_files", job_uploads)
        reference_videos = field_values(form, "reference_video_urls") + saved_uploads(form, "reference_video_files", job_uploads)
        reference_audios = field_values(form, "reference_audio_urls") + saved_uploads(form, "reference_audio_files", job_uploads)
        if selected_mode == "reference" and not (reference_images or reference_videos or reference_audios):
            raise ValueError("多模态参考模式至少需要一个参考素材。")
        for value in reference_images:
            command += ["--reference-image", value]
        for value in reference_videos:
            command += ["--reference-video", value]
        for value in reference_audios:
            command += ["--reference-audio", value]
        command.append("--dry-run" if dry_run else "--no-wait")
        return command

    def seedance_request_payload(self, form: cgi.FieldStorage) -> dict[str, Any]:
        model = field_value(form, "video_model")
        profile = SEEDANCE_MODELS[model]
        label = profile["label"]
        is_seedance_20 = model == "seedance-2.0"
        prompt = field_value(form, "prompt")
        if not prompt:
            raise ValueError("请填写视频提示词。")
        selected_mode = field_value(form, "mode") or "text"
        if selected_mode not in {"text", "frames", "reference"}:
            raise ValueError("生成模式不正确。")
        resolution = field_value(form, "resolution") or "720p"
        if is_seedance_20:
            resolution = resolution.lower()
        if resolution not in profile["resolutions"]:
            allowed_resolutions = "480p、720p、1080p 或 4K" if is_seedance_20 else "480p、720p 或 1080p"
            raise ValueError(f"{label} 仅支持 {allowed_resolutions}。")
        try:
            duration = int(field_value(form, "duration") or "5")
        except ValueError:
            raise ValueError("视频时长不正确。") from None
        max_duration = profile["max_duration"]
        if not (4 <= duration <= max_duration or (is_seedance_20 and duration == -1)):
            allowed_duration = "4–15 秒或自动（-1）" if is_seedance_20 else "4–30 秒"
            raise ValueError(f"{label} 视频时长须为 {allowed_duration}。")
        ratio = field_value(form, "ratio") or ("adaptive" if is_seedance_20 else "9:16")
        ratios = {"16:9", "9:16", "3:4", "4:3", "1:1", "21:9"}
        if is_seedance_20:
            ratios.add("adaptive")
        if selected_mode == "frames":
            ratio = "adaptive"
        elif ratio not in ratios:
            raise ValueError(f"{label} 请为非首尾帧模式选择支持的视频比例。")

        job_uploads = UPLOAD_ROOT / uuid.uuid4().hex
        job_uploads.mkdir(parents=True, exist_ok=True)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]

        if selected_mode == "frames":
            first_files = saved_uploads(form, "first_frame_file", job_uploads)
            last_files = saved_uploads(form, "last_frame_file", job_uploads)
            first_url = field_value(form, "first_frame_url")
            last_url = field_value(form, "last_frame_url")
            if len(first_files) > 1 or len(last_files) > 1:
                raise ValueError("首帧和尾帧各只能上传一张图片。")
            first_source = image_data_url(first_files[0]) if first_files else first_url
            last_source = image_data_url(last_files[0]) if last_files else last_url
            if not (first_source or last_source):
                raise ValueError("首尾帧模式至少需要一张首帧或尾帧图片。")
            if is_seedance_20 and not first_source:
                raise ValueError("Seedance 2.0 使用尾帧时需同时提供首帧。")
            if first_source:
                content.append({"type": "image_url", "image_url": {"url": first_source}, "role": "first_frame"})
            if last_source:
                content.append({"type": "image_url", "image_url": {"url": last_source}, "role": "last_frame"})
        elif selected_mode == "reference":
            image_files = saved_uploads(form, "reference_image_files", job_uploads)
            image_urls = field_values(form, "reference_image_urls") + [image_data_url(path) for path in image_files]
            video_urls = field_values(form, "reference_video_urls")
            audio_urls = field_values(form, "reference_audio_urls")
            for field_name in ("reference_video_files", "reference_audio_files"):
                values = form[field_name] if field_name in form else []
                if not isinstance(values, list):
                    values = [values]
                if any(getattr(item, "filename", None) for item in values):
                    raise ValueError(f"{label} 的视频和音频参考请使用公开 URL 或网关可访问的 asset:// 素材。")
            if not (image_urls or video_urls or audio_urls):
                raise ValueError("智能参考模式至少需要一个参考素材。")
            max_images, max_videos, max_audios = profile["reference_limits"]
            if len(image_urls) > max_images or len(video_urls) > max_videos or len(audio_urls) > max_audios:
                raise ValueError(f"{label} 的参考素材上限为 {max_images} 张图片、{max_videos} 个视频、{max_audios} 个音频。")
            for source in image_urls:
                content.append({"type": "image_url", "image_url": {"url": source}, "role": "reference_image"})
            for source in video_urls:
                content.append({"type": "video_url", "video_url": {"url": source}, "role": "reference_video"})
            for source in audio_urls:
                content.append({"type": "audio_url", "audio_url": {"url": source}, "role": "reference_audio"})

        request_payload = {
            "model": profile["id"],
            "content": content,
            "resolution": resolution,
            "generate_audio": True,
            "ratio": ratio,
            "duration": duration,
            "watermark": False,
            "output_format": "mp4",
        }
        if selected_mode == "reference" and not is_seedance_20:
            request_payload["omni_reference_task_type"] = "reference"
        return request_payload

    def seedance_generation(self, form: cgi.FieldStorage, key: str) -> dict[str, Any]:
        response = seedance_api_request("POST", SEEDANCE_TASKS_PATH, key, self.seedance_request_payload(form))
        task_id = response.get("id")
        if not task_id:
            raise ValueError("要务网关提交成功响应中缺少任务 ID。")
        return {"task_id": str(task_id), "status": str(response.get("status") or "queued").lower()}

    def image_generation(self, form: cgi.FieldStorage) -> dict[str, Any]:
        prompt = field_value(form, "prompt")
        if not prompt:
            raise ValueError("请填写图片提示词。")
        version = field_value(form, "version") or "8.2"
        if version not in {"8.2", "8.1", "7", "6.1", "5.2", "5.1"}:
            raise ValueError("Midjourney 模型版本不正确。")
        size = field_value(form, "size") or "9:16"
        if size not in {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}:
            raise ValueError("图片比例不正确。")
        speed = field_value(form, "speed") or "relax"
        if speed not in {"relax", "fast", "turbo"}:
            raise ValueError("生成速度不正确。")
        job_uploads = UPLOAD_ROOT / uuid.uuid4().hex
        job_uploads.mkdir(parents=True, exist_ok=True)
        reference_files = saved_uploads(form, "reference_image_files", job_uploads)
        payload: dict[str, Any] = {
            "prompt": prompt,
            "version": version,
            "size": size,
            "speed": speed,
            "metadata": {"source": "minimax-h3-local-studio"},
        }
        if reference_files:
            payload["image_urls"] = [image_data_url(path) for path in reference_files]
        response = apimart_request("POST", "/v1/midjourney/generations", secret_for("apimart"), payload)
        data = response.get("data")
        first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        task_id = first.get("task_id")
        if not task_id:
            raise ValueError("APIMart 提交成功响应中缺少任务 ID。")
        return {"task_id": str(task_id), "status": str(first.get("status") or "submitted").lower()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    if not H3_CLIENT_SCRIPT.is_file():
        raise SystemExit(f"Bundled TokenHub H3 client was not found: {H3_CLIENT_SCRIPT}")
    for directory in (WORK_ROOT, UPLOAD_ROOT, OUTPUT_ROOT):
        directory.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), AppHandler)
    print(f"YAO-design-studio is ready at http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
