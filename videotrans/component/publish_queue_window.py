# -*- coding: utf-8 -*-
"""Desktop manager for browser-assisted and opt-in API reup jobs."""

import os
from pathlib import Path

from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from videotrans.component.video_editor import DEFAULT_OUTPUT_DIR
from videotrans.component.tiktok_publish_dialog import (
    DirectTikTokPublishWorker,
    TikTokCreatorInfoWorker,
    TikTokPublishDialog,
    TikTokStatusWorker,
)
from videotrans.process.browser_publish import (
    BROWSER_PUBLISHERS,
    stage_publish_job,
)
from videotrans.process.direct_publish import direct_publish_youtube
from videotrans.process.youtube_oauth import (
    get_youtube_access_token,
    has_saved_youtube_credentials,
)
from videotrans.process.publish_queue import (
    load_publish_queue,
    update_platform_status,
)


ROLE_JOB_ID = QtCore.Qt.UserRole + 1
ROLE_PLATFORM = QtCore.Qt.UserRole + 2
ROLE_VIDEO = QtCore.Qt.UserRole + 3
ROLE_REMOTE_ID = QtCore.Qt.UserRole + 4


class StagePublishWorker(QThread):
    log = Signal(str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, queue_path, job_id, platform, parent=None):
        super().__init__(parent)
        self.queue_path = str(queue_path)
        self.job_id = str(job_id)
        self.platform = str(platform)

    def run(self):
        try:
            result = stage_publish_job(
                self.queue_path,
                self.job_id,
                self.platform,
                log=self.log.emit,
            )
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class DirectYouTubePublishWorker(QThread):
    log = Signal(str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        queue_path,
        job_id,
        access_token,
        client_secret_path,
        privacy,
        contains_synthetic_media,
        parent=None,
    ):
        super().__init__(parent)
        self.queue_path = str(queue_path)
        self.job_id = str(job_id)
        self.access_token = str(access_token)
        self.client_secret_path = str(client_secret_path or "")
        self.privacy = str(privacy)
        self.contains_synthetic_media = bool(contains_synthetic_media)

    def run(self):
        try:
            access_token = self.access_token or get_youtube_access_token(
                client_secret_path=self.client_secret_path,
                log=self.log.emit,
            )
            result = direct_publish_youtube(
                self.queue_path,
                self.job_id,
                access_token=access_token,
                privacy=self.privacy,
                contains_synthetic_media=self.contains_synthetic_media,
                log=self.log.emit,
            )
            self.succeeded.emit(
                {
                    "platform": result.platform,
                    "remote_id": result.remote_id,
                    "remote_url": result.remote_url,
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class PublishQueueWindow(QtWidgets.QWidget):
    def __init__(self, queue_path=None, parent=None):
        super().__init__(parent)
        default = Path(DEFAULT_OUTPUT_DIR) / "batch" / "reup_queue.json"
        self.queue_path = Path(queue_path or default)
        self.worker = None
        self.setWindowTitle("Qu\u1ea3n l\u00fd h\u00e0ng \u0111\u1ee3i reup")
        self.resize(1180, 680)
        self._build_ui()
        self.set_queue_path(self.queue_path)

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        title = QtWidgets.QLabel("H\u00e0ng \u0111\u1ee3i reup - upload staging c\u00f3 x\u00e1c nh\u1eadn")
        title.setStyleSheet("font-size:20px;font-weight:700")
        root.addWidget(title)

        note = QtWidgets.QLabel(
            "Stage s\u1ebd m\u1edf Chrome profile c\u1ee7a tool, ch\u1ecdn video v\u00e0 \u0111i\u1ec1n metadata. "
            "Tool kh\u00f4ng t\u1ef1 b\u1ea5m n\u00fat \u0110\u0103ng/Xu\u1ea5t b\u1ea3n; h\u00e3y ki\u1ec3m tra n\u1ed9i dung tr\u00ean trang creator tr\u01b0\u1edbc."
        )
        note.setWordWrap(True)
        root.addWidget(note)

        path_row = QtWidgets.QHBoxLayout()
        self.path_edit = QtWidgets.QLineEdit()
        self.choose_btn = QtWidgets.QPushButton("Ch\u1ecdn queue")
        self.refresh_btn = QtWidgets.QPushButton("L\u00e0m m\u1edbi")
        self.open_folder_btn = QtWidgets.QPushButton("M\u1edf th\u01b0 m\u1ee5c")
        path_row.addWidget(QtWidgets.QLabel("Queue"))
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.choose_btn)
        path_row.addWidget(self.refresh_btn)
        path_row.addWidget(self.open_folder_btn)
        root.addLayout(path_row)

        filter_row = QtWidgets.QHBoxLayout()
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("L\u1ecdc theo ti\u00eau \u0111\u1ec1, platform, status...")
        self.status_filter = QtWidgets.QComboBox()
        self.status_filter.addItems(
            [
                "T\u1ea5t c\u1ea3",
                "ready",
                "publishing",
                "processing",
                "staged",
                "published",
                "failed",
                "skipped",
            ]
        )
        filter_row.addWidget(self.filter_edit, 1)
        filter_row.addWidget(self.status_filter)
        root.addLayout(filter_row)

        self.table = QtWidgets.QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Job", "Platform", "Status", "Ti\u00eau \u0111\u1ec1", "Video", "Attempts", "Remote URL", "L\u1ed7i cu\u1ed1i"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(7, QtWidgets.QHeaderView.Stretch)
        root.addWidget(self.table, 1)

        actions = QtWidgets.QHBoxLayout()
        self.open_video_btn = QtWidgets.QPushButton("M\u1edf video")
        self.open_creator_btn = QtWidgets.QPushButton("M\u1edf trang creator")
        self.stage_btn = QtWidgets.QPushButton("Stage job \u0111\u00e3 ch\u1ecdn")
        self.youtube_api_btn = QtWidgets.QPushButton("\u0110\u0103ng YouTube qua API")
        self.tiktok_api_btn = QtWidgets.QPushButton("\u0110\u0103ng TikTok qua API")
        self.api_status_btn = QtWidgets.QPushButton("C\u1eadp nh\u1eadt tr\u1ea1ng th\u00e1i API")
        self.mark_published_btn = QtWidgets.QPushButton("\u0110\u00e1nh d\u1ea5u Published")
        self.mark_skipped_btn = QtWidgets.QPushButton("B\u1ecf qua")
        actions.addWidget(self.open_video_btn)
        actions.addWidget(self.open_creator_btn)
        actions.addStretch()
        actions.addWidget(self.mark_skipped_btn)
        actions.addWidget(self.mark_published_btn)
        actions.addWidget(self.api_status_btn)
        actions.addWidget(self.tiktok_api_btn)
        actions.addWidget(self.youtube_api_btn)
        actions.addWidget(self.stage_btn)
        root.addLayout(actions)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        root.addWidget(self.progress)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        root.addWidget(self.log)

        self.choose_btn.clicked.connect(self._choose_queue)
        self.refresh_btn.clicked.connect(self.refresh)
        self.open_folder_btn.clicked.connect(self._open_queue_folder)
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.status_filter.currentTextChanged.connect(self._apply_filter)
        self.stage_btn.clicked.connect(self._stage_selected)
        self.youtube_api_btn.clicked.connect(self._publish_youtube_api)
        self.tiktok_api_btn.clicked.connect(self._publish_tiktok_api)
        self.api_status_btn.clicked.connect(self._refresh_api_status)
        self.open_video_btn.clicked.connect(self._open_selected_video)
        self.open_creator_btn.clicked.connect(self._open_creator)
        self.mark_published_btn.clicked.connect(self._mark_published)
        self.mark_skipped_btn.clicked.connect(self._mark_skipped)

    def set_queue_path(self, queue_path):
        self.queue_path = Path(queue_path)
        self.path_edit.setText(self.queue_path.as_posix())
        self.refresh()

    def _choose_queue(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Ch\u1ecdn reup_queue.json",
            str(self.queue_path.parent),
            "JSON files (*.json);;All files (*)",
        )
        if filename:
            self.set_queue_path(filename)

    def _open_queue_folder(self):
        folder = self.queue_path.parent
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def append_log(self, text):
        if text:
            self.log.appendPlainText(str(text))

    def refresh(self):
        self.queue_path = Path(self.path_edit.text().strip() or self.queue_path)
        try:
            queue = load_publish_queue(self.queue_path)
        except Exception as exc:
            self.append_log(f"L\u1ed7i \u0111\u1ecdc queue: {exc}")
            self.table.setRowCount(0)
            return
        rows = []
        for job in queue.get("jobs", []):
            for platform, state in (job.get("platforms") or {}).items():
                rows.append((job, platform, state))
        self.table.setRowCount(len(rows))
        for row, (job, platform, state) in enumerate(rows):
            values = [
                job.get("id", ""),
                platform,
                state.get("status", ""),
                job.get("title", ""),
                Path(job.get("video") or "").name,
                str(state.get("attempts", 0)),
                state.get("remote_url", ""),
                state.get("last_error", ""),
            ]
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value or ""))
                item.setToolTip(str(value or ""))
                item.setData(ROLE_JOB_ID, job.get("id", ""))
                item.setData(ROLE_PLATFORM, platform)
                item.setData(ROLE_VIDEO, job.get("video", ""))
                item.setData(ROLE_REMOTE_ID, state.get("remote_id", ""))
                self.table.setItem(row, column, item)
        self._apply_filter()

    def _apply_filter(self):
        query = self.filter_edit.text().strip().casefold()
        status = self.status_filter.currentText()
        for row in range(self.table.rowCount()):
            values = " ".join(
                self.table.item(row, column).text()
                for column in range(self.table.columnCount())
                if self.table.item(row, column)
            ).casefold()
            row_status = self.table.item(row, 2).text() if self.table.item(row, 2) else ""
            hide = bool(query and query not in values)
            if status != "T\u1ea5t c\u1ea3" and row_status != status:
                hide = True
            self.table.setRowHidden(row, hide)

    def _selected(self):
        row = self.table.currentRow()
        if row < 0:
            QtWidgets.QMessageBox.information(self, "Reup queue", "H\u00e3y ch\u1ecdn m\u1ed9t job/platform.")
            return None
        item = self.table.item(row, 0)
        return {
            "job_id": item.data(ROLE_JOB_ID),
            "platform": item.data(ROLE_PLATFORM),
            "video": item.data(ROLE_VIDEO),
            "status": self.table.item(row, 2).text(),
            "remote_id": item.data(ROLE_REMOTE_ID),
        }

    def _set_busy(self, busy):
        for widget in (
            self.choose_btn,
            self.refresh_btn,
            self.stage_btn,
            self.youtube_api_btn,
            self.tiktok_api_btn,
            self.api_status_btn,
            self.mark_published_btn,
            self.mark_skipped_btn,
        ):
            widget.setDisabled(busy)
        self.progress.setRange(0, 0 if busy else 1)
        if not busy:
            self.progress.setValue(0)

    def _publish_youtube_api(self):
        selected = self._selected()
        if not selected or self.worker and self.worker.isRunning():
            return
        if selected["platform"] != "youtube":
            QtWidgets.QMessageBox.warning(
                self,
                "YouTube API",
                "Chỉ job/platform YouTube mới có thể đăng bằng API này.",
            )
            return
        if selected["status"] == "published":
            QtWidgets.QMessageBox.warning(
                self,
                "YouTube API",
                "Job này đã Published; không đăng lại.",
            )
            return
        privacy, ok = QtWidgets.QInputDialog.getItem(
            self,
            "YouTube API",
            "Quyền riêng tư:",
            ["private", "unlisted", "public"],
            0,
            False,
        )
        if not ok:
            return
        token = os.environ.get("PYVIDEOTRANS_YOUTUBE_TOKEN", "").strip()
        client_secret_path = ""
        if not token and not has_saved_youtube_credentials():
            client_secret_path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "Chọn OAuth client secret JSON của Google",
                "",
                "JSON files (*.json);;All files (*)",
            )
            if not client_secret_path:
                return
        synthetic = QtWidgets.QMessageBox.question(
            self,
            "Nội dung tổng hợp",
            "Video có lồng tiếng AI hoặc nội dung tổng hợp có tính hiện thực?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        )
        reply = QtWidgets.QMessageBox.question(
            self,
            "Publish thật",
            f"Đăng video này lên YouTube ngay với privacy={privacy}?\n\n"
            "Hành động này tạo video thật trên kênh của access token.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        self._set_busy(True)
        self.worker = DirectYouTubePublishWorker(
            self.queue_path,
            selected["job_id"],
            token,
            client_secret_path,
            privacy,
            synthetic == QtWidgets.QMessageBox.Yes,
            parent=self,
        )
        self.worker.log.connect(self.append_log)
        self.worker.succeeded.connect(self._youtube_publish_succeeded)
        self.worker.failed.connect(self._stage_failed)
        self.worker.finished.connect(lambda: self._set_busy(False))
        self.worker.finished.connect(self.refresh)
        self.worker.start()

    def _youtube_publish_succeeded(self, result):
        remote_url = result.get("remote_url") or ""
        self.append_log(f"Đã publish YouTube thành công: {remote_url}")

    def _refresh_api_status(self):
        selected = self._selected()
        if not selected or self.worker and self.worker.isRunning():
            return
        if selected["platform"] != "tiktok" or not selected.get("remote_id"):
            QtWidgets.QMessageBox.information(
                self,
                "Trạng thái API",
                "Hiện chỉ TikTok Direct Post đang xử lý mới có trạng thái API để cập nhật.",
            )
            return
        token = os.environ.get("PYVIDEOTRANS_TIKTOK_TOKEN", "").strip()
        if not token:
            token, ok = QtWidgets.QInputDialog.getText(
                self,
                "TikTok OAuth",
                "User access token có scope video.publish (token không được lưu):",
                QtWidgets.QLineEdit.Password,
            )
            token = token.strip()
            if not ok or not token:
                return
        self._set_busy(True)
        self.worker = TikTokStatusWorker(
            self.queue_path,
            selected["job_id"],
            token,
            parent=self,
        )
        self.worker.log.connect(self.append_log)
        self.worker.succeeded.connect(self._api_status_succeeded)
        self.worker.failed.connect(self._stage_failed)
        self.worker.finished.connect(lambda: self._set_busy(False))
        self.worker.finished.connect(self.refresh)
        self.worker.start()

    def _api_status_succeeded(self, result):
        target = result.get("remote_url") or result.get("publish_id") or ""
        self.append_log(f"TikTok status: {result.get('status')} - {target}")

    def _publish_tiktok_api(self):
        selected = self._selected()
        if not selected or self.worker and self.worker.isRunning():
            return
        if selected["platform"] != "tiktok":
            QtWidgets.QMessageBox.warning(
                self,
                "TikTok API",
                "Chỉ job/platform TikTok mới có thể đăng bằng API này.",
            )
            return
        if selected["status"] == "published":
            QtWidgets.QMessageBox.warning(
                self,
                "TikTok API",
                "Job này đã Published; không đăng lại.",
            )
            return
        token = os.environ.get("PYVIDEOTRANS_TIKTOK_TOKEN", "").strip()
        if not token:
            token, ok = QtWidgets.QInputDialog.getText(
                self,
                "TikTok OAuth",
                "User access token có scope video.publish (token không được lưu):",
                QtWidgets.QLineEdit.Password,
            )
            token = token.strip()
            if not ok or not token:
                return
        self._pending_tiktok = {
            "selected": selected,
            "token": token,
            "creator": None,
        }
        self._set_busy(True)
        self.worker = TikTokCreatorInfoWorker(token, parent=self)
        self.worker.log.connect(self.append_log)
        self.worker.succeeded.connect(self._tiktok_creator_loaded)
        self.worker.failed.connect(self._stage_failed)
        self.worker.finished.connect(self._finish_tiktok_preflight)
        self.worker.start()

    def _tiktok_creator_loaded(self, creator):
        if getattr(self, "_pending_tiktok", None):
            self._pending_tiktok["creator"] = creator

    def _finish_tiktok_preflight(self):
        self._set_busy(False)
        pending = getattr(self, "_pending_tiktok", None)
        self._pending_tiktok = None
        if not pending or not pending.get("creator"):
            return
        dialog = TikTokPublishDialog(pending["creator"], self)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        options = dialog.values()
        reply = QtWidgets.QMessageBox.question(
            self,
            "Publish thật",
            f"Đăng video ngay lên TikTok @{pending['creator'].get('creator_username', '')} "
            f"với privacy={options['privacy']}?\n\n"
            "Hành động này gửi video thật lên tài khoản TikTok đã cấp token.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        self._set_busy(True)
        self.worker = DirectTikTokPublishWorker(
            self.queue_path,
            pending["selected"]["job_id"],
            pending["token"],
            options,
            parent=self,
        )
        self.worker.log.connect(self.append_log)
        self.worker.succeeded.connect(self._tiktok_publish_succeeded)
        self.worker.failed.connect(self._stage_failed)
        self.worker.finished.connect(lambda: self._set_busy(False))
        self.worker.finished.connect(self.refresh)
        self.worker.start()

    def _tiktok_publish_succeeded(self, result):
        status = result.get("status") or ""
        target = result.get("remote_url") or result.get("publish_id") or ""
        self.append_log(f"TikTok Direct Post: {status} - {target}")

    def _stage_selected(self):
        selected = self._selected()
        if not selected or self.worker and self.worker.isRunning():
            return
        if selected["status"] == "published":
            QtWidgets.QMessageBox.warning(self, "Reup queue", "Job n\u00e0y \u0111\u00e3 Published; kh\u00f4ng stage l\u1ea1i.")
            return
        reply = QtWidgets.QMessageBox.question(
            self,
            "X\u00e1c nh\u1eadn staging",
            f"Upload video v\u00e0o form {selected['platform']}?\n\n"
            "Tool s\u1ebd kh\u00f4ng b\u1ea5m n\u00fat \u0110\u0103ng/Xu\u1ea5t b\u1ea3n cu\u1ed1i c\u00f9ng.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        self._set_busy(True)
        self.worker = StagePublishWorker(
            self.queue_path,
            selected["job_id"],
            selected["platform"],
            parent=self,
        )
        self.worker.log.connect(self.append_log)
        self.worker.succeeded.connect(self._stage_succeeded)
        self.worker.failed.connect(self._stage_failed)
        self.worker.finished.connect(lambda: self._set_busy(False))
        self.worker.finished.connect(self.refresh)
        self.worker.start()

    def _stage_succeeded(self, result):
        self.append_log(
            f"\u0110\u00e3 staging {result.get('platform_name') or result.get('platform')}. "
            "H\u00e3y ki\u1ec3m tra Chrome v\u00e0 t\u1ef1 x\u00e1c nh\u1eadn \u0111\u0103ng."
        )

    def _stage_failed(self, error):
        self.append_log(f"Staging l\u1ed7i: {error}")
        QtWidgets.QMessageBox.warning(self, "Reup queue", error)

    def _open_selected_video(self):
        selected = self._selected()
        if selected and Path(selected["video"]).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(selected["video"]))

    def _open_creator(self):
        selected = self._selected()
        if not selected:
            return
        spec = BROWSER_PUBLISHERS.get(selected["platform"])
        if not spec:
            QtWidgets.QMessageBox.warning(self, "Reup queue", "Ch\u01b0a c\u00f3 browser adapter cho platform n\u00e0y.")
            return
        QDesktopServices.openUrl(QUrl(spec.upload_url))

    def _mark_published(self):
        selected = self._selected()
        if not selected:
            return
        remote_url, ok = QtWidgets.QInputDialog.getText(
            self,
            "\u0110\u00e1nh d\u1ea5u Published",
            "URL b\u00e0i \u0111\u0103ng (c\u00f3 th\u1ec3 \u0111\u1ec3 tr\u1ed1ng):",
        )
        if not ok:
            return
        reply = QtWidgets.QMessageBox.question(
            self,
            "X\u00e1c nh\u1eadn",
            "B\u1ea1n \u0111\u00e3 t\u1ef1 ki\u1ec3m tra v\u00e0 xu\u1ea5t b\u1ea3n video tr\u00ean trang creator?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        update_platform_status(
            self.queue_path,
            selected["job_id"],
            selected["platform"],
            "published",
            remote_url=remote_url.strip(),
        )
        self.refresh()

    def _mark_skipped(self):
        selected = self._selected()
        if not selected:
            return
        update_platform_status(
            self.queue_path,
            selected["job_id"],
            selected["platform"],
            "skipped",
        )
        self.refresh()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            QtWidgets.QMessageBox.warning(self, "Reup queue", "Staging \u0111ang ch\u1ea1y; h\u00e3y ch\u1edd ho\u00e0n t\u1ea5t.")
            event.ignore()
            return
        super().closeEvent(event)
