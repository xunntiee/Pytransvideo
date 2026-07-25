# -*- coding: utf-8 -*-
"""Opt-in direct publishing through official platform APIs."""

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


YOUTUBE_UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_PRIVACY_VALUES = {"private", "unlisted", "public"}


@dataclass(frozen=True)
class DirectPublishResult:
    platform: str
    remote_id: str
    remote_url: str
    raw: dict


class YouTubeResumablePublisher:
    CHUNK_UNIT = 256 * 1024

    def __init__(
        self,
        access_token,
        *,
        session=None,
        timeout=120,
        log=None,
        chunk_size=8 * 1024 * 1024,
        max_retries=2,
        retry_backoff=0.25,
    ):
        token = str(access_token or "").strip()
        if not token:
            raise PublishAdapterError(
                "Thi\u1ebfu YouTube OAuth access token c\u00f3 scope youtube.upload."
            )
        if session is None:
            import requests

            session = requests.Session()
        self.access_token = token
        self.session = session
        self.timeout = int(timeout)
        self.log = log or (lambda _message: None)
        self.chunk_size = int(chunk_size)
        if self.chunk_size <= 0 or self.chunk_size % self.CHUNK_UNIT:
            raise PublishAdapterError(
                "YouTube chunk_size phải là bội số dương của 256 KiB."
            )
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))

    @property
    def authorization(self):
        return f"Bearer {self.access_token}"

    def publish(
        self,
        job,
        *,
        privacy="private",
        category_id="22",
        notify_subscribers=False,
        made_for_kids=False,
        contains_synthetic_media=False,
    ):
        privacy = str(privacy or "private").casefold()
        if privacy not in YOUTUBE_PRIVACY_VALUES:
            raise PublishAdapterError(f"YouTube privacy kh\u00f4ng h\u1ee3p l\u1ec7: {privacy}")
        video = Path(job.get("video") or "")
        if not video.is_file():
            raise PublishAdapterError(f"Video kh\u00f4ng t\u1ed3n t\u1ea1i: {video}")
        size = video.stat().st_size
        content_type = mimetypes.guess_type(video.name)[0] or "application/octet-stream"
        metadata = {
            "snippet": {
                "title": str(job.get("title") or video.stem)[:100],
                "description": str(job.get("description") or ""),
                "tags": list(job.get("hashtags") or []),
                "categoryId": str(category_id or "22"),
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": bool(made_for_kids),
                "containsSyntheticMedia": bool(contains_synthetic_media),
            },
        }
        params = {
            "uploadType": "resumable",
            "part": "snippet,status",
            "notifySubscribers": str(bool(notify_subscribers)).lower(),
        }
        headers = {
            "Authorization": self.authorization,
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": content_type,
        }
        self.log("YouTube: kh\u1edfi t\u1ea1o resumable upload session...")
        response = self.session.post(
            YOUTUBE_UPLOAD_ENDPOINT,
            params=params,
            headers=headers,
            json=metadata,
            timeout=self.timeout,
        )
        response.raise_for_status()
        upload_url = response.headers.get("Location")
        if not upload_url:
            raise PublishAdapterError("YouTube kh\u00f4ng tr\u1ea3 Location cho resumable session.")

        self.log(f"YouTube: upload {video.name} ({size} bytes)...")
        with video.open("rb") as file_obj:
            payload = self._upload_chunks(
                upload_url,
                file_obj,
                size=size,
                content_type=content_type,
            )
        remote_id = str(payload.get("id") or "")
        if not remote_id:
            raise PublishAdapterError(f"YouTube upload kh\u00f4ng tr\u1ea3 video id: {payload}")
        return DirectPublishResult(
            platform="youtube",
            remote_id=remote_id,
            remote_url=f"https://www.youtube.com/watch?v={remote_id}",
            raw=payload,
        )

    def _upload_chunks(self, upload_url, file_obj, *, size, content_type):
        offset = 0
        while offset < size:
            file_obj.seek(offset)
            chunk = file_obj.read(min(self.chunk_size, size - offset))
            end = offset + len(chunk) - 1
            headers = {
                "Authorization": self.authorization,
                "Content-Type": content_type,
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end}/{size}",
            }
            response = None
            for attempt in range(self.max_retries + 1):
                try:
                    response = self.session.put(
                        upload_url,
                        headers=headers,
                        data=io.BytesIO(chunk),
                        timeout=self.timeout,
                    )
                except Exception:
                    if attempt >= self.max_retries:
                        raise
                    self._retry_wait(attempt, offset)
                    continue
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    break
                if attempt >= self.max_retries:
                    response.raise_for_status()
                self._retry_wait(attempt, offset)
            if response is None:
                raise PublishAdapterError("YouTube upload không nhận được phản hồi.")
            if response.status_code in {200, 201}:
                self.log("YouTube: upload 100%.")
                return response.json()
            if response.status_code == 308:
                acknowledged = self._acknowledged_offset(response, end)
                offset = acknowledged + 1
                percent = min(99.9, (offset / size) * 100)
                self.log(f"YouTube: upload {percent:.1f}%.")
                continue
            response.raise_for_status()
            raise PublishAdapterError(
                f"YouTube upload trả HTTP {response.status_code}."
            )
        raise PublishAdapterError("YouTube kết thúc upload nhưng không trả video id.")

    def _retry_wait(self, attempt, offset):
        delay = self.retry_backoff * (2**attempt)
        self.log(
            f"YouTube: mạng/API tạm lỗi tại byte {offset}; "
            f"thử lại sau {delay:.2f}s..."
        )
        if delay:
            time.sleep(delay)

    @staticmethod
    def _acknowledged_offset(response, fallback):
        value = str(response.headers.get("Range") or "")
        if "-" in value:
            try:
                return int(value.rsplit("-", 1)[1])
            except ValueError:
                pass
        return int(fallback)


def _job_for_direct_publish(queue_path, job_id, platform):
    queue = load_publish_queue(queue_path)
    job = next((item for item in queue["jobs"] if item.get("id") == job_id), None)
    if not job:
        raise KeyError(job_id)
    state = (job.get("platforms") or {}).get(platform)
    if state is None:
        raise PublishAdapterError(f"Job {job_id} kh\u00f4ng c\u00f3 platform {platform}.")
    if state.get("status") == "published":
        raise PublishAdapterError(f"Job {job_id} \u0111\u00e3 Published tr\u00ean {platform}.")
    return job


def direct_publish_youtube(
    queue_path,
    job_id,
    *,
    access_token,
    privacy="private",
    session=None,
    log=None,
    made_for_kids=False,
    contains_synthetic_media=False,
):
    """Publish one explicitly confirmed queue job through YouTube Data API."""
    queue_path = Path(queue_path)
    job = _job_for_direct_publish(queue_path, job_id, "youtube")
    update_platform_status(queue_path, job_id, "youtube", "publishing")
    try:
        result = YouTubeResumablePublisher(
            access_token,
            session=session,
            log=log,
        ).publish(
            job,
            privacy=privacy,
            made_for_kids=made_for_kids,
            contains_synthetic_media=contains_synthetic_media,
        )
        update_platform_status(
            queue_path,
            job_id,
            "youtube",
            "published",
            remote_id=result.remote_id,
            remote_url=result.remote_url,
        )
        return result
    except Exception as exc:
        update_platform_status(
            queue_path,
            job_id,
            "youtube",
            "failed",
            error=str(exc),
        )
        raise
