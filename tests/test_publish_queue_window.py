from pathlib import Path

from PySide6 import QtWidgets

from videotrans.component import publish_queue_window as queue_ui
from videotrans.process.publish_queue import enqueue_publish_job, load_publish_queue


def _app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _queue(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    path = tmp_path / "reup_queue.json"
    job, _created = enqueue_publish_job(
        path,
        video,
        platforms=["youtube", "douyin"],
        title="My clip",
    )
    return path, job


def test_queue_window_renders_one_row_per_platform_and_filters(tmp_path):
    _app()
    queue_path, _job = _queue(tmp_path)
    window = queue_ui.PublishQueueWindow(queue_path)

    assert window.table.rowCount() == 2
    assert {window.table.item(row, 1).text() for row in range(2)} == {
        "youtube",
        "douyin",
    }

    window.status_filter.setCurrentText("failed")
    assert all(window.table.isRowHidden(row) for row in range(2))
    window.status_filter.setCurrentText("Tất cả")
    window.filter_edit.setText("douyin")
    visible = [
        window.table.item(row, 1).text()
        for row in range(2)
        if not window.table.isRowHidden(row)
    ]
    assert visible == ["douyin"]


def test_queue_window_can_mark_selected_platform_skipped(tmp_path):
    _app()
    queue_path, job = _queue(tmp_path)
    window = queue_ui.PublishQueueWindow(queue_path)
    window.table.selectRow(0)
    platform = window.table.item(0, 1).text()

    window._mark_skipped()

    updated = load_publish_queue(queue_path)["jobs"][0]
    assert updated["id"] == job["id"]
    assert updated["platforms"][platform]["status"] == "skipped"


def test_stage_worker_forwards_logs_and_result(monkeypatch, tmp_path):
    queue_path, job = _queue(tmp_path)
    logs = []
    results = []
    failures = []

    def fake_stage(queue, job_id, platform, log):
        log("uploading")
        assert Path(queue) == queue_path
        assert job_id == job["id"]
        return {"platform": platform, "requires_final_confirmation": True}

    monkeypatch.setattr(queue_ui, "stage_publish_job", fake_stage)
    worker = queue_ui.StagePublishWorker(queue_path, job["id"], "youtube")
    worker.log.connect(logs.append)
    worker.succeeded.connect(results.append)
    worker.failed.connect(failures.append)

    worker.run()

    assert logs == ["uploading"]
    assert results[0]["requires_final_confirmation"] is True
    assert failures == []


def test_direct_youtube_worker_forwards_options_logs_and_result(monkeypatch, tmp_path):
    queue_path, job = _queue(tmp_path)
    logs = []
    results = []
    failures = []

    class Result:
        platform = "youtube"
        remote_id = "abc123"
        remote_url = "https://www.youtube.com/watch?v=abc123"

    def fake_publish(
        queue,
        job_id,
        *,
        access_token,
        privacy,
        contains_synthetic_media,
        log,
    ):
        assert Path(queue) == queue_path
        assert job_id == job["id"]
        assert access_token == "secret-token"
        assert privacy == "unlisted"
        assert contains_synthetic_media is True
        log("uploading through API")
        return Result()

    monkeypatch.setattr(queue_ui, "direct_publish_youtube", fake_publish)
    worker = queue_ui.DirectYouTubePublishWorker(
        queue_path,
        job["id"],
        "secret-token",
        "",
        "unlisted",
        True,
    )
    worker.log.connect(logs.append)
    worker.succeeded.connect(results.append)
    worker.failed.connect(failures.append)

    worker.run()

    assert logs == ["uploading through API"]
    assert results == [
        {
            "platform": "youtube",
            "remote_id": "abc123",
            "remote_url": "https://www.youtube.com/watch?v=abc123",
        }
    ]
    assert failures == []


def test_queue_window_exposes_opt_in_youtube_api_action(tmp_path):
    _app()
    queue_path, _job = _queue(tmp_path)
    window = queue_ui.PublishQueueWindow(queue_path)

    assert window.youtube_api_btn.text() == "Đăng YouTube qua API"
    assert window.worker is None


def test_batch_workflow_exposes_reup_manager_for_current_output(tmp_path):
    _app()
    from videotrans.component.video_batch_workflow import VideoBatchWorkflowWindow

    batch = VideoBatchWorkflowWindow()
    batch.run_page.output_dir.setText(tmp_path.as_posix())
    batch._open_reup_queue()

    assert batch.run_page.open_reup_btn.text() == "Quản lý reup"
    assert batch.reup_window.queue_path == tmp_path / "reup_queue.json"
