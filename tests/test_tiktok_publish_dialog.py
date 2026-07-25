from types import SimpleNamespace

from PySide6 import QtWidgets

from videotrans.component import tiktok_publish_dialog as tiktok_ui
from videotrans.component.publish_queue_window import PublishQueueWindow
from videotrans.process.publish_queue import enqueue_publish_job


def _app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _creator():
    return {
        "creator_username": "target_account",
        "creator_nickname": "Target Account",
        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
        "comment_disabled": True,
        "duet_disabled": False,
        "stitch_disabled": True,
        "max_video_post_duration_sec": 180,
    }


def test_tiktok_publish_dialog_uses_creator_permissions_and_safe_defaults():
    _app()
    dialog = tiktok_ui.TikTokPublishDialog(_creator())

    values = dialog.values()
    assert dialog.privacy.currentText() == "SELF_ONLY"
    assert dialog.disable_comment.isChecked() is True
    assert dialog.disable_comment.isEnabled() is False
    assert dialog.disable_duet.isChecked() is False
    assert dialog.disable_stitch.isChecked() is True
    assert values["is_aigc"] is True
    assert values["brand_content"] is False
    assert values["brand_organic"] is False


def test_tiktok_creator_worker_forwards_logs_and_creator(monkeypatch):
    logs = []
    results = []
    failures = []

    class FakePublisher:
        def __init__(self, token, log):
            assert token == "token"
            self.log = log

        def creator_info(self):
            self.log("creator loaded")
            return _creator()

    monkeypatch.setattr(tiktok_ui, "TikTokDirectPublisher", FakePublisher)
    worker = tiktok_ui.TikTokCreatorInfoWorker("token")
    worker.log.connect(logs.append)
    worker.succeeded.connect(results.append)
    worker.failed.connect(failures.append)

    worker.run()

    assert logs == ["creator loaded"]
    assert results == [_creator()]
    assert failures == []


def test_direct_tiktok_worker_forwards_options_and_result(monkeypatch, tmp_path):
    results = []
    failures = []
    calls = {}

    def fake_publish(queue_path, job_id, **kwargs):
        calls.update(kwargs)
        assert str(queue_path).endswith("queue.json")
        assert job_id == "job-id"
        return SimpleNamespace(
            platform="tiktok",
            publish_id="pub-id",
            status="PROCESSING_UPLOAD",
            remote_id="pub-id",
            remote_url="",
        )

    monkeypatch.setattr(tiktok_ui, "direct_publish_tiktok", fake_publish)
    worker = tiktok_ui.DirectTikTokPublishWorker(
        tmp_path / "queue.json",
        "job-id",
        "token",
        {
            "privacy": "SELF_ONLY",
            "is_aigc": True,
            "disable_comment": False,
        },
    )
    worker.succeeded.connect(results.append)
    worker.failed.connect(failures.append)

    worker.run()

    assert calls["access_token"] == "token"
    assert calls["privacy"] == "SELF_ONLY"
    assert calls["is_aigc"] is True
    assert results[0]["status"] == "PROCESSING_UPLOAD"
    assert failures == []


def test_publish_queue_window_exposes_tiktok_api_action(tmp_path):
    _app()
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    queue_path = tmp_path / "queue.json"
    enqueue_publish_job(
        queue_path,
        video,
        platforms=["tiktok"],
        title="Clip",
    )

    window = PublishQueueWindow(queue_path)

    assert window.tiktok_api_btn.text() == "Đăng TikTok qua API"
    assert window.worker is None
