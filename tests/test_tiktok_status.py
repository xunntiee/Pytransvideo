from videotrans.process.publish_queue import (
    enqueue_publish_job,
    load_publish_queue,
    update_platform_status,
)
from videotrans.process.tiktok_status import refresh_tiktok_publish_status


class Response:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _ok(data):
    return {"data": data, "error": {"code": "ok", "message": ""}}


class StatusSession:
    def __init__(self, status, *, post_ids=None, fail_reason=""):
        self.status = status
        self.post_ids = post_ids or []
        self.fail_reason = fail_reason

    def post(self, url, **_kwargs):
        if url.endswith("/status/fetch/"):
            return Response(
                _ok(
                    {
                        "status": self.status,
                        "publicaly_available_post_id": self.post_ids,
                        "fail_reason": self.fail_reason,
                    }
                )
            )
        if url.endswith("/creator_info/query/"):
            return Response(_ok({"creator_username": "creator"}))
        raise AssertionError(url)


def _processing_queue(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    queue_path = tmp_path / "queue.json"
    job, _ = enqueue_publish_job(
        queue_path,
        video,
        platforms=["tiktok"],
    )
    update_platform_status(
        queue_path,
        job["id"],
        "tiktok",
        "publishing",
    )
    update_platform_status(
        queue_path,
        job["id"],
        "tiktok",
        "processing",
        remote_id="pub-123",
    )
    return queue_path, job


def test_refresh_tiktok_status_marks_public_post_published(tmp_path):
    queue_path, job = _processing_queue(tmp_path)

    result = refresh_tiktok_publish_status(
        queue_path,
        job["id"],
        access_token="token",
        session=StatusSession("PUBLISH_COMPLETE", post_ids=["999"]),
    )

    state = load_publish_queue(queue_path)["jobs"][0]["platforms"]["tiktok"]
    assert result.remote_url == "https://www.tiktok.com/@creator/video/999"
    assert state["status"] == "published"
    assert state["remote_id"] == "999"
    assert state["remote_url"] == result.remote_url
    assert state["attempts"] == 1


def test_refresh_tiktok_status_keeps_processing_without_new_attempt(tmp_path):
    queue_path, job = _processing_queue(tmp_path)

    refresh_tiktok_publish_status(
        queue_path,
        job["id"],
        access_token="token",
        session=StatusSession("PROCESSING_UPLOAD"),
    )

    state = load_publish_queue(queue_path)["jobs"][0]["platforms"]["tiktok"]
    assert state["status"] == "processing"
    assert state["remote_id"] == "pub-123"
    assert state["attempts"] == 1


def test_refresh_tiktok_status_checkpoints_processing_failure(tmp_path):
    queue_path, job = _processing_queue(tmp_path)

    result = refresh_tiktok_publish_status(
        queue_path,
        job["id"],
        access_token="token",
        session=StatusSession("FAILED", fail_reason="video_pull_failed"),
    )

    state = load_publish_queue(queue_path)["jobs"][0]["platforms"]["tiktok"]
    assert result.status == "FAILED"
    assert state["status"] == "failed"
    assert "video_pull_failed" in state["last_error"]
    assert state["attempts"] == 1
