# -*- coding: utf-8 -*-
"""Refresh asynchronous TikTok Content Posting API jobs."""

from dataclasses import dataclass
from pathlib import Path

from videotrans.process.browser_publish import PublishAdapterError
from videotrans.process.publish_queue import (
    load_publish_queue,
    update_platform_status,
)
from videotrans.process.tiktok_publish import TikTokDirectPublisher


@dataclass(frozen=True)
class TikTokStatusResult:
    publish_id: str
    status: str
    remote_id: str
    remote_url: str
    raw: dict


def refresh_tiktok_publish_status(
    queue_path,
    job_id,
    *,
    access_token,
    session=None,
    log=None,
):
    queue_path = Path(queue_path)
    queue = load_publish_queue(queue_path)
    job = next((item for item in queue["jobs"] if item.get("id") == job_id), None)
    if not job:
        raise KeyError(job_id)
    state = (job.get("platforms") or {}).get("tiktok")
    if state is None:
        raise PublishAdapterError(f"Job {job_id} không có platform tiktok.")
    publish_id = str(state.get("remote_id") or "")
    if not publish_id:
        raise PublishAdapterError(
            "TikTok job chưa có publish_id; hãy Direct Post trước."
        )
    publisher = TikTokDirectPublisher(
        access_token,
        session=session,
        log=log,
    )
    status_data = publisher.fetch_status(publish_id)
    status = str(status_data.get("status") or "")
    post_ids = status_data.get("publicaly_available_post_id") or []
    remote_id = str(post_ids[0]) if post_ids else publish_id
    remote_url = ""
    if status == "PUBLISH_COMPLETE" and post_ids:
        creator = publisher.creator_info()
        username = str(creator.get("creator_username") or "").lstrip("@")
        if username:
            remote_url = f"https://www.tiktok.com/@{username}/video/{remote_id}"
    result = TikTokStatusResult(
        publish_id=publish_id,
        status=status,
        remote_id=remote_id,
        remote_url=remote_url,
        raw=status_data,
    )
    if status == "FAILED":
        reason = str(status_data.get("fail_reason") or "unknown")
        update_platform_status(
            queue_path,
            job_id,
            "tiktok",
            "failed",
            remote_id=publish_id,
            error=f"TikTok xử lý video thất bại: {reason}",
        )
    elif status == "PUBLISH_COMPLETE":
        update_platform_status(
            queue_path,
            job_id,
            "tiktok",
            "published",
            remote_id=remote_id,
            remote_url=remote_url,
        )
    else:
        update_platform_status(
            queue_path,
            job_id,
            "tiktok",
            "processing",
            remote_id=publish_id,
        )
    return result
