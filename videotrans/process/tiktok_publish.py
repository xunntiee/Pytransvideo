# -*- coding: utf-8 -*-
"""Opt-in TikTok Content Posting API adapter."""

from dataclasses import dataclass
import io
import mimetypes
from pathlib import Path
import time

from videotrans.process.browser_publish import PublishAdapterError
from videotrans.process.publish_queue import (
    load_publish_queue,
    update_platform_status,
)


TIKTOK_API_BASE = "https://open.tiktokapis.com"
CREATOR_INFO_ENDPOINT = f"{TIKTOK_API_BASE}/v2/post/publish/creator_info/query/"
DIRECT_POST_ENDPOINT = f"{TIKTOK_API_BASE}/v2/post/publish/video/init/"
STATUS_ENDPOINT = f"{TIKTOK_API_BASE}/v2/post/publish/status/fetch/"
TIKTOK_PRIVACY_LEVELS = {
    "PUBLIC_TO_EVERYONE",
    "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWER_OF_CREATOR",
    "SELF_ONLY",
}
TIKTOK_TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}
TIKTOK_MIN_CHUNK = 5_000_000
TIKTOK_MAX_CHUNK = 64_000_000
TIKTOK_MAX_FINAL_CHUNK = 128_000_000
TIKTOK_MAX_CHUNKS = 1000
TIKTOK_SUPPORTED_MIME = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


@dataclass(frozen=True)
class TikTokPublishResult:
    platform: str
    publish_id: str
    status: str
    remote_id: str
    remote_url: str
    creator: dict
    raw: dict


def _truncate_utf16(text, max_units):
    encoded = str(text or "").encode("utf-16-le")
    return encoded[: max_units * 2].decode("utf-16-le", errors="ignore")


def _caption_for_job(job):
    parts = [
        str(job.get("title") or "").strip(),
        str(job.get("description") or "").strip(),
    ]
    tags = " ".join(
        f"#{str(tag).strip().lstrip('#')}"
        for tag in (job.get("hashtags") or [])
        if str(tag).strip().lstrip("#")
    )
    if tags:
        parts.append(tags)
    return _truncate_utf16("\n".join(item for item in parts if item), 2200)


def tiktok_chunk_plan(video_size, preferred_chunk_size=10_000_000):
    size = int(video_size)
    if size <= 0:
        raise PublishAdapterError("TikTok không thể upload video rỗng.")
    if size < TIKTOK_MIN_CHUNK:
        return {"chunk_size": size, "total_chunk_count": 1}
    chunk_size = min(
        TIKTOK_MAX_CHUNK,
        max(TIKTOK_MIN_CHUNK, int(preferred_chunk_size)),
    )
    if size > TIKTOK_MAX_CHUNK and size // chunk_size < 2:
        chunk_size = max(TIKTOK_MIN_CHUNK, size // 2)
    total = max(1, size // chunk_size)
    if total > TIKTOK_MAX_CHUNKS:
        raise PublishAdapterError(
            f"TikTok cần {total} chunks, vượt giới hạn {TIKTOK_MAX_CHUNKS}."
        )
    final_size = size - (total - 1) * chunk_size
    if final_size > TIKTOK_MAX_FINAL_CHUNK:
        raise PublishAdapterError(
            f"TikTok final chunk quá lớn: {final_size} bytes."
        )
    return {"chunk_size": chunk_size, "total_chunk_count": total}


class TikTokDirectPublisher:
    def __init__(
        self,
        access_token,
        *,
        session=None,
        timeout=120,
        log=None,
        chunk_size=10_000_000,
        max_retries=2,
        retry_backoff=0.25,
    ):
        token = str(access_token or "").strip()
        if not token:
            raise PublishAdapterError(
                "Thiếu TikTok user access token có scope video.publish."
            )
        if session is None:
            import requests

            session = requests.Session()
        self.access_token = token
        self.session = session
        self.timeout = int(timeout)
        self.log = log or (lambda _message: None)
        self.chunk_size = int(chunk_size)
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))

    @property
    def api_headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    def creator_info(self):
        self.log("TikTok: đọc tài khoản và quyền đăng hiện tại...")
        return self._api_post(CREATOR_INFO_ENDPOINT, {}).get("data") or {}

    def fetch_status(self, publish_id):
        return self._api_post(
            STATUS_ENDPOINT,
            {"publish_id": str(publish_id)},
        ).get("data") or {}

    def publish(
        self,
        job,
        *,
        privacy="SELF_ONLY",
        disable_duet=False,
        disable_stitch=False,
        disable_comment=False,
        brand_content=False,
        brand_organic=False,
        is_aigc=False,
        cover_timestamp_ms=0,
        creator=None,
        duration_seconds=None,
    ):
        video = Path(job.get("video") or "")
        if not video.is_file():
            raise PublishAdapterError(f"Video không tồn tại: {video}")
        mime_type = TIKTOK_SUPPORTED_MIME.get(video.suffix.casefold())
        if not mime_type:
            guessed = mimetypes.guess_type(video.name)[0]
            raise PublishAdapterError(
                f"TikTok Direct Post chưa hỗ trợ định dạng {video.suffix} ({guessed})."
            )
        creator = dict(creator or self.creator_info())
        options = list(creator.get("privacy_level_options") or [])
        privacy = str(privacy or "SELF_ONLY").upper()
        if privacy not in TIKTOK_PRIVACY_LEVELS or privacy not in options:
            raise PublishAdapterError(
                f"TikTok privacy {privacy} không có trong quyền tài khoản: {options}"
            )
        if duration_seconds is None:
            from videotrans.util.help_ffmpeg import get_video_info

            duration_seconds = get_video_info(
                str(video),
                video_time=True,
            ) / 1000
        maximum = float(creator.get("max_video_post_duration_sec") or 0)
        if maximum and float(duration_seconds) > maximum:
            raise PublishAdapterError(
                f"Video dài {float(duration_seconds):.1f}s, vượt giới hạn "
                f"{maximum:.0f}s của tài khoản TikTok."
            )
        size = video.stat().st_size
        plan = tiktok_chunk_plan(size, self.chunk_size)
        post_info = {
            "title": _caption_for_job(job),
            "privacy_level": privacy,
            "disable_duet": bool(disable_duet or creator.get("duet_disabled")),
            "disable_stitch": bool(
                disable_stitch or creator.get("stitch_disabled")
            ),
            "disable_comment": bool(
                disable_comment or creator.get("comment_disabled")
            ),
            "video_cover_timestamp_ms": max(0, int(cover_timestamp_ms)),
            "brand_content_toggle": bool(brand_content),
            "brand_organic_toggle": bool(brand_organic),
            "is_aigc": bool(is_aigc),
        }
        request_body = {
            "post_info": post_info,
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                **plan,
            },
        }
        self.log("TikTok: khởi tạo Direct Post...")
        initialized = self._api_post(DIRECT_POST_ENDPOINT, request_body)
        data = initialized.get("data") or {}
        publish_id = str(data.get("publish_id") or "")
        upload_url = str(data.get("upload_url") or "")
        if not publish_id or not upload_url:
            raise PublishAdapterError(
                f"TikTok không trả publish_id/upload_url: {initialized}"
            )
        self._upload_file(
            upload_url,
            video,
            size=size,
            mime_type=mime_type,
            plan=plan,
        )
        status_data = self.fetch_status(publish_id)
        status = str(status_data.get("status") or "PROCESSING_UPLOAD")
        post_ids = status_data.get("publicaly_available_post_id") or []
        remote_id = str(post_ids[0]) if post_ids else publish_id
        username = str(creator.get("creator_username") or "").lstrip("@")
        remote_url = (
            f"https://www.tiktok.com/@{username}/video/{remote_id}"
            if status == "PUBLISH_COMPLETE" and username and post_ids
            else ""
        )
        return TikTokPublishResult(
            platform="tiktok",
            publish_id=publish_id,
            status=status,
            remote_id=remote_id,
            remote_url=remote_url,
            creator=creator,
            raw={"initialize": initialized, "status": status_data},
        )

    def _api_post(self, url, body):
        response = self._request_with_retry(
            "post",
            url,
            headers=self.api_headers,
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
        error = payload.get("error") or {}
        if error.get("code") not in {None, "", "ok"}:
            raise PublishAdapterError(
                f"TikTok API {error.get('code')}: "
                f"{error.get('message') or 'unknown error'}"
            )
        return payload

    def _upload_file(self, upload_url, video, *, size, mime_type, plan):
        base_chunk = int(plan["chunk_size"])
        count = int(plan["total_chunk_count"])
        offset = 0
        with video.open("rb") as file_obj:
            for index in range(count):
                is_final = index == count - 1
                length = size - offset if is_final else base_chunk
                chunk = file_obj.read(length)
                end = offset + len(chunk) - 1
                response = self._request_with_retry(
                    "put",
                    upload_url,
                    headers={
                        "Content-Type": mime_type,
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                    },
                    data=io.BytesIO(chunk),
                )
                expected = 201 if is_final else 206
                if response.status_code != expected:
                    response.raise_for_status()
                    raise PublishAdapterError(
                        f"TikTok chunk {index + 1}/{count} trả HTTP "
                        f"{response.status_code}, cần {expected}."
                    )
                offset = end + 1
                self.log(f"TikTok: upload {(offset / size) * 100:.1f}%.")

    def _request_with_retry(self, method, url, **kwargs):
        response = None
        for attempt in range(self.max_retries + 1):
            try:
                response = getattr(self.session, method)(
                    url,
                    timeout=self.timeout,
                    **kwargs,
                )
            except Exception:
                if attempt >= self.max_retries:
                    raise
                self._retry_wait(attempt)
                if "data" in kwargs:
                    kwargs["data"].seek(0)
                continue
            if response.status_code not in TIKTOK_TRANSIENT_HTTP:
                return response
            if attempt >= self.max_retries:
                return response
            self._retry_wait(attempt)
            if "data" in kwargs:
                kwargs["data"].seek(0)
        return response

    def _retry_wait(self, attempt):
        delay = self.retry_backoff * (2**attempt)
        self.log(f"TikTok: API/mạng tạm lỗi; thử lại sau {delay:.2f}s...")
        if delay:
            time.sleep(delay)


def _tiktok_job(queue_path, job_id):
    queue = load_publish_queue(queue_path)
    job = next((item for item in queue["jobs"] if item.get("id") == job_id), None)
    if not job:
        raise KeyError(job_id)
    state = (job.get("platforms") or {}).get("tiktok")
    if state is None:
        raise PublishAdapterError(f"Job {job_id} không có platform tiktok.")
    if state.get("status") == "published":
        raise PublishAdapterError(f"Job {job_id} đã Published trên TikTok.")
    return job


def direct_publish_tiktok(
    queue_path,
    job_id,
    *,
    access_token,
    privacy="SELF_ONLY",
    session=None,
    log=None,
    duration_seconds=None,
    disable_duet=False,
    disable_stitch=False,
    disable_comment=False,
    brand_content=False,
    brand_organic=False,
    is_aigc=False,
):
    """Publish one explicitly confirmed TikTok queue job."""
    queue_path = Path(queue_path)
    job = _tiktok_job(queue_path, job_id)
    update_platform_status(queue_path, job_id, "tiktok", "publishing")
    try:
        result = TikTokDirectPublisher(
            access_token,
            session=session,
            log=log,
        ).publish(
            job,
            privacy=privacy,
            duration_seconds=duration_seconds,
            disable_duet=disable_duet,
            disable_stitch=disable_stitch,
            disable_comment=disable_comment,
            brand_content=brand_content,
            brand_organic=brand_organic,
            is_aigc=is_aigc,
        )
        if result.status == "FAILED":
            reason = str(result.raw["status"].get("fail_reason") or "unknown")
            raise PublishAdapterError(f"TikTok xử lý video thất bại: {reason}")
        queue_status = (
            "published" if result.status == "PUBLISH_COMPLETE" else "processing"
        )
        update_platform_status(
            queue_path,
            job_id,
            "tiktok",
            queue_status,
            remote_id=result.remote_id,
            remote_url=result.remote_url,
        )
        return result
    except Exception as exc:
        update_platform_status(
            queue_path,
            job_id,
            "tiktok",
            "failed",
            error=str(exc),
        )
        raise
