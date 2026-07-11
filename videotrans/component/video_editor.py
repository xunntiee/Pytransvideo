# -*- coding: utf-8 -*-
import re
import subprocess
import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from videotrans.configure.config import ROOT_DIR, settings
from videotrans.util import tools


VIDEO_EXTENSIONS = "Video files (*.mp4 *.mov *.mkv *.avi *.webm *.flv *.m4v);;All files (*)"
DEFAULT_OUTPUT_DIR = Path(ROOT_DIR) / "output" / "edited"
PREVIEW_DIR = Path(ROOT_DIR) / "tmp" / "video_editor"


def _safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "").strip(" .")
    return value[:120] or "video"


def _even(value):
    value = max(2, int(value or 2))
    return value if value % 2 == 0 else value - 1


def _clamp_region(region, width, height):
    x, y, w, h = [int(v or 0) for v in region]
    width = int(width or 0)
    height = int(height or 0)
    x = max(0, min(x, max(0, width - 2)))
    y = max(0, min(y, max(0, height - 2)))
    w = max(2, min(w, max(2, width - x)))
    h = max(2, min(h, max(2, height - y)))
    return x, y, w, h


def _clamp_delogo_region(region, width, height):
    x, y, w, h = _clamp_region(region, width, height)
    width = int(width or 0)
    height = int(height or 0)
    if width <= 4 or height <= 4:
        return x, y, w, h
    x = max(1, min(x, width - 3))
    y = max(1, min(y, height - 3))
    w = max(2, min(w, width - x - 1))
    h = max(2, min(h, height - y - 1))
    return x, y, w, h


def _default_output_path(input_path):
    input_path = Path(input_path)
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_OUTPUT_DIR / f"{_safe_name(input_path.stem)}_edited.mp4"


def _encode_args():
    return [
        "-c:v",
        "libx264",
        "-crf",
        str(settings.get("crf", 23)),
        "-preset",
        settings.get("preset", "veryfast"),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
    ]


def _vf_command(input_path, output_path, vf):
    return [
        "-i",
        input_path,
        "-vf",
        vf,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        *_encode_args(),
        output_path,
    ]


def _filter_complex_command(input_path, output_path, filter_complex):
    return [
        "-i",
        input_path,
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "0:a?",
        *_encode_args(),
        output_path,
    ]


def build_video_edit_command(input_path, output_path, mode, region, video_size, options):
    video_w, video_h = video_size
    x, y, w, h = _clamp_region(region, video_w, video_h)

    if mode == "cover":
        color = options.get("cover_color", "black")
        opacity = max(0, min(int(options.get("opacity", 90)), 100)) / 100
        return _vf_command(input_path, output_path, f"drawbox=x={x}:y={y}:w={w}:h={h}:color={color}@{opacity:.2f}:t=fill")

    if mode == "blur":
        radius = max(2, min(int(options.get("blur_radius", 18)), 80))
        power = max(1, min(int(options.get("blur_power", 2)), 6))
        filter_complex = (
            f"[0:v]split[base][tmp];"
            f"[tmp]crop={w}:{h}:{x}:{y},boxblur=luma_radius={radius}:luma_power={power}[blur];"
            f"[base][blur]overlay={x}:{y}[v]"
        )
        return _filter_complex_command(input_path, output_path, filter_complex)

    if mode == "delogo":
        x, y, w, h = _clamp_delogo_region(region, video_w, video_h)
        return _vf_command(input_path, output_path, f"delogo=x={x}:y={y}:w={w}:h={h}:show=0")

    if mode == "crop":
        ratio = options.get("ratio", "9:16")
        if ratio == "selected":
            crop_filter = f"crop={w}:{h}:{x}:{y}"
        elif ratio == "16:9":
            crop_filter = "crop=w=min(iw\\,ih*16/9):h=min(ih\\,iw*9/16):x=(iw-ow)/2:y=(ih-oh)/2"
        elif ratio == "1:1":
            crop_filter = "crop=w=min(iw\\,ih):h=min(iw\\,ih):x=(iw-ow)/2:y=(ih-oh)/2"
        else:
            crop_filter = "crop=w=min(iw\\,ih*9/16):h=min(ih\\,iw*16/9):x=(iw-ow)/2:y=(ih-oh)/2"

        scale_w = int(options.get("scale_w", 0) or 0)
        scale_h = int(options.get("scale_h", 0) or 0)
        filters = [crop_filter]
        if scale_w > 0 and scale_h > 0:
            filters.append(f"scale={_even(scale_w)}:{_even(scale_h)}:flags=lanczos")
        else:
            filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        filters.append("setsar=1")
        return _vf_command(input_path, output_path, ",".join(filters))

    raise ValueError("Chưa chọn chế độ chỉnh sửa hợp lệ.")


class VideoEditWorker(QThread):
    log = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, command, output_path, parent=None):
        super().__init__(parent)
        self.command = command
        self.output_path = output_path
        self.process = None

    def stop(self):
        self.requestInterruption()
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def run(self):
        try:
            Path(self.output_path).parent.mkdir(parents=True, exist_ok=True)
            self.log.emit("Đang render video bằng FFmpeg...")
            final_args = [str(arg) for arg in self.command]
            cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-ignore_unknown", "-threads", "0"]
            if "-y" not in final_args:
                cmd.append("-y")
            cmd.extend(final_args)
            if settings.get("ffmpeg_cmd"):
                custom_params = [p for p in settings.get("ffmpeg_cmd", "").split(" ") if p]
                cmd = cmd[:-1] + custom_params + cmd[-1:]
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            tail = []
            for line in self.process.stdout or []:
                line = line.rstrip()
                if line:
                    tail.append(line)
                    tail = tail[-12:]
                if self.isInterruptionRequested():
                    self.stop()
                    self.failed.emit("Đã dừng render.")
                    return
            return_code = self.process.wait()
            if return_code != 0:
                if self.isInterruptionRequested():
                    self.failed.emit("Đã dừng render.")
                    return
                self.failed.emit("\n".join(tail) or f"FFmpeg lỗi: {return_code}")
                return
            self.finished_ok.emit(self.output_path)
        except Exception as e:
            self.failed.emit(str(e))


class RegionPreview(QtWidgets.QWidget):
    region_changed = Signal(tuple)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(640, 360)
        self.setMouseTracking(True)
        self._pixmap = QtGui.QPixmap()
        self._selection = QtCore.QRect()
        self._drag_origin = None
        self._drag_mode = None
        self._drag_start_point = None
        self._drag_start_rect = QtCore.QRect()
        self._resize_handle = ""
        self._video_size = (0, 0)
        self._overlays = []
        self._handle_size = 10

    def set_preview(self, image_path, video_size):
        self._pixmap = QtGui.QPixmap(str(image_path))
        self._video_size = video_size
        self._selection = QtCore.QRect()
        self._drag_origin = None
        self._drag_mode = None
        self.update()

    def has_preview(self):
        return not self._pixmap.isNull()

    def set_video_region(self, region):
        if not self.has_preview():
            return
        rect = self._region_to_rect(region)
        if not rect.isValid():
            return
        self._selection = rect
        self.update()

    def set_overlays(self, overlays):
        self._overlays = list(overlays or [])
        self.update()

    def video_region(self):
        if not self.has_preview() or self._selection.isNull():
            return None
        video_w, video_h = self._video_size
        image_rect = self._image_rect()
        selection = self._selection.normalized().intersected(image_rect)
        if not selection.isValid() or selection.width() < 2 or selection.height() < 2:
            return None
        sx = video_w / image_rect.width()
        sy = video_h / image_rect.height()
        x = int((selection.left() - image_rect.left()) * sx)
        y = int((selection.top() - image_rect.top()) * sy)
        w = int(selection.width() * sx)
        h = int(selection.height() * sy)
        return _clamp_region((x, y, w, h), video_w, video_h)

    def _image_rect(self):
        if self._pixmap.isNull():
            return QtCore.QRect()
        scaled = self._pixmap.size()
        scaled.scale(self.size(), Qt.KeepAspectRatio)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        return QtCore.QRect(x, y, scaled.width(), scaled.height())

    def _region_to_rect(self, region):
        if not self.has_preview():
            return QtCore.QRect()
        x, y, w, h = region
        video_w, video_h = self._video_size
        image_rect = self._image_rect()
        if not image_rect.isValid() or not video_w or not video_h:
            return QtCore.QRect()
        sx = image_rect.width() / video_w
        sy = image_rect.height() / video_h
        return QtCore.QRect(
            int(image_rect.left() + x * sx),
            int(image_rect.top() + y * sy),
            max(1, int(w * sx)),
            max(1, int(h * sy)),
        ).normalized()

    def _clamp_point(self, point):
        rect = self._image_rect()
        return QtCore.QPoint(
            max(rect.left(), min(point.x(), rect.right())),
            max(rect.top(), min(point.y(), rect.bottom())),
        )

    def _selection_rect(self):
        if self._selection.isNull():
            return QtCore.QRect()
        return self._selection.normalized().intersected(self._image_rect())

    def _handle_rects(self):
        rect = self._selection_rect()
        if not rect.isValid():
            return {}
        size = self._handle_size
        half = size // 2
        cx = rect.center().x()
        cy = rect.center().y()
        points = {
            "tl": rect.topLeft(),
            "tr": rect.topRight(),
            "bl": rect.bottomLeft(),
            "br": rect.bottomRight(),
            "t": QtCore.QPoint(cx, rect.top()),
            "b": QtCore.QPoint(cx, rect.bottom()),
            "l": QtCore.QPoint(rect.left(), cy),
            "r": QtCore.QPoint(rect.right(), cy),
        }
        return {
            key: QtCore.QRect(point.x() - half, point.y() - half, size, size)
            for key, point in points.items()
        }

    def _hit_test_selection(self, point):
        for handle, rect in self._handle_rects().items():
            if rect.contains(point):
                return handle
        selection = self._selection_rect()
        if selection.isValid() and selection.contains(point):
            return "move"
        return ""

    def _cursor_for_hit(self, hit):
        if hit == "move":
            return Qt.CursorShape.SizeAllCursor
        if hit in {"tl", "br"}:
            return Qt.CursorShape.SizeFDiagCursor
        if hit in {"tr", "bl"}:
            return Qt.CursorShape.SizeBDiagCursor
        if hit in {"l", "r"}:
            return Qt.CursorShape.SizeHorCursor
        if hit in {"t", "b"}:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.CrossCursor

    def _clamp_rect_to_image(self, rect):
        image_rect = self._image_rect()
        rect = rect.normalized()
        if not image_rect.isValid():
            return rect
        if rect.width() >= image_rect.width():
            rect.setLeft(image_rect.left())
            rect.setRight(image_rect.right())
        else:
            if rect.left() < image_rect.left():
                rect.moveLeft(image_rect.left())
            if rect.right() > image_rect.right():
                rect.moveRight(image_rect.right())
        if rect.height() >= image_rect.height():
            rect.setTop(image_rect.top())
            rect.setBottom(image_rect.bottom())
        else:
            if rect.top() < image_rect.top():
                rect.moveTop(image_rect.top())
            if rect.bottom() > image_rect.bottom():
                rect.moveBottom(image_rect.bottom())
        return rect.intersected(image_rect)

    def _resize_rect(self, point):
        image_rect = self._image_rect()
        rect = self._drag_start_rect.normalized()
        left, right = rect.left(), rect.right()
        top, bottom = rect.top(), rect.bottom()
        if "l" in self._resize_handle:
            left = point.x()
        if "r" in self._resize_handle:
            right = point.x()
        if "t" in self._resize_handle:
            top = point.y()
        if "b" in self._resize_handle:
            bottom = point.y()

        min_size = max(4, self._handle_size)
        if left > right - min_size:
            if "l" in self._resize_handle:
                left = right - min_size
            else:
                right = left + min_size
        if top > bottom - min_size:
            if "t" in self._resize_handle:
                top = bottom - min_size
            else:
                bottom = top + min_size
        rect = QtCore.QRect(QtCore.QPoint(left, top), QtCore.QPoint(right, bottom)).normalized()
        return rect.intersected(image_rect)

    def _emit_current_region(self):
        region = self.video_region()
        if region:
            self.region_changed.emit(region)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor("#111820"))
        if self._pixmap.isNull():
            painter.setPen(QtGui.QColor("#9aa9b8"))
            painter.drawText(self.rect(), Qt.AlignCenter, "Chọn video để xem preview")
            return

        image_rect = self._image_rect()
        painter.drawPixmap(image_rect, self._pixmap)

        for overlay in self._overlays:
            rect = self._region_to_rect(overlay.get("region", (0, 0, 2, 2))).intersected(image_rect)
            if not rect.isValid():
                continue
            color = QtGui.QColor(overlay.get("color", "#f6c445"))
            fill = QtGui.QColor(color)
            fill.setAlpha(45)
            painter.fillRect(rect, fill)
            painter.setPen(QtGui.QPen(color, 2, Qt.DashLine))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            label = overlay.get("label", "")
            if label:
                label_width = max(1, min(120, rect.width() - 8))
                label_rect = QtCore.QRect(rect.left() + 4, rect.top() + 4, label_width, 22)
                painter.fillRect(label_rect, QtGui.QColor(0, 0, 0, 120))
                painter.setPen(color)
                painter.drawText(label_rect, Qt.AlignLeft | Qt.AlignVCenter, label)

        if not self._selection.isNull():
            selection = self._selection.normalized().intersected(image_rect)
            painter.fillRect(selection, QtGui.QColor(35, 130, 220, 70))
            pen = QtGui.QPen(QtGui.QColor("#32a9ff"), 2)
            painter.setPen(pen)
            painter.drawRect(selection.adjusted(0, 0, -1, -1))
            painter.setBrush(QtGui.QColor("#f7fbff"))
            painter.setPen(QtGui.QPen(QtGui.QColor("#32a9ff"), 1))
            for handle_rect in self._handle_rects().values():
                painter.drawRect(handle_rect.adjusted(0, 0, -1, -1))

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or not self.has_preview():
            return
        if not self._image_rect().contains(event.position().toPoint()):
            return
        point = self._clamp_point(event.position().toPoint())
        hit = self._hit_test_selection(point)
        self._drag_start_point = point
        self._drag_start_rect = self._selection_rect()
        if hit == "move":
            self._drag_mode = "move"
        elif hit:
            self._drag_mode = "resize"
            self._resize_handle = hit
        else:
            self._drag_mode = "draw"
            self._drag_origin = point
            self._selection = QtCore.QRect(self._drag_origin, self._drag_origin)
        self.setCursor(self._cursor_for_hit(hit))
        self.update()

    def mouseMoveEvent(self, event):
        point = self._clamp_point(event.position().toPoint())
        if self._drag_mode is None:
            self.setCursor(self._cursor_for_hit(self._hit_test_selection(point)))
            return
        if self._drag_mode == "move":
            delta = point - self._drag_start_point
            self._selection = self._clamp_rect_to_image(self._drag_start_rect.translated(delta))
        elif self._drag_mode == "resize":
            self._selection = self._resize_rect(point)
        else:
            self._selection = QtCore.QRect(self._drag_origin, point).normalized()
        self.update()
        self._emit_current_region()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._drag_mode is None:
            return
        point = self._clamp_point(event.position().toPoint())
        if self._drag_mode == "move":
            delta = point - self._drag_start_point
            self._selection = self._clamp_rect_to_image(self._drag_start_rect.translated(delta))
        elif self._drag_mode == "resize":
            self._selection = self._resize_rect(point)
        else:
            self._selection = QtCore.QRect(self._drag_origin, point).normalized()
        self._drag_origin = None
        self._drag_mode = None
        self._drag_start_point = None
        self._resize_handle = ""
        self.update()
        self._emit_current_region()


class VideoEditorWindow(QtWidgets.QWidget):
    MODES = [
        ("cover", "Che sub nhanh", "Vẽ hộp che vùng subtitle bằng drawbox."),
        ("blur", "Làm mờ sub", "Blur vùng subtitle/logo cố định."),
        ("delogo", "Delogo/sub cố định", "Dùng filter delogo của FFmpeg cho vùng cố định."),
        ("crop", "Cắt/resize/đổi tỉ lệ", "Crop theo tỉ lệ 9:16, 16:9, 1:1 hoặc vùng kéo."),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chỉnh sửa video")
        self.resize(1160, 760)
        self.worker = None
        self.video_info = {"width": 0, "height": 0, "time": 0}
        self._syncing_region = False
        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        self.setStyleSheet(
            self.styleSheet()
            + """
            QGroupBox {
                margin-top: 16px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 10px;
                padding: 0 4px;
            }
            """
        )
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        title = QtWidgets.QLabel("Từng video")
        title.setStyleSheet("font-size:18px;font-weight:700;color:#dce8f8")
        root.addWidget(title)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)

        self.mode_list = QtWidgets.QListWidget()
        self.mode_list.setFixedWidth(220)
        for mode, title, desc in self.MODES:
            item = QtWidgets.QListWidgetItem(title)
            item.setData(Qt.UserRole, mode)
            item.setToolTip(desc)
            self.mode_list.addItem(item)
        self.mode_list.setCurrentRow(0)
        body.addWidget(self.mode_list)

        panel = QtWidgets.QVBoxLayout()
        panel.setSpacing(10)
        body.addLayout(panel, 1)

        file_group = QtWidgets.QGroupBox("Nguồn và đầu ra")
        file_grid = QtWidgets.QGridLayout(file_group)
        file_grid.setContentsMargins(10, 24, 10, 10)
        file_grid.setColumnStretch(1, 1)
        file_grid.setHorizontalSpacing(8)
        self.input_path = QtWidgets.QLineEdit()
        self.input_path.setPlaceholderText("Chọn video cần chỉnh sửa...")
        self.input_btn = QtWidgets.QPushButton("Chọn video")
        self.input_btn.setMinimumWidth(110)
        self.output_path = QtWidgets.QLineEdit()
        self.output_btn = QtWidgets.QPushButton("Lưu thành")
        self.output_btn.setMinimumWidth(90)
        self.open_output_btn = QtWidgets.QPushButton("Mở thư mục")
        self.open_output_btn.setMinimumWidth(95)
        file_grid.addWidget(QtWidgets.QLabel("Video"), 0, 0)
        file_grid.addWidget(self.input_path, 0, 1)
        file_grid.addWidget(self.input_btn, 0, 2)
        file_grid.addWidget(QtWidgets.QLabel("Output"), 1, 0)
        file_grid.addWidget(self.output_path, 1, 1)
        file_grid.addWidget(self.output_btn, 1, 2)
        file_grid.addWidget(self.open_output_btn, 1, 3)
        panel.addWidget(file_group)

        preview_row = QtWidgets.QHBoxLayout()
        self.preview = RegionPreview()
        preview_row.addWidget(self.preview, 1)

        side_wrap = QtWidgets.QWidget()
        side_wrap.setMinimumWidth(300)
        side_wrap.setMaximumWidth(360)
        side = QtWidgets.QVBoxLayout()
        side_wrap.setLayout(side)
        side.setSpacing(8)
        self.info_label = QtWidgets.QLabel("Chưa chọn video")
        self.info_label.setWordWrap(True)
        self.frame_slider = QtWidgets.QSlider(Qt.Horizontal)
        self.frame_slider.setRange(0, 0)
        self.frame_time = QtWidgets.QSpinBox()
        self.frame_time.setRange(0, 0)
        self.frame_time.setSuffix(" s")
        self.preview_btn = QtWidgets.QPushButton("Lấy preview")
        time_row = QtWidgets.QHBoxLayout()
        time_row.addWidget(self.frame_slider, 1)
        time_row.addWidget(self.frame_time)
        side.addWidget(self.info_label)
        side.addLayout(time_row)
        side.addWidget(self.preview_btn)

        region_group = QtWidgets.QGroupBox("Vùng xử lý")
        region_grid = QtWidgets.QGridLayout(region_group)
        region_grid.setContentsMargins(10, 24, 10, 10)
        self.x_spin = self._coord_spin()
        self.y_spin = self._coord_spin()
        self.w_spin = self._coord_spin(minimum=2)
        self.h_spin = self._coord_spin(minimum=2)
        self.bottom_sub_btn = QtWidgets.QPushButton("Vùng sub dưới")
        self.full_width_btn = QtWidgets.QPushButton("Ngang toàn video")
        region_grid.addWidget(QtWidgets.QLabel("X"), 0, 0)
        region_grid.addWidget(self.x_spin, 0, 1)
        region_grid.addWidget(QtWidgets.QLabel("Y"), 0, 2)
        region_grid.addWidget(self.y_spin, 0, 3)
        region_grid.addWidget(QtWidgets.QLabel("W"), 1, 0)
        region_grid.addWidget(self.w_spin, 1, 1)
        region_grid.addWidget(QtWidgets.QLabel("H"), 1, 2)
        region_grid.addWidget(self.h_spin, 1, 3)
        region_grid.addWidget(self.bottom_sub_btn, 2, 0, 1, 2)
        region_grid.addWidget(self.full_width_btn, 2, 2, 1, 2)
        side.addWidget(region_group)

        self.options_stack = QtWidgets.QStackedWidget()
        self.options_stack.addWidget(self._cover_options())
        self.options_stack.addWidget(self._blur_options())
        self.options_stack.addWidget(self._delogo_options())
        self.options_stack.addWidget(self._crop_options())
        side.addWidget(self.options_stack)
        side.addStretch()

        preview_row.addWidget(side_wrap)
        panel.addLayout(preview_row, 1)

        bottom = QtWidgets.QHBoxLayout()
        self.status_label = QtWidgets.QLabel("")
        self.render_btn = QtWidgets.QPushButton("Render")
        self.render_btn.setMinimumWidth(120)
        self.stop_btn = QtWidgets.QPushButton("Dừng")
        self.stop_btn.setDisabled(True)
        bottom.addWidget(self.status_label, 1)
        bottom.addWidget(self.stop_btn)
        bottom.addWidget(self.render_btn)
        panel.addLayout(bottom)

        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(120)
        panel.addWidget(self.log_edit)

    def _coord_spin(self, minimum=0):
        spin = QtWidgets.QSpinBox()
        spin.setRange(minimum, 20000)
        return spin

    def _cover_options(self):
        box = QtWidgets.QGroupBox("Che sub nhanh")
        layout = QtWidgets.QFormLayout(box)
        layout.setContentsMargins(10, 24, 10, 10)
        self.cover_color = QtWidgets.QComboBox()
        self.cover_color.addItems(["black", "white", "gray"])
        self.cover_opacity = QtWidgets.QSpinBox()
        self.cover_opacity.setRange(0, 100)
        self.cover_opacity.setValue(90)
        self.cover_opacity.setSuffix("%")
        layout.addRow("Màu che", self.cover_color)
        layout.addRow("Độ đậm", self.cover_opacity)
        return box

    def _blur_options(self):
        box = QtWidgets.QGroupBox("Làm mờ sub")
        layout = QtWidgets.QFormLayout(box)
        layout.setContentsMargins(10, 24, 10, 10)
        self.blur_radius = QtWidgets.QSpinBox()
        self.blur_radius.setRange(2, 80)
        self.blur_radius.setValue(18)
        self.blur_power = QtWidgets.QSpinBox()
        self.blur_power.setRange(1, 6)
        self.blur_power.setValue(2)
        layout.addRow("Blur radius", self.blur_radius)
        layout.addRow("Blur power", self.blur_power)
        return box

    def _delogo_options(self):
        box = QtWidgets.QGroupBox("Delogo/sub cố định")
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(10, 24, 10, 10)
        note = QtWidgets.QLabel("Hợp với logo/sub ở một vị trí cố định. Vùng quá lớn có thể tạo vệt nhòe.")
        note.setWordWrap(True)
        layout.addWidget(note)
        return box

    def _crop_options(self):
        box = QtWidgets.QGroupBox("Cắt/resize/đổi tỉ lệ")
        layout = QtWidgets.QFormLayout(box)
        layout.setContentsMargins(10, 24, 10, 10)
        self.crop_ratio = QtWidgets.QComboBox()
        self.crop_ratio.addItem("9:16 dọc", "9:16")
        self.crop_ratio.addItem("16:9 ngang", "16:9")
        self.crop_ratio.addItem("1:1 vuông", "1:1")
        self.crop_ratio.addItem("Theo vùng kéo", "selected")
        self.resize_preset = QtWidgets.QComboBox()
        self.resize_preset.addItem("Giữ kích thước sau crop", (0, 0))
        self.resize_preset.addItem("1080 x 1920", (1080, 1920))
        self.resize_preset.addItem("1920 x 1080", (1920, 1080))
        self.resize_preset.addItem("1080 x 1080", (1080, 1080))
        self.resize_preset.addItem("Tùy chỉnh", None)
        self.scale_w = QtWidgets.QSpinBox()
        self.scale_w.setRange(0, 7680)
        self.scale_w.setSingleStep(2)
        self.scale_h = QtWidgets.QSpinBox()
        self.scale_h.setRange(0, 7680)
        self.scale_h.setSingleStep(2)
        size_row = QtWidgets.QHBoxLayout()
        size_row.addWidget(self.scale_w)
        size_row.addWidget(QtWidgets.QLabel("x"))
        size_row.addWidget(self.scale_h)
        layout.addRow("Tỉ lệ", self.crop_ratio)
        layout.addRow("Resize", self.resize_preset)
        layout.addRow("Kích thước", size_row)
        return box

    def _connect_signals(self):
        self.mode_list.currentRowChanged.connect(self._mode_changed)
        self.input_btn.clicked.connect(self._choose_input)
        self.output_btn.clicked.connect(self._choose_output)
        self.open_output_btn.clicked.connect(self._open_output_dir)
        self.preview_btn.clicked.connect(self._load_preview_frame)
        self.frame_slider.valueChanged.connect(self.frame_time.setValue)
        self.frame_time.valueChanged.connect(self.frame_slider.setValue)
        self.preview.region_changed.connect(self._region_from_preview)
        self.bottom_sub_btn.clicked.connect(self._set_bottom_sub_region)
        self.full_width_btn.clicked.connect(self._set_full_width_region)
        self.render_btn.clicked.connect(self._start_render)
        self.stop_btn.clicked.connect(self._stop_render)
        self.resize_preset.currentIndexChanged.connect(self._resize_preset_changed)
        for spin in (self.x_spin, self.y_spin, self.w_spin, self.h_spin):
            spin.valueChanged.connect(self._region_from_spins)

    def _mode_changed(self, row):
        self.options_stack.setCurrentIndex(max(0, row))

    def _choose_input(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Chọn video", settings.get("last_opendir", ""), VIDEO_EXTENSIONS)
        if not filename:
            return
        self.input_path.setText(filename.replace("\\", "/"))
        settings["last_opendir"] = str(Path(filename).parent)
        settings.save()
        self.output_path.setText(str(_default_output_path(filename)).replace("\\", "/"))
        self._read_video_info()
        self._set_bottom_sub_region()
        self._load_preview_frame()

    def _choose_output(self):
        default_path = self.output_path.text().strip() or str(_default_output_path(self.input_path.text().strip()))
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Lưu video thành", default_path, "MP4 video (*.mp4);;All files (*)")
        if filename:
            if not Path(filename).suffix:
                filename += ".mp4"
            self.output_path.setText(filename.replace("\\", "/"))

    def _read_video_info(self):
        path = self.input_path.text().strip()
        if not path:
            return
        try:
            info = tools.get_video_info(path)
            self.video_info = info
            width, height = info.get("width", 0), info.get("height", 0)
            seconds = max(0, int((info.get("time", 0) or 0) / 1000))
            self.frame_slider.setRange(0, seconds)
            self.frame_time.setRange(0, seconds)
            self.info_label.setText(f"{width} x {height} | {seconds}s")
            self._set_region_limits(width, height)
        except Exception as e:
            self._set_status(f"Lỗi đọc video: {e}")

    def _set_region_limits(self, width, height):
        self.x_spin.setRange(0, max(0, width - 2))
        self.y_spin.setRange(0, max(0, height - 2))
        self.w_spin.setRange(2, max(2, width))
        self.h_spin.setRange(2, max(2, height))

    def _load_preview_frame(self):
        path = self.input_path.text().strip()
        if not path or not Path(path).exists():
            QtWidgets.QMessageBox.warning(self, "Chỉnh sửa video", "Hãy chọn video hợp lệ.")
            return
        if not self.video_info.get("width"):
            self._read_video_info()
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        preview_path = PREVIEW_DIR / f"{_safe_name(Path(path).stem)}_{self.frame_time.value()}.jpg"
        try:
            tools.runffmpeg(
                [
                    "-ss",
                    str(self.frame_time.value()),
                    "-i",
                    path,
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(preview_path),
                ],
                force_cpu=True,
            )
            self.preview.set_preview(preview_path, (self.video_info.get("width", 0), self.video_info.get("height", 0)))
            self.preview.set_video_region(self._current_region())
            self._set_status("Đã lấy preview. Kéo trong khung để di chuyển, kéo góc/cạnh để resize.")
        except Exception as e:
            self._set_status(f"Lỗi lấy preview: {e}")

    def _current_mode(self):
        item = self.mode_list.currentItem()
        return item.data(Qt.UserRole) if item else "cover"

    def _current_region(self):
        return self.x_spin.value(), self.y_spin.value(), self.w_spin.value(), self.h_spin.value()

    def _region_from_preview(self, region):
        self._set_region(region, update_preview=False)

    def _region_from_spins(self):
        if self._syncing_region:
            return
        if self.preview.has_preview():
            self.preview.set_video_region(self._current_region())

    def _set_region(self, region, update_preview=True):
        width = self.video_info.get("width", 0)
        height = self.video_info.get("height", 0)
        region = _clamp_region(region, width, height)
        self._syncing_region = True
        self.x_spin.setValue(region[0])
        self.y_spin.setValue(region[1])
        self.w_spin.setValue(region[2])
        self.h_spin.setValue(region[3])
        self._syncing_region = False
        if update_preview and self.preview.has_preview():
            self.preview.set_video_region(region)

    def _set_bottom_sub_region(self):
        width = self.video_info.get("width", 0)
        height = self.video_info.get("height", 0)
        if not width or not height:
            return
        region_h = max(48, int(height * 0.18))
        self._set_region((0, height - region_h, width, region_h))

    def _set_full_width_region(self):
        width = self.video_info.get("width", 0)
        if not width:
            return
        x, y, _w, h = self._current_region()
        self._set_region((0, y, width, h))

    def _resize_preset_changed(self):
        value = self.resize_preset.currentData()
        if value is None:
            return
        width, height = value
        self.scale_w.setValue(width)
        self.scale_h.setValue(height)

    def _render_options(self):
        return {
            "cover_color": self.cover_color.currentText(),
            "opacity": self.cover_opacity.value(),
            "blur_radius": self.blur_radius.value(),
            "blur_power": self.blur_power.value(),
            "ratio": self.crop_ratio.currentData(),
            "scale_w": self.scale_w.value(),
            "scale_h": self.scale_h.value(),
        }

    def _start_render(self):
        if self.worker and self.worker.isRunning():
            return
        input_path = self.input_path.text().strip()
        output_path = self.output_path.text().strip()
        if not input_path or not Path(input_path).exists():
            QtWidgets.QMessageBox.warning(self, "Chỉnh sửa video", "Hãy chọn video hợp lệ.")
            return
        if not output_path:
            output_path = str(_default_output_path(input_path))
            self.output_path.setText(output_path)
        if not self.video_info.get("width"):
            self._read_video_info()

        try:
            command = build_video_edit_command(
                input_path,
                output_path,
                self._current_mode(),
                self._current_region(),
                (self.video_info.get("width", 0), self.video_info.get("height", 0)),
                self._render_options(),
            )
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Chỉnh sửa video", str(e))
            return

        self._set_busy(True)
        self._append_log("FFmpeg: " + " ".join(str(part) for part in command))
        self.worker = VideoEditWorker(command, output_path, parent=self)
        self.worker.log.connect(self._append_log)
        self.worker.finished_ok.connect(self._render_done)
        self.worker.failed.connect(self._render_failed)
        self.worker.finished.connect(lambda: self._set_busy(False))
        self.worker.start()

    def _stop_render(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self._append_log("Đang dừng FFmpeg...")

    def _render_done(self, output_path):
        self._set_status(f"Hoàn tất: {output_path}")
        QtWidgets.QMessageBox.information(self, "Chỉnh sửa video", "Render xong.")

    def _render_failed(self, text):
        self._set_status(f"Lỗi render: {text}")
        QtWidgets.QMessageBox.warning(self, "Chỉnh sửa video", text)

    def _set_busy(self, busy):
        self.render_btn.setDisabled(busy)
        self.stop_btn.setDisabled(not busy)
        self.input_btn.setDisabled(busy)
        self.output_btn.setDisabled(busy)
        self.preview_btn.setDisabled(busy)

    def _open_output_dir(self):
        path = self.output_path.text().strip()
        folder = Path(path).parent if path else DEFAULT_OUTPUT_DIR
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _append_log(self, text):
        if not text:
            return
        self.log_edit.appendPlainText(text)

    def _set_status(self, text):
        self.status_label.setText(text)
        self._append_log(text)
