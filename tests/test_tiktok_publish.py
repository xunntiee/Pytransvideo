from pathlib import Path

import pytest

from videotrans.process.browser_publish import PublishAdapterError
from videotrans.process.publish_queue import enqueue_publish_job, load_publish_queue
from videotrans.process.tiktok_publish import (
    CREATOR_INFO_ENDPOINT,
    DIRECT_POST_ENDPOINT,
    STATUS_ENDPOINT,
    TikTokDirectPublisher,
    direct_publish_tiktok,
    tiktok_chunk_plan,
)


class Response:
    def __init__(self, *, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _ok(data):
    return {"data": data, "error": {"code": "ok", "message": ""}}


class TikTokSession:
    def __init__(self, *, final_status="PUBLISH_COMPLETE"):
        self.posts = []
        self.puts = []
        self.final_status = final_status

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url == CREATOR_INFO_ENDPOINT:
            return Response(
                payload=_ok(
                    {
                        "creator_username": "creator",
                        "creator_nickname": "Creator",
                        "privacy_level_options": [
                            "PUBLIC_TO_EVERYONE",
                            "SELF_ONLY",
                        ],
                        "comment_disabled": False,
                        "duet_disabled": True,
                        "stitch_disabled": False,
                        "max_video_post_duration_sec": 300,
                    }
                )
            )
        if url == DIRECT_POST_ENDPOINT:
            return Response(
                payload=_ok(
                    {
                        "publish_id": "pub-123",
                        "upload_url": "https://upload.example/tiktok",
                    }
                )
            )
        if url == STATUS_ENDPOINT:
            return Response(
                payload=_ok(
                    {
                        "status": self.final_status,
                        "publicaly_available_post_id": (
                            ["987654"] if self.final_status == "PUBLISH_COMPLETE" else []
                        ),
                        "uploaded_bytes": 11,
                    }
                )
            )
        raise AssertionError(url)

    def put(self, url, **kwargs):
        body = kwargs["data"].read()
        self.puts.append((url, kwargs, body))
        return Response(status=201)


def _queue(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video-bytes")
    queue_path = tmp_path / "reup_queue.json"
    job, _created = enqueue_publish_job(
        queue_path,
        video,
        platforms=["tiktok", "douyin"],
        title="My title",
        description="My description",
        hashtags="#one #two",
    )
    return queue_path, job, video


def test_tiktok_direct_post_queries_creator_uploads_and_builds_remote_url(tmp_path):
    _queue_path, job, video = _queue(tmp_path)
    session = TikTokSession()
    publisher = TikTokDirectPublisher(
        "user-token",
        session=session,
        retry_backoff=0,
    )

    result = publisher.publish(
        job,
        privacy="PUBLIC_TO_EVERYONE",
        duration_seconds=12,
        disable_comment=True,
        is_aigc=True,
    )

    assert result.publish_id == "pub-123"
    assert result.status == "PUBLISH_COMPLETE"
    assert result.remote_id == "987654"
    assert result.remote_url == "https://www.tiktok.com/@creator/video/987654"
    creator_post = session.posts[0]
    assert creator_post[0] == CREATOR_INFO_ENDPOINT
    assert creator_post[1]["headers"]["Authorization"] == "Bearer user-token"
    init_body = session.posts[1][1]["json"]
    assert init_body["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert init_body["post_info"]["disable_duet"] is True
    assert init_body["post_info"]["disable_comment"] is True
    assert init_body["post_info"]["is_aigc"] is True
    assert init_body["post_info"]["title"].endswith("#one #two")
    assert init_body["source_info"] == {
        "source": "FILE_UPLOAD",
        "video_size": video.stat().st_size,
        "chunk_size": video.stat().st_size,
        "total_chunk_count": 1,
    }
    assert session.puts[0][1]["headers"]["Content-Range"] == "bytes 0-10/11"
    assert session.puts[0][2] == b"video-bytes"


def test_tiktok_processing_result_is_checkpointed_without_double_attempt(tmp_path):
    queue_path, job, _video = _queue(tmp_path)
    result = direct_publish_tiktok(
        queue_path,
        job["id"],
        access_token="token",
        privacy="SELF_ONLY",
        session=TikTokSession(final_status="PROCESSING_UPLOAD"),
        duration_seconds=1,
    )

    state = load_publish_queue(queue_path)["jobs"][0]["platforms"]["tiktok"]
    assert result.status == "PROCESSING_UPLOAD"
    assert state["status"] == "processing"
    assert state["remote_id"] == "pub-123"
    assert state["attempts"] == 1


def test_tiktok_rejects_privacy_not_returned_by_creator_and_checkpoints_failure(
    tmp_path,
):
    queue_path, job, _video = _queue(tmp_path)
    with pytest.raises(PublishAdapterError, match="privacy"):
        direct_publish_tiktok(
            queue_path,
            job["id"],
            access_token="token",
            privacy="FOLLOWER_OF_CREATOR",
            session=TikTokSession(),
            duration_seconds=1,
        )

    state = load_publish_queue(queue_path)["jobs"][0]["platforms"]["tiktok"]
    assert state["status"] == "failed"
    assert state["attempts"] == 1
    assert "privacy" in state["last_error"]


def test_tiktok_chunk_plan_follows_small_large_and_limit_rules():
    assert tiktok_chunk_plan(4_000_000) == {
        "chunk_size": 4_000_000,
        "total_chunk_count": 1,
    }
    assert tiktok_chunk_plan(50_000_123, 10_000_000) == {
        "chunk_size": 10_000_000,
        "total_chunk_count": 5,
    }
    just_over_limit = tiktok_chunk_plan(64_000_001, 64_000_000)
    assert just_over_limit["total_chunk_count"] == 2
    assert just_over_limit["chunk_size"] >= 5_000_000
    with pytest.raises(PublishAdapterError, match="1000"):
        tiktok_chunk_plan(64_000_000 * 1001, 64_000_000)
