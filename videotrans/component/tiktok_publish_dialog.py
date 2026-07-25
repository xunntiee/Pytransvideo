# -*- coding: utf-8 -*-
"""TikTok Direct Post preflight dialog and workers."""

from PySide6 import QtWidgets
from PySide6.QtCore import QThread, Signal

from videotrans.process.tiktok_publish import (
    TikTokDirectPublisher,
    direct_publish_tiktok,
)
from videotrans.process.tiktok_status import refresh_tiktok_publish_status


class TikTokCreatorInfoWorker(QThread):
    log = Signal(str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, access_token, parent=None):
        super().__init__(parent)
        self.access_token = str(access_token)

    def run(self):
        try:
            creator = TikTokDirectPublisher(
                self.access_token,
                log=self.log.emit,
            ).creator_info()
            self.succeeded.emit(creator)
        except Exception as exc:
            self.failed.emit(str(exc))


class DirectTikTokPublishWorker(QThread):
    log = Signal(str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        queue_path,
        job_id,
        access_token,
        options,
        parent=None,
    ):
        super().__init__(parent)
        self.queue_path = str(queue_path)
        self.job_id = str(job_id)
        self.access_token = str(access_token)
        self.options = dict(options or {})

    def run(self):
        try:
            result = direct_publish_tiktok(
                self.queue_path,
                self.job_id,
                access_token=self.access_token,
                log=self.log.emit,
                **self.options,
            )
            self.succeeded.emit(
                {
                    "platform": result.platform,
                    "publish_id": result.publish_id,
                    "status": result.status,
                    "remote_id": result.remote_id,
                    "remote_url": result.remote_url,
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class TikTokStatusWorker(QThread):
    log = Signal(str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, queue_path, job_id, access_token, parent=None):
        super().__init__(parent)
        self.queue_path = str(queue_path)
        self.job_id = str(job_id)
        self.access_token = str(access_token)

    def run(self):
        try:
            result = refresh_tiktok_publish_status(
                self.queue_path,
                self.job_id,
                access_token=self.access_token,
                log=self.log.emit,
            )
            self.succeeded.emit(
                {
                    "publish_id": result.publish_id,
                    "status": result.status,
                    "remote_id": result.remote_id,
                    "remote_url": result.remote_url,
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class TikTokPublishDialog(QtWidgets.QDialog):
    def __init__(self, creator, parent=None):
        super().__init__(parent)
        self.creator = dict(creator or {})
        self.setWindowTitle("TikTok Direct Post")
        self.resize(560, 420)
        self._build_ui()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        nickname = self.creator.get("creator_nickname") or ""
        username = self.creator.get("creator_username") or ""
        identity = QtWidgets.QLabel(
            f"<b>Tài khoản đích:</b> {nickname} (@{str(username).lstrip('@')})"
        )

        root.addWidget(identity)

        maximum = self.creator.get("max_video_post_duration_sec") or "?"
        root.addWidget(
            QtWidgets.QLabel(f"Giới hạn video của tài khoản: {maximum} giây")
        )

        form = QtWidgets.QFormLayout()
        self.privacy = QtWidgets.QComboBox()
        self.privacy.addItems(
            list(self.creator.get("privacy_level_options") or ["SELF_ONLY"])
        )
        private_index = self.privacy.findText("SELF_ONLY")
        if private_index >= 0:
            self.privacy.setCurrentIndex(private_index)
        form.addRow("Quyền riêng tư", self.privacy)

        self.disable_comment = QtWidgets.QCheckBox("Tắt bình luận")
        self.disable_duet = QtWidgets.QCheckBox("Tắt Duet")
        self.disable_stitch = QtWidgets.QCheckBox("Tắt Stitch")
        self.is_aigc = QtWidgets.QCheckBox(
            "Gắn nhãn nội dung tạo/chỉnh sửa bằng AI"
        )
        self.is_aigc.setChecked(True)
        self.brand_content = QtWidgets.QCheckBox(
            "Paid partnership / quảng bá bên thứ ba"
        )
        self.brand_organic = QtWidgets.QCheckBox(
            "Quảng bá doanh nghiệp/sản phẩm của chính creator"
        )

        account_flags = (
            (self.disable_comment, "comment_disabled"),
            (self.disable_duet, "duet_disabled"),
            (self.disable_stitch, "stitch_disabled"),
        )
        for checkbox, key in account_flags:
            if self.creator.get(key):
                checkbox.setChecked(True)
                checkbox.setDisabled(True)
                checkbox.setToolTip("Tài khoản TikTok đã khóa quyền này.")
            form.addRow("", checkbox)
        form.addRow("", self.is_aigc)
        form.addRow("", self.brand_content)
        form.addRow("", self.brand_organic)
        root.addLayout(form)

        note = QtWidgets.QLabel(
            "TikTok yêu cầu hiển thị đúng tài khoản, privacy và quyền tương tác "
            "trước khi đăng. Client API chưa được TikTok audit có thể chỉ đăng private."
        )
        note.setWordWrap(True)
        root.addWidget(note)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Tiếp tục")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("Hủy")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def values(self):
        return {
            "privacy": self.privacy.currentText(),
            "disable_comment": self.disable_comment.isChecked(),
            "disable_duet": self.disable_duet.isChecked(),
            "disable_stitch": self.disable_stitch.isChecked(),
            "is_aigc": self.is_aigc.isChecked(),
            "brand_content": self.brand_content.isChecked(),
            "brand_organic": self.brand_organic.isChecked(),
        }
