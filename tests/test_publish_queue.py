import json
from pathlib import Path

import pytest

from videotrans.process.publish_queue import (
    enqueue_publish_job,
    load_publish_queue,
    normalize_hashtags,
    normalize_platforms,
    update_platform_status,
    validate_publish_input,
    video_fingerprint,
)


def test_normalizes_platforms_and_hashtags():
    assert normalize_platforms(["Shorts", "TikTok", "xhs", "TikTok"]) == [
        "youtube",
        "tiktok",
        "xiaohongshu",
    ]
    assert normalize_hashtags("#viral, video #VIRAL vietsub") == ["viral", "video", "vietsub"]


def test_fingerprint_changes_when_video_content_changes(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"first")
    first = video_fingerprint(video)
    video.write_bytes(b"second")

    assert video_fingerprint(video) != first


def test_enqueue_is_idempotent_for_same_video_and_platforms(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    queue_path = tmp_path / "reup_queue.json"

    first, first_created = enqueue_publish_job(
        queue_path,
        video,
        platforms=["youtube", "tiktok"],
        title="Clip",
        hashtags="#viral #vietsub",
    )
    second, second_created = enqueue_publish_job(
        queue_path,
        video,
        platforms=["TikTok", "Shorts"],
        title="A changed title must not create a duplicate",
    )

    assert first_created is True
    assert second_created is False
    assert first["id"] == second["id"]
    assert len(load_publish_queue(queue_path)["jobs"]) == 1
    assert json.loads(queue_path.read_text(encoding="utf-8"))["version"] == 1


def test_platform_status_is_recorded_without_affecting_other_platforms(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    queue_path = tmp_path / "reup_queue.json"
    job, _created = enqueue_publish_job(
        queue_path,
        video,
        platforms=["youtube", "douyin"],
    )

    update_platform_status(queue_path, job["id"], "youtube", "publishing")
    updated = update_platform_status(
        queue_path,
        job["id"],
        "youtube",
        "published",
        remote_id="abc",
        remote_url="https://example.test/watch/abc",
    )

    assert updated["platforms"]["youtube"]["status"] == "published"
    assert updated["platforms"]["youtube"]["remote_id"] == "abc"
    assert updated["platforms"]["youtube"]["attempts"] == 1
    assert updated["platforms"]["douyin"]["status"] == "ready"


def test_validation_warns_for_landscape_short_form_and_rejects_bad_input(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")

    result = validate_publish_input(video, ["TikTok"], width=1920, height=1080)

    assert result["errors"] == []
    assert result["warnings"]
    with pytest.raises(ValueError, match="nền tảng"):
        normalize_platforms([])
    with pytest.raises(ValueError, match="không tồn tại"):
        enqueue_publish_job(
            tmp_path / "queue.json",
            tmp_path / "missing.mp4",
            platforms=["youtube"],
        )


def test_queue_save_is_atomic_and_leaves_no_temporary_file(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    queue_path = tmp_path / "reup_queue.json"

    enqueue_publish_job(queue_path, video, platforms=["bilibili"])

    assert queue_path.is_file()
    assert not Path(str(queue_path) + ".tmp").exists()
