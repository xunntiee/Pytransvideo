import pytest

from videotrans.process.browser_publish import PublishAdapterError
from videotrans.process.direct_publish import (
    YouTubeResumablePublisher,
    direct_publish_youtube,
)
from videotrans.process.publish_queue import (
    enqueue_publish_job,
    load_publish_queue,
    update_platform_status,
)


class FakeResponse:
    def __init__(self, *, payload=None, headers=None, status=200):
        self._payload = payload or {}
        self.headers = headers or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeYouTubeSession:
    def __init__(self, *, upload_payload=None, fail_put=False):
        self.upload_payload = upload_payload or {"id": "video123"}
        self.fail_put = fail_put
        self.post_call = None
        self.put_call = None

    def post(self, url, **kwargs):
        self.post_call = (url, kwargs)
        return FakeResponse(headers={"Location": "https://upload.example/session/1"})

    def put(self, url, **kwargs):
        body = kwargs["data"].read()
        self.put_call = (url, kwargs, body)
        return FakeResponse(
            payload=self.upload_payload,
            status=500 if self.fail_put else 201,
        )


def _queue(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video-bytes")
    queue_path = tmp_path / "reup_queue.json"
    job, _created = enqueue_publish_job(
        queue_path,
        video,
        platforms=["youtube", "douyin"],
        title="My title",
        description="My description",
        hashtags="#one #two",
    )
    return queue_path, job, video


def test_youtube_resumable_upload_uses_safe_metadata_and_streams_file(tmp_path):
    _queue_path, job, video = _queue(tmp_path)
    session = FakeYouTubeSession()
    publisher = YouTubeResumablePublisher("secret-token", session=session)

    result = publisher.publish(
        job,
        privacy="private",
        made_for_kids=False,
        contains_synthetic_media=True,
    )

    post_url, post = session.post_call
    assert post_url.endswith("/youtube/v3/videos")
    assert post["params"]["uploadType"] == "resumable"
    assert post["headers"]["Authorization"] == "Bearer secret-token"
    assert post["headers"]["X-Upload-Content-Length"] == str(video.stat().st_size)
    assert post["json"]["snippet"]["title"] == "My title"
    assert post["json"]["snippet"]["tags"] == ["one", "two"]
    assert post["json"]["status"]["privacyStatus"] == "private"
    assert post["json"]["status"]["containsSyntheticMedia"] is True
    assert session.put_call[2] == b"video-bytes"
    assert result.remote_id == "video123"
    assert result.remote_url.endswith("watch?v=video123")


def test_direct_publish_updates_only_youtube_platform_state(tmp_path):
    queue_path, job, _video = _queue(tmp_path)
    session = FakeYouTubeSession(upload_payload={"id": "abc"})

    result = direct_publish_youtube(
        queue_path,
        job["id"],
        access_token="token",
        privacy="unlisted",
        session=session,
    )

    updated = load_publish_queue(queue_path)["jobs"][0]
    assert result.remote_id == "abc"
    assert updated["platforms"]["youtube"]["status"] == "published"
    assert updated["platforms"]["youtube"]["remote_url"].endswith("watch?v=abc")
    assert updated["platforms"]["youtube"]["attempts"] == 1
    assert updated["platforms"]["douyin"]["status"] == "ready"


def test_failed_direct_publish_is_checkpointed(tmp_path):
    queue_path, job, _video = _queue(tmp_path)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        direct_publish_youtube(
            queue_path,
            job["id"],
            access_token="token",
            session=FakeYouTubeSession(fail_put=True),
        )

    state = load_publish_queue(queue_path)["jobs"][0]["platforms"]["youtube"]
    assert state["status"] == "failed"
    assert state["attempts"] == 1
    assert "HTTP 500" in state["last_error"]


def test_direct_publish_rejects_missing_token_invalid_privacy_and_duplicates(tmp_path):
    queue_path, job, _video = _queue(tmp_path)
    with pytest.raises(PublishAdapterError, match="OAuth"):
        direct_publish_youtube(
            queue_path,
            job["id"],
            access_token="",
            session=FakeYouTubeSession(),
        )

    session = FakeYouTubeSession()
    with pytest.raises(PublishAdapterError, match="privacy"):
        YouTubeResumablePublisher("token", session=session).publish(
            job,
            privacy="friends",
        )

    update_platform_status(queue_path, job["id"], "youtube", "published")
    with pytest.raises(PublishAdapterError, match="Published"):
        direct_publish_youtube(
            queue_path,
            job["id"],
            access_token="token",
            session=FakeYouTubeSession(),
        )
