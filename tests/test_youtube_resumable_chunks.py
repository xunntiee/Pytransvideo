import pytest

from videotrans.process.browser_publish import PublishAdapterError
from videotrans.process.direct_publish import YouTubeResumablePublisher


class Response:
    def __init__(self, status, *, headers=None, payload=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class ChunkSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.put_calls = []

    def post(self, _url, **_kwargs):
        return Response(200, headers={"Location": "https://upload.example/session"})

    def put(self, url, **kwargs):
        body = kwargs["data"].read()
        self.put_calls.append((url, kwargs["headers"], body))
        return self.responses.pop(0)


def _job(tmp_path, data):
    video = tmp_path / "large.mp4"
    video.write_bytes(data)
    return {
        "video": str(video),
        "title": "Chunked upload",
        "description": "",
        "hashtags": [],
    }


def test_youtube_upload_sends_acknowledged_chunks_with_content_ranges(tmp_path):
    unit = YouTubeResumablePublisher.CHUNK_UNIT
    session = ChunkSession(
        [
            Response(308, headers={"Range": f"bytes=0-{unit - 1}"}),
            Response(308, headers={"Range": f"bytes=0-{2 * unit - 1}"}),
            Response(201, payload={"id": "chunked-video"}),
        ]
    )
    publisher = YouTubeResumablePublisher(
        "token",
        session=session,
        chunk_size=unit,
        retry_backoff=0,
    )

    result = publisher.publish(_job(tmp_path, b"x" * (2 * unit + 5)))

    assert result.remote_id == "chunked-video"
    assert [call[1]["Content-Range"] for call in session.put_calls] == [
        f"bytes 0-{unit - 1}/{2 * unit + 5}",
        f"bytes {unit}-{2 * unit - 1}/{2 * unit + 5}",
        f"bytes {2 * unit}-{2 * unit + 4}/{2 * unit + 5}",
    ]
    assert [len(call[2]) for call in session.put_calls] == [unit, unit, 5]


def test_youtube_upload_retries_transient_status_without_advancing(tmp_path):
    logs = []
    session = ChunkSession(
        [
            Response(503),
            Response(201, payload={"id": "retried-video"}),
        ]
    )
    publisher = YouTubeResumablePublisher(
        "token",
        session=session,
        max_retries=1,
        retry_backoff=0,
        log=logs.append,
    )

    result = publisher.publish(_job(tmp_path, b"small-video"))

    assert result.remote_id == "retried-video"
    assert len(session.put_calls) == 2
    assert session.put_calls[0][1]["Content-Range"] == (
        session.put_calls[1][1]["Content-Range"]
    )
    assert any("thử lại" in message for message in logs)


def test_youtube_rejects_non_aligned_chunk_size():
    with pytest.raises(PublishAdapterError, match="256 KiB"):
        YouTubeResumablePublisher("token", chunk_size=123)
