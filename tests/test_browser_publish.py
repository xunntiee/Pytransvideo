import pytest

from videotrans.process.browser_publish import (
    PublishAdapterError,
    formatted_caption,
    publisher_spec,
    stage_publish_job,
)
from videotrans.process.publish_queue import (
    enqueue_publish_job,
    load_publish_queue,
    update_platform_status,
)


class FakeBrowser:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def stage(self, spec, job):
        self.calls.append((spec, job))
        if self.error:
            raise self.error
        return {
            "platform": spec.id,
            "requires_final_confirmation": True,
        }


def _queued_job(tmp_path, platforms=("youtube", "douyin")):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    queue_path = tmp_path / "reup_queue.json"
    job, _created = enqueue_publish_job(
        queue_path,
        video,
        platforms=platforms,
        title="Clip title",
        description="Clip description",
        hashtags="#vietsub #viral",
    )
    return queue_path, job


def test_browser_specs_cover_primary_reup_platforms():
    assert publisher_spec("youtube").name == "YouTube Studio"
    assert publisher_spec("tiktok").upload_url.startswith("https://")
    assert publisher_spec("douyin").id == "douyin"
    assert publisher_spec("bilibili").id == "bilibili"
    assert publisher_spec("kuaishou").id == "kuaishou"
    assert publisher_spec("xiaohongshu").id == "xiaohongshu"


def test_caption_combines_title_description_and_unique_hashtags(tmp_path):
    _queue, job = _queued_job(tmp_path)

    assert formatted_caption(job) == (
        "Clip title\n\nClip description\n\n#vietsub #viral"
    )


def test_stage_job_records_staged_and_requires_manual_confirmation(tmp_path):
    queue_path, job = _queued_job(tmp_path)
    browser = FakeBrowser()

    result = stage_publish_job(
        queue_path,
        job["id"],
        "youtube",
        browser=browser,
    )

    updated = load_publish_queue(queue_path)["jobs"][0]
    assert result["requires_final_confirmation"] is True
    assert browser.calls[0][0].id == "youtube"
    assert browser.calls[0][1]["id"] == job["id"]
    assert updated["platforms"]["youtube"]["status"] == "staged"
    assert updated["platforms"]["youtube"]["attempts"] == 1
    assert updated["platforms"]["douyin"]["status"] == "ready"


def test_stage_failure_is_checkpointed_for_retry(tmp_path):
    queue_path, job = _queued_job(tmp_path)
    browser = FakeBrowser(error=RuntimeError("login required"))

    with pytest.raises(RuntimeError, match="login required"):
        stage_publish_job(queue_path, job["id"], "douyin", browser=browser)

    state = load_publish_queue(queue_path)["jobs"][0]["platforms"]["douyin"]
    assert state["status"] == "failed"
    assert state["attempts"] == 1
    assert state["last_error"] == "login required"


def test_published_job_cannot_be_staged_again(tmp_path):
    queue_path, job = _queued_job(tmp_path)
    update_platform_status(queue_path, job["id"], "youtube", "published")

    with pytest.raises(PublishAdapterError, match="published"):
        stage_publish_job(
            queue_path,
            job["id"],
            "youtube",
            browser=FakeBrowser(),
        )


def test_unknown_or_unassigned_platform_is_rejected(tmp_path):
    queue_path, job = _queued_job(tmp_path, platforms=("youtube",))

    with pytest.raises(PublishAdapterError, match="browser adapter"):
        stage_publish_job(queue_path, job["id"], "unknown", browser=FakeBrowser())
    with pytest.raises(PublishAdapterError, match="không có nền tảng"):
        stage_publish_job(queue_path, job["id"], "douyin", browser=FakeBrowser())
