from types import SimpleNamespace

from PySide6 import QtWidgets

from videotrans.component import tiktok_publish_dialog as tiktok_ui
from videotrans.component.publish_queue_window import PublishQueueWindow
from videotrans.process.publish_queue import (
    enqueue_publish_job,
    update_platform_status,
)


def _app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_tiktok_status_worker_forwards_result(monkeypatch, tmp_path):
    results = []
    failures = []

    def fake_refresh(queue_path, job_id, *, access_token, log):
        assert str(queue_path).endswith("queue.json")
        assert job_id == "job-id"
        assert access_token == "token"
        log("status fetched")
        return SimpleNamespace(
            publish_id="pub-id",
            status="PUBLISH_COMPLETE",
            remote_id="post-id",
            remote_url="https://www.tiktok.com/@creator/video/post-id",
        )

    monkeypatch.setattr(
        tiktok_ui,
        "refresh_tiktok_publish_status",
        fake_refresh,
    )
    worker = tiktok_ui.TikTokStatusWorker(
        tmp_path / "queue.json",
        "job-id",
        "token",
    )
    logs = []
    worker.log.connect(logs.append)
    worker.succeeded.connect(results.append)
    worker.failed.connect(failures.append)

    worker.run()

    assert logs == ["status fetched"]
    assert results[0]["status"] == "PUBLISH_COMPLETE"
    assert results[0]["remote_id"] == "post-id"
    assert failures == []


def test_queue_window_exposes_processing_remote_id_and_status_action(tmp_path):
    _app()
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
        "processing",
        remote_id="pub-123",
    )
    window = PublishQueueWindow(queue_path)
    window.table.selectRow(0)

    assert window.api_status_btn.text() == "Cập nhật trạng thái API"
    assert window._selected()["remote_id"] == "pub-123"
