#!/usr/bin/env python3
"""Generate and download MiniMax H3 videos through Tencent Cloud TokenHub."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import mimetypes
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


API_BASE = "https://tokenhub.tencentmaas.com"
SUBMIT_PATH = "/v1/wand/minimax-video-v2/generation"
TASK_PATH = "/v1/wand/minimax-video-v2/tasks/{task_id}"
MODEL = "minimax-video-h3"
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_VIDEO_BYTES = 50 * 1024 * 1024
MAX_AUDIO_BYTES = 15 * 1024 * 1024
SUPPORTED_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
RATIO_VALUES = {
    "21:9": 21 / 9,
    "16:9": 16 / 9,
    "4:3": 4 / 3,
    "1:1": 1.0,
    "3:4": 3 / 4,
    "9:16": 9 / 16,
}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
VIDEO_SUFFIXES = {".mp4", ".mov"}
AUDIO_SUFFIXES = {".wav", ".mp3"}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "success", "fail"}
RUNNING_STATUSES = {"queued", "running", "preparing", "queueing", "processing"}
KEYCHAIN_SERVICE = "TOKENHUB_API_KEY"


class SkillError(RuntimeError):
    """User-facing validation or API error."""


class ApiError(SkillError):
    def __init__(self, message: str, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class MediaValue:
    wire_value: str
    summary: Dict[str, Any]
    local_path: Optional[Path] = None
    dimensions: Optional[Tuple[int, int]] = None
    duration: Optional[float] = None


def json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def stderr(message: str) -> None:
    print(message, file=sys.stderr)


def keychain_api_key() -> Optional[str]:
    """Read the TokenHub key from the current user's macOS Keychain without logging it."""
    if sys.platform != "darwin":
        return None
    security = shutil.which("security")
    if not security:
        return None
    account = os.environ.get("USER", "").strip() or getpass.getuser()
    try:
        completed = subprocess.run(
            [security, "find-generic-password", "-a", account, "-s", KEYCHAIN_SERVICE, "-w"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    api_key = completed.stdout.strip()
    return api_key or None


def require_api_key() -> str:
    api_key = os.environ.get("TOKENHUB_API_KEY", "").strip()
    if api_key:
        return api_key
    api_key = keychain_api_key()
    if api_key:
        return api_key
    raise SkillError(
        "TokenHub API key was not found in TOKENHUB_API_KEY or macOS Keychain. "
        "For first-time macOS setup, run: "
        'security add-generic-password -a "$USER" -s "TOKENHUB_API_KEY" -U -w '
        "and paste the key at the hidden prompt, then retry. "
        "On non-macOS systems, set TOKENHUB_API_KEY in the process environment."
    )


def is_remote_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_data_url(value: str) -> bool:
    return value.startswith("data:") and ";base64," in value[:128]


def data_url_mime(value: str) -> str:
    return value[5 : value.index(";")].lower()


def data_url_decoded_size(value: str) -> int:
    encoded = value.split(",", 1)[1]
    padding = encoded[-2:].count("=") if encoded else 0
    return max(0, (len(encoded) * 3) // 4 - padding)


def mime_for_path(path: Path, category: str) -> str:
    suffix = path.suffix.lower()
    known = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
    }
    mime = known.get(suffix) or mimetypes.guess_type(path.name)[0]
    if not mime or not mime.startswith(category + "/"):
        raise SkillError(f"Cannot determine a supported {category} MIME type for {path}")
    return mime


def jpeg_dimensions(path: Path) -> Tuple[int, int]:
    with path.open("rb") as handle:
        if handle.read(2) != b"\xff\xd8":
            raise SkillError(f"Invalid JPEG file: {path}")
        while True:
            byte = handle.read(1)
            if not byte:
                break
            if byte != b"\xff":
                continue
            marker = handle.read(1)
            while marker == b"\xff":
                marker = handle.read(1)
            if not marker or marker in {b"\xd8", b"\xd9"}:
                continue
            raw_length = handle.read(2)
            if len(raw_length) != 2:
                break
            length = struct.unpack(">H", raw_length)[0]
            marker_value = marker[0]
            if marker_value in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                payload = handle.read(5)
                if len(payload) != 5:
                    break
                height, width = struct.unpack(">HH", payload[1:5])
                return width, height
            handle.seek(max(0, length - 2), 1)
    raise SkillError(f"Could not read JPEG dimensions: {path}")


def webp_dimensions(path: Path) -> Tuple[int, int]:
    data = path.read_bytes()
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise SkillError(f"Invalid WebP file: {path}")
    offset = 12
    while offset + 8 <= len(data):
        kind = data[offset : offset + 4]
        size = struct.unpack("<I", data[offset + 4 : offset + 8])[0]
        chunk = data[offset + 8 : offset + 8 + size]
        if kind == b"VP8X" and len(chunk) >= 10:
            width = 1 + int.from_bytes(chunk[4:7], "little")
            height = 1 + int.from_bytes(chunk[7:10], "little")
            return width, height
        if kind == b"VP8 " and len(chunk) >= 10 and chunk[3:6] == b"\x9d\x01\x2a":
            width = int.from_bytes(chunk[6:8], "little") & 0x3FFF
            height = int.from_bytes(chunk[8:10], "little") & 0x3FFF
            return width, height
        if kind == b"VP8L" and len(chunk) >= 5 and chunk[0] == 0x2F:
            bits = int.from_bytes(chunk[1:5], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return width, height
        offset += 8 + size + (size % 2)
    raise SkillError(f"Could not read WebP dimensions: {path}")


def sips_dimensions(path: Path) -> Optional[Tuple[int, int]]:
    if not shutil.which("sips"):
        return None
    try:
        result = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    width = height = None
    for line in result.stdout.splitlines():
        key, separator, raw_value = line.strip().partition(":")
        if not separator:
            continue
        if key == "pixelWidth":
            width = int(raw_value.strip())
        elif key == "pixelHeight":
            height = int(raw_value.strip())
    return (width, height) if width and height else None


def image_dimensions(path: Path) -> Tuple[int, int]:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return jpeg_dimensions(path)
    if suffix == ".png":
        with path.open("rb") as handle:
            header = handle.read(24)
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            raise SkillError(f"Invalid PNG file: {path}")
        return struct.unpack(">II", header[16:24])
    if suffix == ".webp":
        return webp_dimensions(path)
    dimensions = sips_dimensions(path)
    if dimensions:
        return dimensions
    raise SkillError(f"Cannot validate dimensions for {path}; use a public URL or provide JPG/PNG/WebP")


def validate_dimensions(path: Path, dimensions: Tuple[int, int]) -> None:
    width, height = dimensions
    if not (256 <= width <= 5760 and 256 <= height <= 5760):
        raise SkillError(f"Image dimensions must each be 256-5760 px: {path} is {width}x{height}")
    ratio = width / height
    if not (0.4 <= ratio <= 2.5):
        raise SkillError(f"Image aspect ratio must be 0.4-2.5: {path} is {ratio:.3f}")


def ffprobe_info(path: Path) -> Optional[Dict[str, Any]]:
    executable = shutil.which("ffprobe")
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def parse_frame_rate(value: str) -> Optional[float]:
    try:
        numerator, denominator = value.split("/", 1)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else None
    except (AttributeError, ValueError, ZeroDivisionError):
        return None


def validate_video_probe(path: Path, info: Dict[str, Any]) -> Optional[float]:
    video_streams = [stream for stream in info.get("streams", []) if stream.get("codec_type") == "video"]
    if not video_streams:
        raise SkillError(f"Reference video has no video stream: {path}")
    stream = video_streams[0]
    codec = str(stream.get("codec_name", "")).lower()
    if codec and codec not in {"h264", "hevc"}:
        raise SkillError(f"Reference video codec must be H.264 or H.265/HEVC: {path} uses {codec}")
    width, height = stream.get("width"), stream.get("height")
    if width and height:
        validate_dimensions(path, (int(width), int(height)))
    frame_rate = parse_frame_rate(str(stream.get("r_frame_rate", "")))
    if frame_rate and not (23.976 <= frame_rate <= 60.0):
        raise SkillError(f"Reference video frame rate must be 23.976-60 fps: {path} is {frame_rate:.3f}")
    raw_duration = info.get("format", {}).get("duration")
    if raw_duration is None:
        return None
    duration = float(raw_duration)
    if not (2 <= duration <= 15):
        raise SkillError(f"Reference video duration must be 2-15 seconds: {path} is {duration:.3f}s")
    return duration


def local_audio_duration(path: Path) -> Optional[float]:
    info = ffprobe_info(path)
    if info and info.get("format", {}).get("duration") is not None:
        duration = float(info["format"]["duration"])
    elif path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as handle:
                duration = handle.getnframes() / float(handle.getframerate())
        except (wave.Error, OSError, ZeroDivisionError):
            return None
    else:
        return None
    if not (2 <= duration <= 15):
        raise SkillError(f"Reference audio duration must be 2-15 seconds: {path} is {duration:.3f}s")
    return duration


def validate_local_media(path: Path, category: str) -> Tuple[str, Optional[Tuple[int, int]], Optional[float]]:
    if not path.is_file():
        raise SkillError(f"Local media file does not exist: {path}")
    suffix = path.suffix.lower()
    supported = {"image": IMAGE_SUFFIXES, "video": VIDEO_SUFFIXES, "audio": AUDIO_SUFFIXES}[category]
    if suffix not in supported:
        raise SkillError(f"Unsupported {category} format for {path}; allowed: {', '.join(sorted(supported))}")
    size = path.stat().st_size
    limit = {"image": MAX_IMAGE_BYTES, "video": MAX_VIDEO_BYTES, "audio": MAX_AUDIO_BYTES}[category]
    if size > limit:
        raise SkillError(f"{category.title()} exceeds {limit // (1024 * 1024)} MiB: {path}")
    dimensions: Optional[Tuple[int, int]] = None
    duration: Optional[float] = None
    if category == "image":
        dimensions = image_dimensions(path)
        validate_dimensions(path, dimensions)
    elif category == "video":
        info = ffprobe_info(path)
        if info:
            duration = validate_video_probe(path, info)
        else:
            stderr(f"Warning: ffprobe is unavailable; TokenHub will validate duration, codec, dimensions, and frame rate for {path}")
    else:
        duration = local_audio_duration(path)
        if duration is None:
            stderr(f"Warning: local audio duration could not be read; TokenHub will validate it for {path}")
    return mime_for_path(path, category), dimensions, duration


def prepare_media(value: str, category: str) -> MediaValue:
    if is_remote_url(value):
        return MediaValue(value, {"source": "url", "url": value, "category": category})
    if is_data_url(value):
        mime = data_url_mime(value)
        if not mime.startswith(category + "/"):
            raise SkillError(f"Expected a {category} Data URL, got {mime}")
        size = data_url_decoded_size(value)
        limit = {"image": MAX_IMAGE_BYTES, "video": MAX_VIDEO_BYTES, "audio": MAX_AUDIO_BYTES}[category]
        if size > limit:
            raise SkillError(f"{category.title()} Data URL exceeds {limit // (1024 * 1024)} MiB")
        return MediaValue(value, {"source": "data_url", "mime": mime, "bytes": size, "category": category})
    path = Path(value).expanduser().resolve()
    mime, dimensions, duration = validate_local_media(path, category)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    summary: Dict[str, Any] = {
        "source": "local",
        "path": str(path),
        "mime": mime,
        "bytes": path.stat().st_size,
        "category": category,
    }
    if dimensions:
        summary["width"], summary["height"] = dimensions
    if duration is not None:
        summary["duration"] = round(duration, 3)
    return MediaValue(f"data:{mime};base64,{encoded}", summary, path, dimensions, duration)


def closest_ratio(dimensions: Tuple[int, int]) -> str:
    actual = dimensions[0] / dimensions[1]
    return min(SUPPORTED_RATIOS, key=lambda ratio: abs(RATIO_VALUES[ratio] - actual))


def validate_generation_options(args: argparse.Namespace) -> str:
    if not args.prompt or not args.prompt.strip():
        raise SkillError("--prompt must be non-empty")
    if args.resolution not in {"768P", "2K"}:
        raise SkillError("--resolution must be 768P or 2K")
    if not (4 <= args.duration <= 15):
        raise SkillError("--duration must be an integer from 4 through 15")
    has_frames = bool(args.first_frame or args.last_frame)
    has_references = bool(args.reference_image or args.reference_video or args.reference_audio)
    if has_frames and has_references:
        raise SkillError("First/last frames cannot be mixed with reference media")
    if has_references:
        if len(args.reference_image) > 9:
            raise SkillError("At most 9 reference images are allowed")
        if len(args.reference_video) > 3:
            raise SkillError("At most 3 reference videos are allowed")
        if len(args.reference_audio) > 3:
            raise SkillError("At most 3 reference audio files are allowed")
        if len(args.reference_image) + len(args.reference_video) + len(args.reference_audio) > 12:
            raise SkillError("At most 12 reference assets are allowed in total")
        if args.reference_audio and not (args.reference_image or args.reference_video):
            raise SkillError("Reference audio requires at least one reference image or video")
        return "reference"
    if has_frames:
        return "frames"
    return "text"


def content_item(media_type: str, role: str, media: MediaValue) -> Dict[str, Any]:
    return {"type": media_type, media_type: {"url": media.wire_value}, "role": role}


def build_generation_payload(args: argparse.Namespace) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    mode = validate_generation_options(args)
    prompt = args.prompt.strip()
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    media_summaries: List[Dict[str, Any]] = []
    first_reference_dimensions: Optional[Tuple[int, int]] = None

    def append_media(value: str, category: str, role: str) -> None:
        nonlocal first_reference_dimensions
        media = prepare_media(value, category)
        content.append(content_item(f"{category}_url", role, media))
        summary = dict(media.summary)
        summary["role"] = role
        media_summaries.append(summary)
        if role == "reference_image" and first_reference_dimensions is None and media.dimensions:
            first_reference_dimensions = media.dimensions

    if args.first_frame:
        append_media(args.first_frame, "image", "first_frame")
    if args.last_frame:
        append_media(args.last_frame, "image", "last_frame")
    for value in args.reference_image:
        append_media(value, "image", "reference_image")
    for value in args.reference_video:
        append_media(value, "video", "reference_video")
    for value in args.reference_audio:
        append_media(value, "audio", "reference_audio")

    if mode == "text":
        ratio = args.ratio or "16:9"
        if ratio == "adaptive" or ratio not in SUPPORTED_RATIOS:
            raise SkillError(f"Text mode ratio must be one of: {', '.join(SUPPORTED_RATIOS)}")
    elif mode == "frames":
        ratio = "adaptive"
        if args.ratio and args.ratio != "adaptive":
            stderr("Note: frame mode ignores --ratio and uses adaptive")
    else:
        if args.ratio:
            if args.ratio != "adaptive" and args.ratio not in SUPPORTED_RATIOS:
                raise SkillError(f"Reference mode ratio must be adaptive or one of: {', '.join(SUPPORTED_RATIOS)}")
            ratio = args.ratio
        elif first_reference_dimensions:
            ratio = closest_ratio(first_reference_dimensions)
        else:
            ratio = "16:9"

    video_durations = [item.get("duration") for item in media_summaries if item["category"] == "video" and item.get("duration") is not None]
    audio_durations = [item.get("duration") for item in media_summaries if item["category"] == "audio" and item.get("duration") is not None]
    if video_durations and sum(video_durations) > 15:
        raise SkillError(f"Known reference video durations total {sum(video_durations):.3f}s; maximum is 15s")
    if audio_durations and sum(audio_durations) > 15:
        raise SkillError(f"Known reference audio durations total {sum(audio_durations):.3f}s; maximum is 15s")

    payload = {
        "model": MODEL,
        "content": content,
        "resolution": args.resolution,
        "duration": args.duration,
        "ratio": ratio,
    }
    request_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if request_size > MAX_REQUEST_BYTES:
        raise SkillError(
            f"JSON request is {request_size / (1024 * 1024):.2f} MiB; maximum is 64 MiB. Use public URLs for large media."
        )
    sanitized = {
        "model": MODEL,
        "mode": mode,
        "prompt": prompt,
        "resolution": args.resolution,
        "duration": args.duration,
        "ratio": ratio,
        "media": media_summaries,
        "request_bytes": request_size,
    }
    return payload, sanitized


def api_request(method: str, path: str, api_key: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = API_BASE + path
    body = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "Codex-TokenHub-MiniMax-H3-Skill/1.0",
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            parsed: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = raw.decode("utf-8", errors="replace")
        message = parsed.get("error", {}).get("message") if isinstance(parsed, dict) else None
        raise ApiError(message or f"TokenHub returned HTTP {error.code}", error.code, parsed) from None
    except urllib.error.URLError as error:
        raise ApiError(f"Could not reach TokenHub: {error.reason}") from None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("TokenHub returned a non-JSON response") from None
    if not isinstance(parsed, dict):
        raise ApiError("TokenHub returned an unexpected JSON response", body=parsed)
    if parsed.get("error"):
        error_object = parsed["error"]
        message = error_object.get("message") if isinstance(error_object, dict) else str(error_object)
        raise ApiError(message or "TokenHub returned an error", body=parsed)
    return parsed


def task_status(response: Dict[str, Any]) -> str:
    task = response.get("task")
    if isinstance(task, dict):
        return str(task.get("status", "unknown")).lower()
    return str(response.get("status", "unknown")).lower()


def task_download_url(response: Dict[str, Any]) -> Optional[str]:
    task = response.get("task")
    if isinstance(task, dict):
        content = task.get("content")
        if isinstance(content, dict) and content.get("url"):
            return str(content["url"])
    value = response.get("url")
    return str(value) if value else None


def query_task(task_id: str, api_key: str) -> Dict[str, Any]:
    quoted = urllib.parse.quote(task_id, safe="")
    return api_request("GET", TASK_PATH.format(task_id=quoted), api_key)


def poll_until_terminal(task_id: str, api_key: str, interval: float, timeout: float) -> Dict[str, Any]:
    started = time.monotonic()
    while True:
        response = query_task(task_id, api_key)
        status = task_status(response)
        stderr(f"Task {task_id}: {status}")
        if status in TERMINAL_STATUSES:
            return response
        if status not in RUNNING_STATUSES:
            raise SkillError(f"Task returned unknown status: {status}")
        if time.monotonic() - started >= timeout:
            raise SkillError(f"Timed out after {timeout:g} seconds while waiting for task {task_id}")
        time.sleep(interval)


def safe_task_name(task_id: str) -> str:
    safe = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in task_id)
    return safe[:180] or "task"


def default_output_dir() -> Path:
    return Path.cwd() / "outputs" / "minimax-h3"


def resolve_output_dir(value: Optional[str]) -> Path:
    return Path(value).expanduser().resolve() if value else default_output_dir().resolve()


def write_metadata(output_dir: Path, task_id: str, metadata: Dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{safe_task_name(task_id)}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def download_video(url: str, output_dir: Path, task_id: str) -> Path:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SkillError("Task returned a non-HTTP download URL")
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{safe_task_name(task_id)}.mp4"
    temporary = destination.with_suffix(".mp4.part")
    request = urllib.request.Request(url, headers={"User-Agent": "Codex-TokenHub-MiniMax-H3-Skill/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response, temporary.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
    except (urllib.error.URLError, OSError) as error:
        if temporary.exists():
            temporary.unlink()
        raise SkillError(f"Video download failed: {error}") from None
    temporary.replace(destination)
    return destination


def status_summary(response: Dict[str, Any]) -> Dict[str, Any]:
    task = response.get("task") if isinstance(response.get("task"), dict) else response
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    return {
        "task_id": task.get("id") or response.get("task_id"),
        "status": task_status(response),
        "model": task.get("model"),
        "duration": task.get("duration") or response.get("duration"),
        "resolution": task.get("resolution") or response.get("resolution"),
        "ratio": task.get("ratio"),
        "total_tokens": usage.get("total_tokens"),
        "request_id": response.get("request_id"),
    }


def handle_generate(args: argparse.Namespace) -> int:
    payload, sanitized = build_generation_payload(args)
    stderr(f"Final prompt: {sanitized['prompt']}")
    stderr(
        f"Mode: {sanitized['mode']} | Resolution: {sanitized['resolution']} | "
        f"Duration: {sanitized['duration']}s | Ratio: {sanitized['ratio']}"
    )
    if args.dry_run:
        json_print({"dry_run": True, "request": sanitized})
        return 0
    api_key = require_api_key()
    submission = api_request("POST", SUBMIT_PATH, api_key, payload)
    task_id = submission.get("task_id")
    if not task_id:
        raise ApiError("TokenHub submission did not return task_id", body=submission)
    task_id = str(task_id)
    output_dir = resolve_output_dir(args.output_dir)
    metadata: Dict[str, Any] = {
        "task_id": task_id,
        "request": sanitized,
        "submission": submission,
        "created_at": int(time.time()),
    }
    metadata_path = write_metadata(output_dir, task_id, metadata)
    if args.no_wait:
        json_print({"task_id": task_id, "status": "submitted", "metadata_path": str(metadata_path)})
        return 0
    response = poll_until_terminal(task_id, api_key, args.interval, args.timeout)
    metadata["task_response"] = response
    metadata["updated_at"] = int(time.time())
    status = task_status(response)
    result = status_summary(response)
    if status == "succeeded" or status == "success":
        url = task_download_url(response)
        if not url:
            raise SkillError("Task succeeded but no download URL was returned")
        video_path = download_video(url, output_dir, task_id)
        metadata["video_path"] = str(video_path)
        result["video_path"] = str(video_path)
    metadata_path = write_metadata(output_dir, task_id, metadata)
    result["metadata_path"] = str(metadata_path)
    json_print(result)
    return 0 if status in {"succeeded", "success"} else 2


def handle_task(args: argparse.Namespace) -> int:
    api_key = require_api_key()
    response = (
        poll_until_terminal(args.task_id, api_key, args.interval, args.timeout)
        if args.wait
        else query_task(args.task_id, api_key)
    )
    output_dir = resolve_output_dir(args.output_dir)
    result = status_summary(response)
    metadata: Dict[str, Any] = {
        "task_id": args.task_id,
        "task_response": response,
        "queried_at": int(time.time()),
    }
    status = task_status(response)
    if args.download:
        if status not in {"succeeded", "success"}:
            raise SkillError(f"Task is {status}; video can only be downloaded after success")
        url = task_download_url(response)
        if not url:
            raise SkillError("Task succeeded but no download URL was returned")
        video_path = download_video(url, output_dir, args.task_id)
        metadata["video_path"] = str(video_path)
        result["video_path"] = str(video_path)
    metadata_path = write_metadata(output_dir, args.task_id, metadata)
    result["metadata_path"] = str(metadata_path)
    json_print(result)
    return 0 if status not in {"failed", "fail", "cancelled"} else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="Submit a MiniMax H3 video generation task")
    generate.add_argument("--prompt", required=True, help="Final prompt to send to MiniMax H3")
    generate.add_argument("--first-frame", help="Local path, public URL, or image Data URL")
    generate.add_argument("--last-frame", help="Local path, public URL, or image Data URL")
    generate.add_argument("--reference-image", action="append", default=[], help="Repeatable local path, URL, or Data URL")
    generate.add_argument("--reference-video", action="append", default=[], help="Repeatable local path, URL, or Data URL")
    generate.add_argument("--reference-audio", action="append", default=[], help="Repeatable local path, URL, or Data URL")
    generate.add_argument("--resolution", default="768P", choices=("768P", "2K"))
    generate.add_argument("--duration", type=int, default=4)
    generate.add_argument("--ratio", choices=("adaptive",) + SUPPORTED_RATIOS)
    generate.add_argument("--output-dir", help="Default: ./outputs/minimax-h3")
    generate.add_argument("--interval", type=float, default=5.0)
    generate.add_argument("--timeout", type=float, default=900.0)
    generate.add_argument("--dry-run", action="store_true", help="Validate and print a sanitized request without calling TokenHub")
    generate.add_argument("--no-wait", action="store_true", help="Submit and return without polling")
    generate.set_defaults(handler=handle_generate)

    task = subparsers.add_parser("task", help="Query an existing MiniMax H3 task")
    task.add_argument("task_id")
    task.add_argument("--wait", action="store_true", help="Poll until a terminal state")
    task.add_argument("--download", action="store_true", help="Download a successful task")
    task.add_argument("--output-dir", help="Default: ./outputs/minimax-h3")
    task.add_argument("--interval", type=float, default=5.0)
    task.add_argument("--timeout", type=float, default=900.0)
    task.set_defaults(handler=handle_task)
    return parser


def validate_runtime_numbers(args: argparse.Namespace) -> None:
    if hasattr(args, "interval") and args.interval <= 0:
        raise SkillError("--interval must be greater than zero")
    if hasattr(args, "timeout") and args.timeout <= 0:
        raise SkillError("--timeout must be greater than zero")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_runtime_numbers(args)
        return int(args.handler(args))
    except ApiError as error:
        payload: Dict[str, Any] = {"error": str(error), "type": "api_error"}
        if error.status is not None:
            payload["http_status"] = error.status
        if error.body is not None:
            payload["details"] = error.body
        json_print(payload)
        return 1
    except SkillError as error:
        json_print({"error": str(error), "type": "validation_error"})
        return 1
    except KeyboardInterrupt:
        json_print({"error": "Interrupted", "type": "cancelled"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
