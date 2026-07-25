# -*- coding: utf-8 -*-
"""Local, idempotent queue used by future publishing/reup adapters."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


QUEUE_VERSION = 1
PUBLISH_STATUSES = {
    "pending",
    "ready",
    "publishing",
    "processing",
    "staged",
    "published",
    "failed",
    "skipped",
}
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

PLATFORM_ALIASES = {
    "youtube": "youtube",
    "youtube shorts": "youtube",
    "shorts": "youtube",
    "tiktok": "tiktok",
    "douyin": "douyin",
    "bilibili": "bilibili",
    "kuaishou": "kuaishou",
    "xiaohongshu": "xiaohongshu",
    "xhs": "xiaohongshu",
    "facebook": "facebook",
    "facebook reels": "facebook",
    "reels": "facebook",
}

SHORT_FORM_PLATFORMS = {"youtube", "tiktok", "douyin", "kuaishou", "xiaohongshu", "facebook"}


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_platforms(platforms):
    normalized = []
    for value in platforms or []:
        platform = PLATFORM_ALIASES.get(str(value or "").strip().casefold())
        if platform and platform not in normalized:
            normalized.append(platform)
    if not normalized:
        raise ValueError("H\u00e3y ch\u1ecdn \u00edt nh\u1ea5t m\u1ed9t n\u1ec1n t\u1ea3ng reup.")
    return normalized


def normalize_hashtags(hashtags):
    if isinstance(hashtags, str):
        values = hashtags.replace(",", " ").split()
    else:
        values = hashtags or []
    result = []
    for value in values:
        tag = str(value or "").strip().lstrip("#")
        if tag and tag.casefold() not in {item.casefold() for item in result}:
            result.append(tag)
    return result


def video_fingerprint(video_path, chunk_size=1024 * 1024):
    """Hash the complete video in bounded chunks to prevent duplicate posts."""
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        while True:
            chunk = file_obj.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def validate_publish_input(video_path, platforms, *, width=0, height=0):
    path = Path(video_path)
    errors = []
    warnings = []
    if not path.is_file():
        errors.append(f"Video kh\u00f4ng t\u1ed3n t\u1ea1i: {path}")
    elif path.stat().st_size < 1:
        errors.append(f"Video r\u1ed7ng: {path}")
    elif path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        errors.append(f"\u0110\u1ecbnh d\u1ea1ng ch\u01b0a h\u1ed7 tr\u1ee3 reup: {path.suffix}")

    normalized = normalize_platforms(platforms)
    if width and height and width > height and set(normalized) & SHORT_FORM_PLATFORMS:
        warnings.append(
            "Video ngang: n\u00ean t\u1ea1o th\u00eam b\u1ea3n 9:16 cho Shorts/TikTok/Douyin/Kuaishou/Xiaohongshu/Reels."
        )
    return {"errors": errors, "warnings": warnings, "platforms": normalized}


def _empty_queue():
    return {"version": QUEUE_VERSION, "updated_at": _utc_now(), "jobs": []}


def load_publish_queue(queue_path):
    path = Path(queue_path)
    if not path.is_file():
        return _empty_queue()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ValueError(f"Publish queue kh\u00f4ng h\u1ee3p l\u1ec7: {path}")
    data.setdefault("version", QUEUE_VERSION)
    return data


def save_publish_queue(queue_path, queue):
    path = Path(queue_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    queue["version"] = QUEUE_VERSION
    queue["updated_at"] = _utc_now()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def enqueue_publish_job(
    queue_path,
    video_path,
    *,
    platforms,
    title="",
    description="",
    hashtags=None,
    source_url="",
    thumbnail="",
    width=0,
    height=0,
):
    """Add one publish job, returning an existing job for the same file/platforms."""
    path = Path(video_path).resolve()
    validation = validate_publish_input(path, platforms, width=width, height=height)
    if validation["errors"]:
        raise ValueError("; ".join(validation["errors"]))

    fingerprint = video_fingerprint(path)
    platform_ids = validation["platforms"]
    dedupe_key = hashlib.sha256(
        f"{fingerprint}|{'|'.join(sorted(platform_ids))}".encode("utf-8")
    ).hexdigest()
    queue = load_publish_queue(queue_path)
    for existing in queue["jobs"]:
        if existing.get("dedupe_key") == dedupe_key:
            return existing, False

    now = _utc_now()
    job = {
        "id": dedupe_key[:16],
        "dedupe_key": dedupe_key,
        "video": path.as_posix(),
        "fingerprint": fingerprint,
        "title": str(title or path.stem).strip(),
        "description": str(description or "").strip(),
        "hashtags": normalize_hashtags(hashtags),
        "source_url": str(source_url or "").strip(),
        "thumbnail": str(thumbnail or "").strip(),
        "warnings": validation["warnings"],
        "created_at": now,
        "updated_at": now,
        "platforms": {
            platform: {
                "status": "ready",
                "attempts": 0,
                "remote_id": "",
                "remote_url": "",
                "last_error": "",
                "updated_at": now,
            }
            for platform in platform_ids
        },
    }
    queue["jobs"].append(job)
    save_publish_queue(queue_path, queue)
    return job, True


def update_platform_status(
    queue_path,
    job_id,
    platform,
    status,
    *,
    remote_id="",
    remote_url="",
    error="",
):
    """Atomically record one adapter attempt/result."""
    if status not in PUBLISH_STATUSES:
        raise ValueError(f"Publish status kh\u00f4ng h\u1ee3p l\u1ec7: {status}")
    platform = PLATFORM_ALIASES.get(str(platform or "").casefold(), str(platform or "").casefold())
    queue = load_publish_queue(queue_path)
    job = next((item for item in queue["jobs"] if item.get("id") == job_id), None)
    if not job:
        raise KeyError(job_id)
    state = job.get("platforms", {}).get(platform)
    if state is None:
        raise KeyError(f"{job_id}:{platform}")
    now = _utc_now()
    state["status"] = status
    state["updated_at"] = now
    state["remote_id"] = str(remote_id or state.get("remote_id") or "")
    state["remote_url"] = str(remote_url or state.get("remote_url") or "")
    state["last_error"] = str(error or "")
    if status == "publishing" or (status in {"published", "failed"} and not state.get("attempts")):
        state["attempts"] = int(state.get("attempts", 0)) + 1
    job["updated_at"] = now
    save_publish_queue(queue_path, queue)
    return job
