# -*- coding: utf-8 -*-
import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from videotrans.component.video_editor import (
    DEFAULT_OUTPUT_DIR,
    PREVIEW_DIR,
    VIDEO_EXTENSIONS,
    RegionPreview,
    VideoEditorWindow,
    _clamp_region,
    _default_output_path,
    _filter_complex_command,
    _safe_name,
    _vf_command,
)
from videotrans.configure.config import ROOT_DIR, params, settings
from videotrans.util.help_srt import get_subtitle_from_srt
from videotrans.util import tools


SRT_EXTENSIONS = "Subtitle files (*.srt);;All files (*)"
PLAN_VERSION = 3


def _norm_stem(path):
    stem = Path(path).stem.lower()
    stem = re.sub(r"[\s._-]+", "", stem)
    stem = re.sub(r"(subtitle|sub|srt|origin|original|source|target|vi|vn|zh|cn)$", "", stem)
    return stem


def _short_path(path):
    if not path:
        return ""
    return Path(path).name


def _read_text(path):
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "cp1258"):
        try:
            return Path(path).read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def _ass_time(value):
    value = str(value or "00:00:00,000").strip().replace(".", ",")
    match = re.match(r"(\d+):(\d+):(\d+),(\d+)", value)
    if not match:
        return "0:00:00.00"
    hour, minute, second, milli = match.groups()
    centisecond = int((milli + "000")[:3]) // 10
    return f"{int(hour)}:{int(minute):02}:{int(second):02}.{centisecond:02}"


def _ass_escape_text(value):
    text = str(value or "").replace("\r", "").strip()
    text = text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
    return re.sub(r"\n+", r"\\N", text)


def _ass_color(color_name):
    colors = {
        "white": "&H00FFFFFF&",
        "yellow": "&H0000FFFF&",
        "cyan": "&H00FFFF00&",
        "black": "&H00000000&",
    }
    return colors.get(str(color_name or "").lower(), "&H00FFFFFF&")


def _ffmpeg_filter_path(path):
    value = Path(path).resolve().as_posix()
    return value.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _default_subtitle_style(width, height):
    width = int(width or 0)
    height = int(height or 0)
    if width < 2 or height < 2:
        return {
            "enabled": True,
            "region": [0, 0, 2, 2],
            "font_name": "Arial",
            "font_size": 42,
            "color": "white",
            "outline": 3,
        }
    region_h = max(58, int(height * 0.13))
    region_w = max(2, int(width * 0.82))
    x = max(0, int((width - region_w) / 2))
    y = max(0, height - int(height * 0.22) - region_h)
    return {
        "enabled": True,
        "region": [x, y, region_w, region_h],
        "font_name": "Arial",
        "font_size": max(24, min(72, int(height * 0.055))),
        "color": "white",
        "outline": 3,
    }


def _write_ass_from_srt(srt_path, ass_path, style, video_size):
    width, height = [int(v or 0) for v in video_size]
    region = style.get("region") or _default_subtitle_style(width, height)["region"]
    x, y, w, h = _clamp_region(region, width, height)
    margin_l = max(0, x)
    margin_r = max(0, width - x - w)
    margin_v = max(0, height - y - h)
    font_name = str(style.get("font_name") or "Arial").replace(",", " ")
    font_size = max(10, min(int(style.get("font_size", 42)), 160))
    outline = max(0, min(int(style.get("outline", 3)), 12))
    primary = _ass_color(style.get("color", "white"))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {max(width, 2)}",
        f"PlayResY: {max(height, 2)}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font_name},{font_size},{primary},&H00FFFFFF&,&H00000000&,&H64000000&,"
        f"-1,0,0,0,100,100,0,0,1,{outline},0,2,{margin_l},{margin_r},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for item in get_subtitle_from_srt(str(srt_path), is_file=True):
        text = _ass_escape_text(item.get("text", ""))
        if not text:
            continue
        lines.append(f"Dialogue: 0,{_ass_time(item.get('startraw'))},{_ass_time(item.get('endraw'))},Default,,0,0,0,,{text}")
    Path(ass_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_burn_subtitle_command(input_path, output_path, ass_path):
    return _vf_command(input_path, output_path, f"ass='{_ffmpeg_filter_path(ass_path)}'")


def match_srt_video_files(srt_files, video_files):
    videos_by_key = {}
    for video in video_files:
        videos_by_key.setdefault(_norm_stem(video), []).append(video)

    used_videos = set()
    rows = []
    for index, srt in enumerate(srt_files, start=1):
        key = _norm_stem(srt)
        video = ""
        candidates = videos_by_key.get(key, [])
        for candidate in candidates:
            if candidate not in used_videos:
                video = candidate
                break
        if video:
            used_videos.add(video)
        rows.append(_make_batch_item(index=index, srt=srt, video=video))

    for video in video_files:
        if video not in used_videos:
            rows.append(_make_batch_item(index=len(rows) + 1, srt="", video=video))
    return rows


def _make_batch_item(index, srt="", video=""):
    return {
        "index": index,
        "srt": str(srt).replace("\\", "/") if srt else "",
        "video": str(video).replace("\\", "/") if video else "",
        "translated_srt": "",
        "status": ("Sẵn sàng" if srt and video else ("Thiếu video" if srt else ("Sẵn sàng - tự lấy SRT" if video else "Thiếu video"))),
        "edit": {},
    }


def _default_edit_for_video(width, height):
    width = int(width or 0)
    height = int(height or 0)
    if width < 2 or height < 2:
        return {
            "enabled": True,
            "blur": True,
            "cover": False,
            "region": [0, 0, 2, 2],
            "subtitle": _default_subtitle_style(width, height),
            "blur_radius": 18,
            "blur_power": 2,
            "cover_color": "black",
            "cover_opacity": 35,
            "preview_second": 1,
        }
    region_h = max(48, int(height * 0.18))
    return {
        "enabled": True,
        "blur": True,
        "cover": False,
        "region": [0, height - region_h, width, region_h],
        "subtitle": _default_subtitle_style(width, height),
        "blur_radius": 18,
        "blur_power": 2,
        "cover_color": "black",
        "cover_opacity": 35,
        "preview_second": 1,
    }


def build_batch_edit_command(input_path, output_path, edit, video_size):
    width, height = video_size
    region = edit.get("region") or _default_edit_for_video(width, height)["region"]
    x, y, w, h = _clamp_region(region, width, height)
    blur = bool(edit.get("blur", True))
    cover = bool(edit.get("cover", False))
    if not blur and not cover:
        return [
            "-i",
            input_path,
            "-map",
            "0",
            "-c",
            "copy",
            output_path,
        ]

    if blur:
        radius = max(2, min(int(edit.get("blur_radius", 18)), 80))
        power = max(1, min(int(edit.get("blur_power", 2)), 6))
        filter_complex = (
            f"[0:v]split[base][tmp];"
            f"[tmp]crop={w}:{h}:{x}:{y},boxblur=luma_radius={radius}:luma_power={power}[blur];"
            f"[base][blur]overlay={x}:{y}[v0]"
        )
        if cover:
            color = edit.get("cover_color", "black")
            opacity = max(0, min(int(edit.get("cover_opacity", 35)), 100)) / 100
            filter_complex += f";[v0]drawbox=x={x}:y={y}:w={w}:h={h}:color={color}@{opacity:.2f}:t=fill[v]"
        else:
            filter_complex += ";[v0]copy[v]"
        return _filter_complex_command(input_path, output_path, filter_complex)

    color = edit.get("cover_color", "black")
    opacity = max(0, min(int(edit.get("cover_opacity", 35)), 100)) / 100
    return _vf_command(input_path, output_path, f"drawbox=x={x}:y={y}:w={w}:h={h}:color={color}@{opacity:.2f}:t=fill")


class BatchWorkflowWorker(QThread):
    log = Signal(str)
    progress = Signal(int)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, items, output_dir, ai_config, parent=None):
        super().__init__(parent)
        self.items = copy.deepcopy(items)
        self.output_dir = Path(output_dir)
        self.ai_config = copy.deepcopy(ai_config)
        self.process = None
        self.gemini = None

    def stop(self):
        self.requestInterruption()
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def run(self):
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            runnable = [it for it in self.items if it.get("video")]
            if not runnable:
                raise RuntimeError("Không có video hợp lệ.")
            plan_path = self.output_dir / "batch_vietsub_plan.json"
            self._write_plan(plan_path)
            self.log.emit(f"Đã lưu plan: {plan_path}")

            for offset, item in enumerate(runnable, start=1):
                if self.isInterruptionRequested():
                    self.failed.emit("Đã dừng xử lý.")
                    return
                self._process_item(item, offset, len(runnable))
                self._write_plan(plan_path)
                self.progress.emit(int(offset * 100 / len(runnable)))
            self.finished_ok.emit(str(self.output_dir))
        except Exception as e:
            self.failed.emit(str(e))

    def _write_plan(self, plan_path):
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            json.dumps(
                {
                    "version": PLAN_VERSION,
                    "ai": self.ai_config,
                    "items": self.items,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _artifact_ready(self, path):
        artifact = Path(path)
        if self.ai_config.get("force_rebuild"):
            return False
        return artifact.is_file() and artifact.stat().st_size > 0

    def _mark_stage(self, item, stage, artifact="", status="completed", detail=""):
        data = {"status": status}
        if artifact:
            data["artifact"] = str(artifact).replace("\\", "/")
        if detail:
            data["detail"] = str(detail)
        item.setdefault("stages", {})[stage] = data
        self._write_plan(self.output_dir / "batch_vietsub_plan.json")

    def _translate_item_if_needed(self, item, srt, item_dir, offset, total):
        mode = self.ai_config.get("translation_mode")
        if mode == "api_llm":
            from videotrans.process.batch_pipeline import translate_srt_api

            item["status"] = "\u0110ang d\u1ecbch qua API / LLM"
            self.log.emit(f"[{offset}/{total}] D\u1ecbch API/LLM: {Path(srt).name}")
            result = translate_srt_api(
                srt,
                item_dir,
                source_language=self.ai_config.get("source_language"),
                target_language=self.ai_config.get("subtitle_language"),
                translate_type=self.ai_config.get("translate_type", 0),
                force=bool(self.ai_config.get("force_retranslate")),
            )
            if result:
                self.log.emit(f"[{offset}/{total}] D\u1ecbch API/LLM xong: {Path(result).name}")
            return result

        if mode != "chrome_gemini_gem":
            return None
        if self.isInterruptionRequested():
            raise RuntimeError("\u0110\u00e3 d\u1eebng tr\u01b0\u1edbc khi d\u1ecbch Gemini.")
        from videotrans.component.gemini_browser_automation import GeminiBrowserAutomation

        if not self.gemini:
            self.gemini = GeminiBrowserAutomation(
                gem_url=self.ai_config.get("gem_url") or None,
                log=self.log.emit,
            )
        target = Path(item_dir) / f"{_safe_name(Path(srt).stem)}_vi.srt"
        item["status"] = "\u0110ang d\u1ecbch Gemini"
        self.log.emit(f"[{offset}/{total}] D\u1ecbch Gemini: {Path(srt).name} -> {target}")
        result = self.gemini.translate_srt(
            srt,
            target,
            force=bool(self.ai_config.get("force_retranslate")),
        )
        self.log.emit(f"[{offset}/{total}] Gemini xong: {Path(result).name}")
        return result

    def _process_item_pipeline(self, item, offset, total):
        from videotrans.process.batch_pipeline import (
            build_dub_mux_command,
            dub_srt,
            language_code,
            transcribe_video,
        )

        video = Path(item.get("video") or "")
        if not video.is_file():
            raise RuntimeError(f"Thi\u1ebfu video \u0111\u1ea7u v\u00e0o: {video}")

        item_dir = self.output_dir / _safe_name(video.stem)
        item_dir.mkdir(parents=True, exist_ok=True)
        self.log.emit(f"[{offset}/{total}] B\u1eaft \u0111\u1ea7u pipeline: {video.name}")

        source_srt = item_dir / f"{_safe_name(video.stem)}.source.srt"
        source_value = item.get("srt")
        if source_value:
            supplied_srt = Path(source_value)
            if not supplied_srt.is_file():
                raise RuntimeError(f"SRT kh\u00f4ng t\u1ed3n t\u1ea1i: {supplied_srt}")
            if not self._artifact_ready(source_srt):
                source_srt.write_text(_read_text(supplied_srt), encoding="utf-8")
            self._mark_stage(item, "recognition", source_srt, detail="D\u00f9ng SRT \u0111\u00e3 cung c\u1ea5p")
        else:
            if not self.ai_config.get("auto_stt", True):
                raise RuntimeError("Video ch\u01b0a c\u00f3 SRT v\u00e0 t\u00ednh n\u0103ng t\u1ef1 nh\u1eadn d\u1ea1ng \u0111ang t\u1eaft.")
            if self._artifact_ready(source_srt):
                self.log.emit(f"[{offset}/{total}] D\u00f9ng l\u1ea1i SRT \u0111\u00e3 nh\u1eadn d\u1ea1ng: {source_srt.name}")
            else:
                item["status"] = "\u0110ang nh\u1eadn d\u1ea1ng gi\u1ecdng n\u00f3i"
                self.log.emit(f"[{offset}/{total}] T\u00e1ch audio v\u00e0 nh\u1eadn d\u1ea1ng subtitle...")
                recognized_srt = transcribe_video(
                    video,
                    item_dir / "recognition",
                    source_language=self.ai_config.get("source_language", "auto"),
                    recogn_type=self.ai_config.get("recogn_type", 0),
                    model_name=self.ai_config.get("recogn_model", ""),
                    remove_noise=bool(self.ai_config.get("stt_remove_noise", False)),
                    enable_diariz=bool(self.ai_config.get("stt_diarization", False)),
                    nums_diariz=self.ai_config.get("stt_speakers", 0),
                    is_cuda=bool(self.ai_config.get("stt_cuda", False)),
                    force=bool(self.ai_config.get("force_rebuild", False)),
                )
                source_srt.write_text(_read_text(recognized_srt), encoding="utf-8")
            item["recognized_srt"] = source_srt.as_posix()
            self._mark_stage(item, "recognition", source_srt)

        item["source_srt"] = source_srt.as_posix()
        self._mark_stage(item, "source", source_srt)

        translated_srt = self._translate_item_if_needed(item, source_srt, item_dir, offset, total)
        if translated_srt:
            translated_srt = Path(translated_srt)
            item["translated_srt"] = translated_srt.as_posix()
            self._mark_stage(item, "translate", translated_srt)
        else:
            translated_srt = None
            self._mark_stage(item, "translate", source_srt, detail="Gi\u1eef nguy\u00ean SRT ngu\u1ed3n")

        info = tools.get_video_info(str(video))
        width, height = info.get("width", 0), info.get("height", 0)
        edit = item.get("edit") or {}
        if not edit.get("region"):
            defaults = _default_edit_for_video(width, height)
            defaults.update(edit)
            edit = defaults
            item["edit"] = edit

        working_video = video
        if edit.get("enabled", True):
            edited_video = item_dir / f"{_safe_name(video.stem)}_edited.mp4"
            if not self._artifact_ready(edited_video):
                item["status"] = "\u0110ang che/blur video"
                self.log.emit(f"[{offset}/{total}] Che/blur subtitle c\u0169...")
                command = build_batch_edit_command(str(video), str(edited_video), edit, (width, height))
                self._run_ffmpeg(command)
            else:
                self.log.emit(f"[{offset}/{total}] D\u00f9ng l\u1ea1i video \u0111\u00e3 edit: {edited_video.name}")
            working_video = edited_video
            item["edited_video"] = edited_video.as_posix()
            self._mark_stage(item, "edit", edited_video)
        else:
            self._mark_stage(item, "edit", video, detail="B\u1ecf qua edit")

        subtitle_source = translated_srt or source_srt
        subtitle_style = edit.get("subtitle") or _default_subtitle_style(width, height)
        if subtitle_style.get("enabled", True):
            ass_path = item_dir / f"{_safe_name(video.stem)}_styled.ass"
            subtitled_video = item_dir / f"{_safe_name(video.stem)}_subtitled.mp4"
            if not self._artifact_ready(subtitled_video):
                _write_ass_from_srt(subtitle_source, ass_path, subtitle_style, (width, height))
                item["status"] = "\u0110ang burn subtitle"
                self.log.emit(f"[{offset}/{total}] Burn subtitle m\u1edbi...")
                self._run_ffmpeg(build_burn_subtitle_command(str(working_video), str(subtitled_video), ass_path))
            else:
                self.log.emit(f"[{offset}/{total}] D\u00f9ng l\u1ea1i video \u0111\u00e3 burn sub: {subtitled_video.name}")
            working_video = subtitled_video
            item["ass_subtitle"] = ass_path.as_posix()
            self._mark_stage(item, "subtitle", subtitled_video)
        else:
            self._mark_stage(item, "subtitle", working_video, detail="Kh\u00f4ng burn subtitle")

        voice_language = self.ai_config.get("voice_language")
        voice_code = language_code(voice_language)
        source_code = language_code(self.ai_config.get("source_language"))
        if voice_code and not translated_srt and voice_code != source_code:
            raise RuntimeError("Ng\u00f4n ng\u1eef l\u1ed3ng ti\u1ebfng kh\u00e1c SRT ngu\u1ed3n. H\u00e3y b\u1eadt d\u1ecbch subtitle tr\u01b0\u1edbc khi l\u1ed3ng ti\u1ebfng.")

        if voice_code:
            dubbed_audio = dub_srt(
                subtitle_source,
                item_dir / "dubbing",
                target_language=voice_language,
                tts_type=self.ai_config.get("tts_type", 0),
                voice_role=self.ai_config.get("voice", ""),
                sync_mode=self.ai_config.get("sync_mode", "auto"),
                threads=self.ai_config.get("tts_threads", 1),
                is_cuda=bool(self.ai_config.get("is_cuda", False)),
                force=bool(self.ai_config.get("force_rebuild", False)),
            )
            item["dubbed_audio"] = Path(dubbed_audio).as_posix()
            self._mark_stage(item, "dubbing", dubbed_audio)

            audio_mode = self.ai_config.get("audio_mode", "replace")
            if audio_mode != "voice_only":
                dubbed_video = item_dir / f"{_safe_name(video.stem)}_final_dubbed.mp4"
                if not self._artifact_ready(dubbed_video):
                    item["status"] = "\u0110ang gh\u00e9p gi\u1ecdng \u0111\u1ecdc"
                    self._run_ffmpeg(
                        build_dub_mux_command(
                            working_video,
                            dubbed_audio,
                            dubbed_video,
                            audio_mode=audio_mode,
                            has_original_audio=bool(info.get("streams_audio", 0)),
                            original_volume=self.ai_config.get("original_volume", 0.15),
                        )
                    )
                working_video = dubbed_video
                self._mark_stage(item, "mux", dubbed_video)

        item["final_video"] = Path(working_video).as_posix()
        if self.ai_config.get("prepare_reup"):
            self._prepare_publish_jobs(
                item,
                working_video,
                video,
                item_dir,
                width,
                height,
                offset,
                total,
            )

        item["status"] = "Ho\u00e0n t\u1ea5t"
        self._mark_stage(item, "complete", working_video)
        self.log.emit(f"[{offset}/{total}] Ho\u00e0n t\u1ea5t: {working_video}")

    def _prepare_publish_jobs(self, item, working_video, source_video, item_dir, width, height, offset, total):
        from videotrans.process.batch_pipeline import build_vertical_reframe_command
        from videotrans.process.publish_queue import (
            SHORT_FORM_PLATFORMS,
            enqueue_publish_job,
            normalize_platforms,
        )

        selected_platforms = normalize_platforms(self.ai_config.get("reup_platforms") or [])
        queue_path = self.output_dir / "reup_queue.json"
        title = str(
            self.ai_config.get("reup_title")
            or item.get("source_title")
            or Path(working_video).stem
        ).replace("{stem}", source_video.stem)
        variants = []
        short_platforms = [platform for platform in selected_platforms if platform in SHORT_FORM_PLATFORMS]
        other_platforms = [platform for platform in selected_platforms if platform not in SHORT_FORM_PLATFORMS]
        should_reframe = (
            bool(self.ai_config.get("reup_vertical", True))
            and width
            and height
            and width > height
            and short_platforms
        )
        if should_reframe:
            vertical_video = item_dir / f"{_safe_name(Path(working_video).stem)}_9x16.mp4"
            if not self._artifact_ready(vertical_video):
                item["status"] = "Đang tạo bản 9:16"
                self.log.emit(f"[{offset}/{total}] Tạo bản 9:16 cho short-form...")
                self._run_ffmpeg(
                    build_vertical_reframe_command(
                        working_video,
                        vertical_video,
                        strategy=self.ai_config.get("reup_vertical_strategy", "blur"),
                    )
                )
            item["vertical_video"] = vertical_video.as_posix()
            self._mark_stage(item, "vertical_reframe", vertical_video)
            variants.append((vertical_video, short_platforms, 1080, 1920))
            if other_platforms:
                variants.append((Path(working_video), other_platforms, width, height))
        else:
            variants.append((Path(working_video), selected_platforms, width, height))

        jobs = []
        created_count = 0
        for variant_video, platforms, variant_width, variant_height in variants:
            publish_job, created = enqueue_publish_job(
                queue_path,
                variant_video,
                platforms=platforms,
                title=title,
                description=self.ai_config.get("reup_description", ""),
                hashtags=self.ai_config.get("reup_hashtags", ""),
                source_url=item.get("source_url", ""),
                width=variant_width,
                height=variant_height,
            )
            jobs.append(publish_job["id"])
            created_count += int(created)

        item["publish_job_ids"] = jobs
        item["publish_job_id"] = jobs[0]
        item["publish_queue"] = queue_path.as_posix()
        detail = f"Reup queue: {created_count} job mới, {len(jobs) - created_count} job dùng lại"
        self._mark_stage(item, "publish_prepare", queue_path, detail=detail)
        self.log.emit(f"[{offset}/{total}] {detail}: {', '.join(jobs)}")
    def _process_item(self, item, offset, total):
        try:
            return self._process_item_pipeline(item, offset, total)
        except Exception as exc:
            item["status"] = f"L\u1ed7i: {exc}"
            self._mark_stage(item, "failed", status="failed", detail=str(exc))
            self.log.emit(f"[{offset}/{total}] L\u1ed7i: {exc}")
            raise


    def _run_ffmpeg(self, command):
        final_args = [str(arg) for arg in command]
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-ignore_unknown", "-threads", "0"]
        if "-y" not in final_args:
            cmd.append("-y")
        cmd.extend(final_args)
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
                tail = tail[-10:]
            if self.isInterruptionRequested():
                self.stop()
                raise RuntimeError("Đã dừng FFmpeg.")
        code = self.process.wait()
        if code != 0:
            raise RuntimeError("\n".join(tail) or f"FFmpeg lỗi: {code}")


class BatchFilePage(QtWidgets.QWidget):
    items_changed = Signal(list)
    open_batch_editor = Signal()
    open_ai_setup = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.srt_files = []
        self.video_files = []
        self.video_metadata = {}
        self.items = []
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
        root.setSpacing(12)

        title = QtWidgets.QLabel("Batch video: có thể chọn video kèm SRT hoặc chỉ video")
        title.setObjectName("batchTitle")
        title.setStyleSheet("font-size:20px;font-weight:700;color:#dce8f8")
        root.addWidget(title)

        mode_group = QtWidgets.QGroupBox("Dịch batch")
        mode_layout = QtWidgets.QVBoxLayout(mode_group)
        mode_layout.setContentsMargins(10, 24, 10, 10)
        mode_layout.setSpacing(8)
        self.radio_api = QtWidgets.QRadioButton("API / LLM")
        self.radio_chrome = QtWidgets.QRadioButton("Chrome + Gemini Gem")
        self.radio_chrome.setChecked(True)
        self.source_lang = QtWidgets.QComboBox()
        self.source_lang.addItems(["Tiếng Trung", "Tiếng Anh", "Tiếng Việt", "Nhật", "Hàn"])
        self.subtitle_lang = QtWidgets.QComboBox()
        self.subtitle_lang.addItems(["Tiếng Việt", "Tiếng Anh", "Giữ nguyên"])
        self.voice_lang = QtWidgets.QComboBox()
        self.voice_lang.addItems(["Tiếng Việt", "Không lồng tiếng", "Tiếng Anh"])
        for combo in (self.source_lang, self.subtitle_lang, self.voice_lang):
            combo.setMinimumWidth(130)
            combo.setMaximumWidth(210)
        self.gem_url = QtWidgets.QLineEdit()
        self.gem_url.setPlaceholderText("https://gemini.google.com/gem/...")
        self.gem_url.setText(settings.get("batch_gemini_gem_url", "https://gemini.google.com/gem/90edf2642605"))
        mode_top = QtWidgets.QHBoxLayout()
        mode_top.addWidget(self.radio_api)
        mode_top.addWidget(self.radio_chrome)
        mode_top.addWidget(QtWidgets.QLabel("Gem URL"))
        mode_top.addWidget(self.gem_url, 1)
        mode_layout.addLayout(mode_top)

        mode_bottom = QtWidgets.QHBoxLayout()
        mode_bottom.addWidget(QtWidgets.QLabel("SRT gốc"))
        mode_bottom.addWidget(self.source_lang)
        mode_bottom.addSpacing(12)
        mode_bottom.addWidget(QtWidgets.QLabel("Phụ đề"))
        mode_bottom.addWidget(self.subtitle_lang)
        mode_bottom.addSpacing(12)
        mode_bottom.addWidget(QtWidgets.QLabel("Lồng tiếng"))
        mode_bottom.addWidget(self.voice_lang)
        mode_bottom.addStretch()
        mode_layout.addLayout(mode_bottom)
        root.addWidget(mode_group)

        folder_group = QtWidgets.QGroupBox("Thư mục mặc định")
        folder_grid = QtWidgets.QGridLayout(folder_group)
        folder_grid.setContentsMargins(10, 24, 10, 10)
        folder_grid.setHorizontalSpacing(8)
        folder_grid.setVerticalSpacing(8)
        default_input_dir = params.get("last_opendir", f"{ROOT_DIR}/input")
        self.srt_default_dir = QtWidgets.QLineEdit(params.get("batch_srt_default_dir", default_input_dir))
        self.video_default_dir = QtWidgets.QLineEdit(params.get("batch_video_default_dir", default_input_dir))
        self.srt_default_btn = QtWidgets.QPushButton("Chọn")
        self.video_default_btn = QtWidgets.QPushButton("Chọn")
        self.open_srt_default_btn = QtWidgets.QPushButton("Mở")
        self.open_video_default_btn = QtWidgets.QPushButton("Mở")
        for button in (self.srt_default_btn, self.video_default_btn, self.open_srt_default_btn, self.open_video_default_btn):
            button.setFixedWidth(58)
        self.srt_default_dir.setPlaceholderText("Thư mục mở mặc định khi chọn nhiều SRT")
        self.video_default_dir.setPlaceholderText("Thư mục mở mặc định khi chọn nhiều video")
        folder_grid.addWidget(QtWidgets.QLabel("SRT"), 0, 0)
        folder_grid.addWidget(self.srt_default_dir, 0, 1)
        folder_grid.addWidget(self.srt_default_btn, 0, 2)
        folder_grid.addWidget(self.open_srt_default_btn, 0, 3)
        folder_grid.addWidget(QtWidgets.QLabel("Video"), 1, 0)
        folder_grid.addWidget(self.video_default_dir, 1, 1)
        folder_grid.addWidget(self.video_default_btn, 1, 2)
        folder_grid.addWidget(self.open_video_default_btn, 1, 3)
        folder_grid.setColumnStretch(1, 1)
        root.addWidget(folder_group)

        select_row = QtWidgets.QHBoxLayout()
        self.choose_srt_btn = QtWidgets.QPushButton("Chọn nhiều SRT...")
        self.choose_video_btn = QtWidgets.QPushButton("Chọn nhiều Video...")
        self.clear_btn = QtWidgets.QPushButton("Xóa danh sách")
        self.choose_srt_btn.setMinimumWidth(125)
        self.choose_video_btn.setMinimumWidth(135)
        self.clear_btn.setMinimumWidth(105)
        self.srt_count = QtWidgets.QLabel("0 file SRT")
        self.video_count = QtWidgets.QLabel("0 file Video")
        select_row.addWidget(self.choose_srt_btn)
        select_row.addWidget(self.srt_count)
        select_row.addWidget(self.choose_video_btn)
        select_row.addWidget(self.video_count)
        select_row.addStretch()
        select_row.addWidget(self.clear_btn)
        root.addLayout(select_row)

        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "SRT", "Video", "Trạng thái", "Edit"])
        self.table.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.table.setMinimumHeight(260)
        self.table.verticalHeader().setVisible(False)
        self.table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(42)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        root.addWidget(self.table, 1)

        bottom = QtWidgets.QHBoxLayout()
        self.open_editor_btn = QtWidgets.QPushButton("Mở editor hàng loạt")
        self.open_ai_btn = QtWidgets.QPushButton("Setup AI + giọng đọc")
        bottom.addStretch()
        bottom.addWidget(self.open_editor_btn)
        bottom.addWidget(self.open_ai_btn)
        root.addLayout(bottom)

    def _connect_signals(self):
        self.choose_srt_btn.clicked.connect(self._choose_srts)
        self.choose_video_btn.clicked.connect(self._choose_videos)
        self.clear_btn.clicked.connect(self._clear)
        self.open_editor_btn.clicked.connect(self.open_batch_editor.emit)
        self.open_ai_btn.clicked.connect(self.open_ai_setup.emit)
        self.gem_url.textChanged.connect(self._save_gem_url)
        self.srt_default_btn.clicked.connect(lambda: self._choose_default_dir("srt"))
        self.video_default_btn.clicked.connect(lambda: self._choose_default_dir("video"))
        self.open_srt_default_btn.clicked.connect(lambda: self._open_default_dir(self.srt_default_dir.text()))
        self.open_video_default_btn.clicked.connect(lambda: self._open_default_dir(self.video_default_dir.text()))
        self.srt_default_dir.editingFinished.connect(self._save_default_dirs)
        self.video_default_dir.editingFinished.connect(self._save_default_dirs)

    def ai_config(self):
        return {
            "translation_mode": "chrome_gemini_gem" if self.radio_chrome.isChecked() else "api_llm",
            "gem_url": self.gem_url.text().strip(),
            "source_language": self.source_lang.currentText(),
            "subtitle_language": self.subtitle_lang.currentText(),
            "voice_language": self.voice_lang.currentText(),
        }

    def _save_gem_url(self):
        settings["batch_gemini_gem_url"] = self.gem_url.text().strip()
        settings.save()

    def _save_default_dirs(self):
        params["batch_srt_default_dir"] = self.srt_default_dir.text().strip().replace("\\", "/")
        params["batch_video_default_dir"] = self.video_default_dir.text().strip().replace("\\", "/")
        params.save()

    def _default_dir(self, line_edit):
        value = line_edit.text().strip()
        if value and Path(value).exists():
            return value
        fallback = params.get("last_opendir", f"{ROOT_DIR}/input")
        return fallback if Path(fallback).exists() else str(Path(ROOT_DIR) / "input")

    def _choose_default_dir(self, kind):
        line_edit = self.srt_default_dir if kind == "srt" else self.video_default_dir
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Chọn thư mục mặc định", self._default_dir(line_edit))
        if not folder:
            return
        line_edit.setText(folder.replace("\\", "/"))
        self._save_default_dirs()

    def _open_default_dir(self, folder):
        folder = folder.strip() or str(Path(ROOT_DIR) / "input")
        Path(folder).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _choose_srts(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Chọn nhiều file SRT", self._default_dir(self.srt_default_dir), SRT_EXTENSIONS)
        if not files:
            return
        self.srt_files = [f.replace("\\", "/") for f in files]
        self.srt_default_dir.setText(str(Path(files[0]).parent).replace("\\", "/"))
        params["last_opendir"] = self.srt_default_dir.text()
        self._save_default_dirs()
        self._rematch()

    def _choose_videos(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Chọn nhiều video", self._default_dir(self.video_default_dir), VIDEO_EXTENSIONS)
        if not files:
            return
        self.video_files = [f.replace("\\", "/") for f in files]
        self.video_default_dir.setText(str(Path(files[0]).parent).replace("\\", "/"))
        params["last_opendir"] = self.video_default_dir.text()
        self._save_default_dirs()
        self._rematch()

    def add_video_files(self, files):
        supported = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v"}
        combined = list(self.video_files)
        seen = {str(Path(item).resolve()).casefold() for item in combined}
        for entry in files or []:
            metadata = dict(entry) if isinstance(entry, dict) else {}
            file_path = metadata.get("video") or metadata.get("file") or entry
            path = Path(file_path)
            if not path.is_file() or path.suffix.lower() not in supported:
                continue
            resolved = path.resolve()
            key = str(resolved).casefold()
            if key not in seen:
                seen.add(key)
                combined.append(resolved.as_posix())
            if metadata:
                self.video_metadata[key] = {
                    "source_url": str(metadata.get("source_url") or ""),
                    "source_title": str(metadata.get("source_title") or ""),
                }
        self.video_files = combined
        if self.video_files:
            self.video_default_dir.setText(Path(self.video_files[-1]).parent.as_posix())
            self._save_default_dirs()
        self._rematch()

    def _clear(self):
        self.srt_files = []
        self.video_files = []
        self.video_metadata = {}
        self.items = []
        self._render_table()
        self.items_changed.emit(self.items)

    def _rematch(self):
        old_data = {
            _norm_stem(it.get("srt") or it.get("video")): {
                "edit": it.get("edit", {}),
                "source_url": it.get("source_url", ""),
                "source_title": it.get("source_title", ""),
            }
            for it in self.items
        }
        self.items = match_srt_video_files(self.srt_files, self.video_files)
        for item in self.items:
            key = _norm_stem(item.get("srt") or item.get("video"))
            previous = old_data.get(key, {})
            for field in ("edit", "source_url", "source_title"):
                if previous.get(field):
                    item[field] = previous[field]
            if item.get("video"):
                video_key = str(Path(item["video"]).resolve()).casefold()
                metadata = self.video_metadata.get(video_key, {})
                item.update({field: value for field, value in metadata.items() if value})
        self._render_table()
        self.items_changed.emit(self.items)

    def set_items(self, items, emit=True):
        self.items = copy.deepcopy(items)
        self._render_table()
        if emit:
            self.items_changed.emit(self.items)

    def _render_table(self):
        self.srt_count.setText(f"{len(self.srt_files)} file SRT")
        self.video_count.setText(f"{len(self.video_files)} file Video")
        self.table.setRowCount(len(self.items))
        for row, item in enumerate(self.items):
            values = [
                str(row + 1),
                _short_path(item.get("srt")),
                _short_path(item.get("video")),
                item.get("status", ""),
                self._edit_label(item.get("edit")),
            ]
            for col, value in enumerate(values):
                table_item = QtWidgets.QTableWidgetItem(value)
                if item.get("srt"):
                    table_item.setToolTip(item.get("srt"))
                if col == 2 and item.get("video"):
                    table_item.setToolTip(item.get("video"))
                if item.get("status") != "Sẵn sàng":
                    table_item.setForeground(QtGui.QColor("#f0b65b"))
                self.table.setItem(row, col, table_item)

    def _edit_label(self, edit):
        if not edit:
            return "Mặc định"
        parts = []
        if edit.get("enabled", True):
            if edit.get("blur", True):
                parts.append("blur")
            if edit.get("cover", False):
                parts.append("che")
        subtitle = edit.get("subtitle") or {}
        if subtitle.get("enabled", True):
            parts.append("sub")
        return " + ".join(parts) if parts else "Tắt"


class BatchEditorPage(QtWidgets.QWidget):
    items_changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.index = 0
        self.video_info = {"width": 0, "height": 0, "time": 0}
        self._syncing = False
        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        title = QtWidgets.QLabel("Batch Editor - chỉnh subtitle + blur cho từng video")
        title.setStyleSheet("font-size:20px;font-weight:700;color:#dce8f8")
        root.addWidget(title)

        controls = QtWidgets.QGridLayout()
        self.prev_btn = QtWidgets.QPushButton("<< Trước")
        self.next_btn = QtWidgets.QPushButton("Sau >>")
        self.current_label = QtWidgets.QLabel("Chưa có video")
        self.preview_btn = QtWidgets.QPushButton("Lấy frame preview")
        self.reset_btn = QtWidgets.QPushButton("Reset vị trí")
        self.bottom_btn = QtWidgets.QPushButton("Căn vùng subtitle")
        self.apply_all_btn = QtWidgets.QPushButton("Áp dụng cho tất cả")
        self.minus_btn = QtWidgets.QPushButton("-1s")
        self.plus_btn = QtWidgets.QPushButton("+1s")
        self.preview_second = QtWidgets.QSpinBox()
        self.preview_second.setRange(0, 36000)
        self.preview_second.setSuffix("s")
        self.enable_edit = QtWidgets.QCheckBox("Bật edit")
        self.enable_edit.setChecked(True)
        self.blur_check = QtWidgets.QCheckBox("Blur")
        self.blur_check.setChecked(True)
        self.cover_check = QtWidgets.QCheckBox("Che")
        self.blur_strength = QtWidgets.QComboBox()
        self.blur_strength.addItem("Light", 12)
        self.blur_strength.addItem("Medium", 22)
        self.blur_strength.addItem("Strong", 36)
        self.orientation = QtWidgets.QComboBox()
        self.orientation.addItems(["Auto", "Dọc 9:16", "Ngang 16:9", "Vuông 1:1"])
        self.target_combo = QtWidgets.QComboBox()
        self.target_combo.addItem("Che/blur cũ", "mask")
        self.target_combo.addItem("Sub mới", "subtitle")

        controls.addWidget(self.prev_btn, 0, 0)
        controls.addWidget(self.next_btn, 0, 1)
        controls.addWidget(self.current_label, 0, 2, 1, 3)
        controls.addWidget(self.preview_btn, 0, 5)
        controls.addWidget(self.reset_btn, 0, 6)
        controls.addWidget(self.bottom_btn, 0, 7)
        controls.addWidget(self.apply_all_btn, 0, 8)
        controls.addWidget(self.minus_btn, 0, 9)
        controls.addWidget(self.preview_second, 0, 10)
        controls.addWidget(self.plus_btn, 0, 11)
        controls.addWidget(QtWidgets.QLabel("Hướng"), 1, 0)
        controls.addWidget(self.orientation, 1, 1)
        controls.addWidget(self.enable_edit, 1, 2)
        controls.addWidget(self.blur_check, 1, 3)
        controls.addWidget(self.cover_check, 1, 4)
        controls.addWidget(QtWidgets.QLabel("Độ mạnh"), 1, 5)
        controls.addWidget(self.blur_strength, 1, 6)
        controls.addWidget(QtWidgets.QLabel("Kéo vùng"), 2, 0)
        controls.addWidget(self.target_combo, 2, 1, 1, 2)

        self.x_spin = self._coord_spin()
        self.y_spin = self._coord_spin()
        self.w_spin = self._coord_spin(minimum=2)
        self.h_spin = self._coord_spin(minimum=2)
        controls.addWidget(QtWidgets.QLabel("X"), 1, 7)
        controls.addWidget(self.x_spin, 1, 8)
        controls.addWidget(QtWidgets.QLabel("Y"), 1, 9)
        controls.addWidget(self.y_spin, 1, 10)
        controls.addWidget(QtWidgets.QLabel("W"), 2, 7)
        controls.addWidget(self.w_spin, 2, 8)
        controls.addWidget(QtWidgets.QLabel("H"), 2, 9)
        controls.addWidget(self.h_spin, 2, 10)
        root.addLayout(controls)

        sub_group = QtWidgets.QGroupBox("Vị trí phụ đề sau dịch")
        sub_grid = QtWidgets.QGridLayout(sub_group)
        sub_grid.setContentsMargins(10, 24, 10, 10)
        sub_grid.setHorizontalSpacing(8)
        sub_grid.setVerticalSpacing(8)
        self.subtitle_enabled = QtWidgets.QCheckBox("Burn sub sau dịch")
        self.subtitle_enabled.setChecked(True)
        self.sub_bottom_btn = QtWidgets.QPushButton("Căn sub mới")
        self.sub_x_spin = self._coord_spin()
        self.sub_y_spin = self._coord_spin()
        self.sub_w_spin = self._coord_spin(minimum=2)
        self.sub_h_spin = self._coord_spin(minimum=2)
        self.sub_font_name = QtWidgets.QLineEdit("Arial")
        self.sub_font_size = QtWidgets.QSpinBox()
        self.sub_font_size.setRange(10, 160)
        self.sub_font_size.setValue(42)
        self.sub_color = QtWidgets.QComboBox()
        self.sub_color.addItems(["white", "yellow", "cyan", "black"])
        self.sub_outline = QtWidgets.QSpinBox()
        self.sub_outline.setRange(0, 12)
        self.sub_outline.setValue(3)
        sub_grid.addWidget(self.subtitle_enabled, 0, 0, 1, 2)
        sub_grid.addWidget(self.sub_bottom_btn, 0, 2, 1, 2)
        sub_grid.addWidget(QtWidgets.QLabel("X"), 1, 0)
        sub_grid.addWidget(self.sub_x_spin, 1, 1)
        sub_grid.addWidget(QtWidgets.QLabel("Y"), 1, 2)
        sub_grid.addWidget(self.sub_y_spin, 1, 3)
        sub_grid.addWidget(QtWidgets.QLabel("W"), 1, 4)
        sub_grid.addWidget(self.sub_w_spin, 1, 5)
        sub_grid.addWidget(QtWidgets.QLabel("H"), 1, 6)
        sub_grid.addWidget(self.sub_h_spin, 1, 7)
        sub_grid.addWidget(QtWidgets.QLabel("Font"), 2, 0)
        sub_grid.addWidget(self.sub_font_name, 2, 1, 1, 2)
        sub_grid.addWidget(QtWidgets.QLabel("Size"), 2, 3)
        sub_grid.addWidget(self.sub_font_size, 2, 4)
        sub_grid.addWidget(QtWidgets.QLabel("Màu"), 2, 5)
        sub_grid.addWidget(self.sub_color, 2, 6)
        sub_grid.addWidget(QtWidgets.QLabel("Viền"), 2, 7)
        sub_grid.addWidget(self.sub_outline, 2, 8)
        sub_grid.setColumnStretch(8, 1)
        root.addWidget(sub_group)

        self.preview = RegionPreview()
        self.preview.setMinimumSize(760, 430)
        root.addWidget(self.preview, 1)

        self.status_label = QtWidgets.QLabel(
            "Kéo trong khung để di chuyển, kéo góc/cạnh để resize, kéo vùng trống để tạo khung mới."
        )
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

    def _connect_signals(self):
        self.prev_btn.clicked.connect(lambda: self._move(-1))
        self.next_btn.clicked.connect(lambda: self._move(1))
        self.preview_btn.clicked.connect(self._load_preview)
        self.reset_btn.clicked.connect(self._reset_current)
        self.bottom_btn.clicked.connect(self._set_bottom_region)
        self.apply_all_btn.clicked.connect(self._apply_all)
        self.minus_btn.clicked.connect(lambda: self._shift_preview(-1))
        self.plus_btn.clicked.connect(lambda: self._shift_preview(1))
        self.preview_second.valueChanged.connect(self._save_controls)
        self.preview.region_changed.connect(self._region_from_preview)
        self.enable_edit.toggled.connect(self._save_controls)
        self.blur_check.toggled.connect(self._save_controls)
        self.cover_check.toggled.connect(self._save_controls)
        self.blur_strength.currentIndexChanged.connect(self._save_controls)
        self.orientation.currentIndexChanged.connect(self._save_controls)
        self.target_combo.currentIndexChanged.connect(self._active_region_changed)
        self.subtitle_enabled.toggled.connect(self._save_controls)
        self.sub_bottom_btn.clicked.connect(self._set_bottom_subtitle_region)
        self.sub_font_name.editingFinished.connect(self._save_controls)
        self.sub_font_size.valueChanged.connect(self._save_controls)
        self.sub_color.currentIndexChanged.connect(self._save_controls)
        self.sub_outline.valueChanged.connect(self._save_controls)
        for spin in (self.x_spin, self.y_spin, self.w_spin, self.h_spin):
            spin.valueChanged.connect(self._region_from_spins)
        for spin in (self.sub_x_spin, self.sub_y_spin, self.sub_w_spin, self.sub_h_spin):
            spin.valueChanged.connect(self._subtitle_from_spins)

    def _coord_spin(self, minimum=0):
        spin = QtWidgets.QSpinBox()
        spin.setRange(minimum, 20000)
        return spin

    def set_items(self, items):
        self.items = copy.deepcopy(items)
        self.index = min(self.index, max(0, len(self.items) - 1))
        self._load_current()

    def current_items(self):
        return copy.deepcopy(self.items)

    def _current_item(self):
        if not self.items or self.index < 0 or self.index >= len(self.items):
            return None
        return self.items[self.index]

    def _move(self, delta):
        if not self.items:
            return
        self._save_controls()
        self.index = max(0, min(self.index + delta, len(self.items) - 1))
        self._load_current()

    def _load_current(self):
        item = self._current_item()
        if not item:
            self.current_label.setText("Chưa có video")
            return
        self.current_label.setText(
            f"Video {self.index + 1}/{len(self.items)}: {_short_path(item.get('video')) or '(thiếu video)'} | "
            f"SRT: {_short_path(item.get('srt')) or '(thiếu SRT)'}"
        )
        if not item.get("video") or not Path(item["video"]).exists():
            self.status_label.setText("Dòng này thiếu video.")
            return
        self._read_video_info(item["video"])
        edit = item.get("edit") or _default_edit_for_video(self.video_info.get("width", 0), self.video_info.get("height", 0))
        item["edit"] = edit
        self._set_controls_from_edit(edit)
        self._load_preview()

    def _read_video_info(self, video):
        try:
            info = tools.get_video_info(video)
            self.video_info = info
            width, height = info.get("width", 0), info.get("height", 0)
            seconds = max(0, int((info.get("time", 0) or 0) / 1000))
            self.preview_second.setRange(0, max(0, seconds))
            self._set_region_limits(width, height)
            self.status_label.setText(f"{width} x {height} | {seconds}s")
        except Exception as e:
            self.status_label.setText(f"Lỗi đọc video: {e}")

    def _set_region_limits(self, width, height):
        self.x_spin.setRange(0, max(0, width - 2))
        self.y_spin.setRange(0, max(0, height - 2))
        self.w_spin.setRange(2, max(2, width))
        self.h_spin.setRange(2, max(2, height))
        self.sub_x_spin.setRange(0, max(0, width - 2))
        self.sub_y_spin.setRange(0, max(0, height - 2))
        self.sub_w_spin.setRange(2, max(2, width))
        self.sub_h_spin.setRange(2, max(2, height))

    def _set_controls_from_edit(self, edit):
        self._syncing = True
        self.enable_edit.setChecked(bool(edit.get("enabled", True)))
        self.blur_check.setChecked(bool(edit.get("blur", True)))
        self.cover_check.setChecked(bool(edit.get("cover", False)))
        radius = int(edit.get("blur_radius", 18))
        idx = 0 if radius <= 14 else (1 if radius <= 28 else 2)
        self.blur_strength.setCurrentIndex(idx)
        self.preview_second.setValue(int(edit.get("preview_second", 1)))
        x, y, w, h = edit.get("region") or _default_edit_for_video(self.video_info.get("width", 0), self.video_info.get("height", 0))["region"]
        self.x_spin.setValue(int(x))
        self.y_spin.setValue(int(y))
        self.w_spin.setValue(int(w))
        self.h_spin.setValue(int(h))
        subtitle = edit.get("subtitle") or _default_subtitle_style(self.video_info.get("width", 0), self.video_info.get("height", 0))
        sx, sy, sw, sh = subtitle.get("region") or _default_subtitle_style(
            self.video_info.get("width", 0), self.video_info.get("height", 0)
        )["region"]
        self.subtitle_enabled.setChecked(bool(subtitle.get("enabled", True)))
        self.sub_x_spin.setValue(int(sx))
        self.sub_y_spin.setValue(int(sy))
        self.sub_w_spin.setValue(int(sw))
        self.sub_h_spin.setValue(int(sh))
        self.sub_font_name.setText(str(subtitle.get("font_name") or "Arial"))
        self.sub_font_size.setValue(int(subtitle.get("font_size", 42)))
        color = str(subtitle.get("color") or "white")
        color_index = self.sub_color.findText(color)
        self.sub_color.setCurrentIndex(max(0, color_index))
        self.sub_outline.setValue(int(subtitle.get("outline", 3)))
        self._syncing = False
        self._refresh_preview_regions()

    def _save_controls(self):
        if self._syncing:
            return
        item = self._current_item()
        if not item:
            return
        item["edit"] = {
            "enabled": self.enable_edit.isChecked(),
            "blur": self.blur_check.isChecked(),
            "cover": self.cover_check.isChecked(),
            "region": [self.x_spin.value(), self.y_spin.value(), self.w_spin.value(), self.h_spin.value()],
            "blur_radius": self.blur_strength.currentData(),
            "blur_power": 2,
            "cover_color": "black",
            "cover_opacity": 35,
            "orientation": self.orientation.currentText(),
            "preview_second": self.preview_second.value(),
            "subtitle": self._subtitle_style_from_controls(),
        }
        self.items_changed.emit(self.items)

    def _subtitle_style_from_controls(self):
        return {
            "enabled": self.subtitle_enabled.isChecked(),
            "region": [
                self.sub_x_spin.value(),
                self.sub_y_spin.value(),
                self.sub_w_spin.value(),
                self.sub_h_spin.value(),
            ],
            "font_name": self.sub_font_name.text().strip() or "Arial",
            "font_size": self.sub_font_size.value(),
            "color": self.sub_color.currentText(),
            "outline": self.sub_outline.value(),
        }

    def _load_preview(self):
        item = self._current_item()
        if not item or not item.get("video") or not Path(item["video"]).exists():
            return
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        video = item["video"]
        preview_path = PREVIEW_DIR / f"batch_{_safe_name(Path(video).stem)}_{self.preview_second.value()}.jpg"
        try:
            tools.runffmpeg(
                [
                    "-ss",
                    str(self.preview_second.value()),
                    "-i",
                    video,
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(preview_path),
                ],
                force_cpu=True,
            )
            self.preview.set_preview(preview_path, (self.video_info.get("width", 0), self.video_info.get("height", 0)))
            self._refresh_preview_regions()
            self.status_label.setText(
                "Kéo trong khung để di chuyển, kéo góc/cạnh để resize, kéo vùng trống để tạo khung mới."
            )
        except Exception as e:
            self.status_label.setText(f"Lỗi lấy preview: {e}")

    def _region_from_preview(self, region):
        if self._current_target() == "subtitle":
            self._set_subtitle_region(region, update_preview=False)
        else:
            self._set_region(region, update_preview=False)

    def _region_from_spins(self):
        if self._syncing:
            return
        self._save_controls()
        self._refresh_preview_regions()

    def _subtitle_from_spins(self):
        if self._syncing:
            return
        self._save_controls()
        self._refresh_preview_regions()

    def _set_region(self, region, update_preview=True):
        region = _clamp_region(region, self.video_info.get("width", 0), self.video_info.get("height", 0))
        self._syncing = True
        self.x_spin.setValue(region[0])
        self.y_spin.setValue(region[1])
        self.w_spin.setValue(region[2])
        self.h_spin.setValue(region[3])
        self._syncing = False
        self._save_controls()
        if update_preview and self.preview.has_preview():
            self._refresh_preview_regions()

    def _set_subtitle_region(self, region, update_preview=True):
        region = _clamp_region(region, self.video_info.get("width", 0), self.video_info.get("height", 0))
        self._syncing = True
        self.sub_x_spin.setValue(region[0])
        self.sub_y_spin.setValue(region[1])
        self.sub_w_spin.setValue(region[2])
        self.sub_h_spin.setValue(region[3])
        self._syncing = False
        self._save_controls()
        if update_preview and self.preview.has_preview():
            self._refresh_preview_regions()

    def _current_target(self):
        return self.target_combo.currentData() or "mask"

    def _active_region_changed(self):
        self._refresh_preview_regions()

    def _refresh_preview_regions(self):
        if not self.preview.has_preview():
            return
        mask_region = [self.x_spin.value(), self.y_spin.value(), self.w_spin.value(), self.h_spin.value()]
        sub_region = [self.sub_x_spin.value(), self.sub_y_spin.value(), self.sub_w_spin.value(), self.sub_h_spin.value()]
        overlays = []
        if self._current_target() != "mask":
            overlays.append({"region": mask_region, "label": "BLUR/CHE", "color": "#32a9ff"})
        if self._current_target() != "subtitle":
            overlays.append({"region": sub_region, "label": "SUB MỚI", "color": "#f6c445"})
        self.preview.set_overlays(overlays)
        self.preview.set_video_region(sub_region if self._current_target() == "subtitle" else mask_region)

    def _set_bottom_region(self):
        width = self.video_info.get("width", 0)
        height = self.video_info.get("height", 0)
        if not width or not height:
            return
        region_h = max(48, int(height * 0.18))
        self._set_region([0, height - region_h, width, region_h])

    def _set_bottom_subtitle_region(self):
        width = self.video_info.get("width", 0)
        height = self.video_info.get("height", 0)
        if not width or not height:
            return
        style = _default_subtitle_style(width, height)
        self.target_combo.setCurrentIndex(max(0, self.target_combo.findData("subtitle")))
        self._set_subtitle_region(style["region"])

    def _reset_current(self):
        width = self.video_info.get("width", 0)
        height = self.video_info.get("height", 0)
        self._set_controls_from_edit(_default_edit_for_video(width, height))
        self._save_controls()
        self._load_preview()

    def _apply_all(self):
        if not self.items:
            return
        self._save_controls()
        current = copy.deepcopy((self._current_item() or {}).get("edit") or {})
        for item in self.items:
            if item.get("video"):
                item["edit"] = copy.deepcopy(current)
        self.items_changed.emit(self.items)
        self.status_label.setText("Đã áp dụng vùng edit hiện tại cho tất cả video.")

    def _shift_preview(self, delta):
        self.preview_second.setValue(max(0, self.preview_second.value() + delta))
        self._save_controls()
        self._load_preview()


class AiVoicePage(QtWidgets.QWidget):
    def __init__(self, file_page: BatchFilePage, parent=None):
        super().__init__(parent)
        self.file_page = file_page
        self._build_ui()

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
        root.setSpacing(12)
        title = QtWidgets.QLabel("AI + Giọng đọc")
        title.setStyleSheet("font-size:20px;font-weight:700;color:#dce8f8")
        root.addWidget(title)

        stt_box = QtWidgets.QGroupBox("Nh\u1eadn d\u1ea1ng subtitle khi video thi\u1ebfu SRT")
        stt_grid = QtWidgets.QGridLayout(stt_box)
        stt_grid.setContentsMargins(10, 24, 10, 10)
        stt_grid.setHorizontalSpacing(8)
        stt_grid.setVerticalSpacing(8)
        self.auto_stt = QtWidgets.QCheckBox("T\u1ef1 t\u00e1ch audio v\u00e0 t\u1ea1o SRT")
        self.auto_stt.setChecked(True)
        self.recogn_engine = QtWidgets.QComboBox()
        self.recogn_model = QtWidgets.QComboBox()
        self.recogn_model.setEditable(True)
        self.stt_cuda = QtWidgets.QCheckBox("CUDA")
        self.stt_cuda.setChecked(bool(params.get("stt_cuda", False)))
        self.stt_remove_noise = QtWidgets.QCheckBox("Gi\u1ea3m nhi\u1ec5u")
        self.stt_remove_noise.setChecked(bool(params.get("stt_remove_noise", False)))
        self.stt_diarization = QtWidgets.QCheckBox("T\u00e1ch ng\u01b0\u1eddi n\u00f3i")
        self.stt_diarization.setChecked(bool(params.get("stt_enable_diariz", False)))
        self.stt_speakers = QtWidgets.QSpinBox()
        self.stt_speakers.setRange(0, 20)
        self.stt_speakers.setSpecialValueText("T\u1ef1 ph\u00e1t hi\u1ec7n")
        self.stt_speakers.setValue(max(0, int(params.get("stt_nums_diariz", 0) or 0)))
        try:
            from videotrans import recognition

            self.recogn_engine.addItems(recognition.RECOGN_NAME_LIST)
            self.recogn_engine.setCurrentIndex(int(params.get("stt_recogn_type", 0) or 0))
        except Exception:
            self.recogn_engine.addItems(["faster-whisper", "openai-whisper"])
        self.recogn_engine.currentIndexChanged.connect(self._recogn_type_changed)
        self.auto_stt.toggled.connect(self._stt_enabled_changed)
        self.stt_diarization.toggled.connect(self.stt_speakers.setEnabled)
        stt_grid.addWidget(self.auto_stt, 0, 0, 1, 2)
        stt_grid.addWidget(QtWidgets.QLabel("Engine STT"), 1, 0)
        stt_grid.addWidget(self.recogn_engine, 1, 1)
        stt_grid.addWidget(QtWidgets.QLabel("Model"), 1, 2)
        stt_grid.addWidget(self.recogn_model, 1, 3)
        stt_grid.addWidget(self.stt_cuda, 2, 0)
        stt_grid.addWidget(self.stt_remove_noise, 2, 1)
        stt_grid.addWidget(self.stt_diarization, 2, 2)
        stt_grid.addWidget(self.stt_speakers, 2, 3)
        root.addWidget(stt_box)
        self._recogn_type_changed(self.recogn_engine.currentIndex())
        self._stt_enabled_changed(self.auto_stt.isChecked())
        box = QtWidgets.QGroupBox("Thiết lập dịch")
        grid = QtWidgets.QGridLayout(box)
        grid.setContentsMargins(10, 24, 10, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self.model_mode = QtWidgets.QComboBox()
        self.model_mode.addItems(["Chrome + Gemini Gem", "API / LLM"])
        self.model_mode.currentIndexChanged.connect(self._mode_changed)
        self.translate_engine = QtWidgets.QComboBox()
        try:
            from videotrans import translator

            self.translate_engine.addItems(translator.TRANSLASTE_NAME_LIST)
            self.translate_engine.setCurrentIndex(int(params.get("translate_type", 0) or 0))
        except Exception:
            self.translate_engine.addItems(["Google", "Microsoft"])
        self.gem_url = QtWidgets.QLineEdit(self.file_page.gem_url.text())
        self.gem_url.textChanged.connect(self.file_page.gem_url.setText)
        self.note = QtWidgets.QLabel(
            "Chrome + Gemini Gem dùng một Chrome profile cố định của tool. Bấm 'Mở Gemini Gem' để đăng nhập trước; "
            "khi chạy batch tool mới mở Gem URL, upload từng SRT, tải kết quả *_vi.srt rồi dùng file đó để render."
        )
        self.note.setWordWrap(True)
        grid.addWidget(QtWidgets.QLabel("Dịch qua"), 0, 0)
        grid.addWidget(self.model_mode, 0, 1)
        grid.addWidget(QtWidgets.QLabel("Provider"), 0, 2)
        grid.addWidget(self.translate_engine, 0, 3, 1, 2)
        grid.addWidget(QtWidgets.QLabel("Gem URL"), 1, 0)
        grid.addWidget(self.gem_url, 1, 1, 1, 4)
        grid.addWidget(self.note, 2, 0, 1, 5)
        root.addWidget(box)

        voice_box = QtWidgets.QGroupBox("Giọng đọc")
        voice_grid = QtWidgets.QGridLayout(voice_box)
        voice_grid.setContentsMargins(10, 24, 10, 10)
        voice_grid.setHorizontalSpacing(8)
        voice_grid.setVerticalSpacing(8)
        self.tts_engine = QtWidgets.QComboBox()
        self.voice = QtWidgets.QLineEdit()
        self.voice.setPlaceholderText("Tên voice/role trong pyvideotrans hoặc CapCut")
        self.sync_mode = QtWidgets.QComboBox()
        self.sync_mode.addItems(["Tự động cân bằng", "Chỉ ép audio", "Kéo video", "Subtitle theo audio"])
        self.thread_count = QtWidgets.QSpinBox()
        self.thread_count.setRange(1, 16)
        self.thread_count.setValue(1)
        self.audio_mode = QtWidgets.QComboBox()
        self.audio_mode.addItem("Thay audio b\u1eb1ng gi\u1ecdng \u0111\u1ecdc", "replace")
        self.audio_mode.addItem("Tr\u1ed9n nh\u1ecf audio g\u1ed1c", "mix")
        self.audio_mode.addItem("Ch\u1ec9 t\u1ea1o file gi\u1ecdng \u0111\u1ecdc", "voice_only")
        self.original_volume = QtWidgets.QSpinBox()
        self.original_volume.setRange(0, 100)
        self.original_volume.setValue(15)
        self.original_volume.setSuffix("%")
        voice_grid.addWidget(QtWidgets.QLabel("TTS"), 0, 0)
        voice_grid.addWidget(self.tts_engine, 0, 1)
        voice_grid.addWidget(QtWidgets.QLabel("Voice"), 0, 2)
        voice_grid.addWidget(self.voice, 0, 3)
        voice_grid.addWidget(QtWidgets.QLabel("Khớp thời gian"), 1, 0)
        voice_grid.addWidget(self.sync_mode, 1, 1)
        voice_grid.addWidget(QtWidgets.QLabel("Luồng TTS"), 1, 2)
        voice_grid.addWidget(self.thread_count, 1, 3)
        voice_grid.addWidget(QtWidgets.QLabel("Audio g\u1ed1c"), 2, 0)
        voice_grid.addWidget(self.audio_mode, 2, 1)
        voice_grid.addWidget(QtWidgets.QLabel("\u00c2m l\u01b0\u1ee3ng g\u1ed1c"), 2, 2)
        voice_grid.addWidget(self.original_volume, 2, 3)
        try:
            from videotrans import tts

            self.tts_engine.addItems(tts.TTS_NAME_LIST)
        except Exception:
            self.tts_engine.addItems(["Edge TTS", "Azure TTS", "Gemini TTS"])
        root.addWidget(voice_box)

        reup_box = QtWidgets.QGroupBox("Chuẩn bị reup")
        reup_grid = QtWidgets.QGridLayout(reup_box)
        reup_grid.setContentsMargins(10, 24, 10, 10)
        self.prepare_reup = QtWidgets.QCheckBox("Tạo job reup sau khi xử lý xong")
        self.reup_platforms = QtWidgets.QLineEdit("youtube, tiktok, douyin, bilibili")
        self.reup_platforms.setPlaceholderText("youtube, tiktok, douyin, bilibili, kuaishou, xiaohongshu")
        self.reup_title = QtWidgets.QLineEdit()
        self.reup_title.setPlaceholderText("Tiêu đề; để trống sẽ dùng tên file")
        self.reup_description = QtWidgets.QLineEdit()
        self.reup_description.setPlaceholderText("Mô tả dùng chung cho job reup")
        self.reup_hashtags = QtWidgets.QLineEdit("#vietsub #viral")
        self.reup_vertical = QtWidgets.QCheckBox("Tự tạo bản 9:16 cho nền tảng short-form")
        self.reup_vertical.setChecked(True)
        self.reup_vertical_strategy = QtWidgets.QComboBox()
        self.reup_vertical_strategy.addItem("Nền blur - giữ đủ khung hình", "blur")
        self.reup_vertical_strategy.addItem("Crop giữa", "crop")
        self.reup_vertical_strategy.addItem("Viền đen", "pad")
        reup_grid.addWidget(self.prepare_reup, 0, 0, 1, 4)
        reup_grid.addWidget(QtWidgets.QLabel("Nền tảng"), 1, 0)
        reup_grid.addWidget(self.reup_platforms, 1, 1, 1, 3)
        reup_grid.addWidget(QtWidgets.QLabel("Tiêu đề"), 2, 0)
        reup_grid.addWidget(self.reup_title, 2, 1, 1, 3)
        reup_grid.addWidget(QtWidgets.QLabel("Mô tả"), 3, 0)
        reup_grid.addWidget(self.reup_description, 3, 1, 1, 3)
        reup_grid.addWidget(QtWidgets.QLabel("Hashtag"), 4, 0)
        reup_grid.addWidget(self.reup_hashtags, 4, 1, 1, 3)
        reup_grid.addWidget(self.reup_vertical, 5, 0, 1, 2)
        reup_grid.addWidget(self.reup_vertical_strategy, 5, 2, 1, 2)
        self.prepare_reup.toggled.connect(self._reup_enabled_changed)
        root.addWidget(reup_box)
        root.addStretch()
        try:
            self.tts_engine.setCurrentIndex(int(params.get("tts_type", 0) or 0))
        except (TypeError, ValueError):
            self.tts_engine.setCurrentIndex(0)
        saved_voice = str(params.get("voice_role", "") or "").strip()
        if saved_voice not in {"No", "-"}:
            self.voice.setText(saved_voice)
        self.thread_count.setValue(max(1, min(int(settings.get("dubbing_thread", 1) or 1), 16)))
        self._mode_changed(self.model_mode.currentIndex())
        self._reup_enabled_changed(self.prepare_reup.isChecked())

    def _recogn_type_changed(self, index):
        current = self.recogn_model.currentText().strip()
        saved = str(params.get("stt_model_name", "") or "").strip()
        models = []
        try:
            from videotrans import recognition

            models = recognition.get_model_by_type(int(index))
        except Exception:
            pass
        self.recogn_model.clear()
        self.recogn_model.addItems([str(model) for model in models])
        preferred = current or saved
        if preferred:
            self.recogn_model.setCurrentText(preferred)

    def _stt_enabled_changed(self, enabled):
        for widget in (
            self.recogn_engine,
            self.recogn_model,
            self.stt_cuda,
            self.stt_remove_noise,
            self.stt_diarization,
        ):
            widget.setEnabled(bool(enabled))
        self.stt_speakers.setEnabled(bool(enabled) and self.stt_diarization.isChecked())

    def _reup_enabled_changed(self, enabled):
        for widget in (
            self.reup_platforms,
            self.reup_title,
            self.reup_description,
            self.reup_hashtags,
            self.reup_vertical,
            self.reup_vertical_strategy,
        ):
            widget.setEnabled(bool(enabled))

    def _mode_changed(self, index):
        self.file_page.radio_chrome.setChecked(index == 0)
        self.file_page.radio_api.setChecked(index == 1)
        self.translate_engine.setEnabled(index == 1)
        self.gem_url.setEnabled(index == 0)

    def ai_config(self):
        cfg = self.file_page.ai_config()
        cfg.update(
            {
                "prepare_reup": self.prepare_reup.isChecked(),
                "reup_platforms": [
                    value.strip()
                    for value in self.reup_platforms.text().split(",")
                    if value.strip()
                ],
                "reup_title": self.reup_title.text().strip(),
                "reup_description": self.reup_description.text().strip(),
                "reup_hashtags": self.reup_hashtags.text().strip(),
                "reup_vertical": self.reup_vertical.isChecked(),
                "reup_vertical_strategy": self.reup_vertical_strategy.currentData() or "blur",
                "auto_stt": self.auto_stt.isChecked(),
                "recogn_type": self.recogn_engine.currentIndex(),
                "recogn_engine": self.recogn_engine.currentText(),
                "recogn_model": self.recogn_model.currentText().strip(),
                "stt_cuda": self.stt_cuda.isChecked(),
                "stt_remove_noise": self.stt_remove_noise.isChecked(),
                "stt_diarization": self.stt_diarization.isChecked(),
                "stt_speakers": self.stt_speakers.value(),
                "model_mode_label": self.model_mode.currentText(),
                "translate_type": self.translate_engine.currentIndex(),
                "tts_engine": self.tts_engine.currentText(),
                "tts_type": self.tts_engine.currentIndex(),
                "voice": self.voice.text().strip(),
                "sync_mode": self.sync_mode.currentText(),
                "tts_threads": self.thread_count.value(),
                "audio_mode": self.audio_mode.currentData() or "replace",
                "original_volume": self.original_volume.value() / 100.0,
            }
        )
        return cfg



class RunPage(QtWidgets.QWidget):
    start_requested = Signal()
    stop_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        title = QtWidgets.QLabel("Chạy tất cả")
        title.setStyleSheet("font-size:20px;font-weight:700;color:#dce8f8")
        root.addWidget(title)

        row = QtWidgets.QHBoxLayout()
        self.output_dir = QtWidgets.QLineEdit(str(DEFAULT_OUTPUT_DIR / "batch").replace("\\", "/"))
        self.choose_output = QtWidgets.QPushButton("Chọn thư mục")
        self.open_gem_btn = QtWidgets.QPushButton("Mở Gemini Gem")
        self.open_reup_btn = QtWidgets.QPushButton("Quản lý reup")
        self.start_btn = QtWidgets.QPushButton("Bắt đầu xử lý tất cả")
        self.stop_btn = QtWidgets.QPushButton("Dừng")
        self.stop_btn.setDisabled(True)
        self.force_retranslate = QtWidgets.QCheckBox("Dịch lại _vi.srt đã có")
        self.force_retranslate.setToolTip("Bật mục này nếu muốn upload lại SRT lên Gemini dù file *_vi.srt đã tồn tại.")
        self.force_rebuild = QtWidgets.QCheckBox("L\u00e0m l\u1ea1i edit, subtitle v\u00e0 gi\u1ecdng \u0111\u1ecdc")
        self.force_rebuild.setToolTip(
            "B\u1eadt khi \u0111\u00e3 \u0111\u1ed5i v\u00f9ng blur, style subtitle, voice ho\u1eb7c c\u00e1ch gh\u00e9p audio."
        )
        row.addWidget(QtWidgets.QLabel("Lưu tại"))
        row.addWidget(self.output_dir, 1)
        row.addWidget(self.choose_output)
        row.addWidget(self.open_gem_btn)
        row.addWidget(self.open_reup_btn)
        row.addWidget(self.stop_btn)
        row.addWidget(self.start_btn)
        root.addLayout(row)
        root.addWidget(self.force_retranslate)
        root.addWidget(self.force_rebuild)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        root.addWidget(self.progress)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        root.addWidget(self.log, 1)
        self.choose_output.clicked.connect(self._choose_output)
        self.start_btn.clicked.connect(self.start_requested.emit)
        self.stop_btn.clicked.connect(self.stop_requested.emit)

    def _choose_output(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu", self.output_dir.text().strip())
        if folder:
            self.output_dir.setText(folder.replace("\\", "/"))

    def append_log(self, text):
        self.log.appendPlainText(text)

    def set_busy(self, busy):
        self.start_btn.setDisabled(busy)
        self.stop_btn.setDisabled(not busy)
        self.force_retranslate.setDisabled(busy)
        self.force_rebuild.setDisabled(busy)


class VideoBatchWorkflowWindow(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chỉnh sửa video")
        self.resize(1280, 820)
        self.worker = None
        self.gemini_launcher = None
        self.reup_window = None
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
        root.setContentsMargins(8, 8, 8, 8)
        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self.tabs)

        self.single_editor = VideoEditorWindow()
        self.batch_page = QtWidgets.QWidget()
        batch_layout = QtWidgets.QVBoxLayout(self.batch_page)
        batch_layout.setContentsMargins(0, 0, 0, 0)
        self.batch_tabs = QtWidgets.QTabWidget()
        batch_layout.addWidget(self.batch_tabs)

        self.file_page = BatchFilePage()
        self.batch_editor = BatchEditorPage()
        self.ai_page = AiVoicePage(self.file_page)
        self.run_page = RunPage()

        self.tabs.addTab(self.single_editor, "Từng video")
        self.tabs.addTab(self.batch_page, "Batch video")

        self.batch_tabs.addTab(self.file_page, "1. Chọn video / SRT")
        self.batch_tabs.addTab(self.batch_editor, "2. Edit hàng loạt")
        self.batch_tabs.addTab(self.ai_page, "3. AI + Giọng đọc")
        self.batch_tabs.addTab(self.run_page, "4. Chạy tất cả")

    def add_video_files(self, files):
        self.file_page.add_video_files(files)
        self._show_batch_page(self.file_page)
        self.show()
        self.activateWindow()
    def _connect_signals(self):
        self.file_page.items_changed.connect(self.batch_editor.set_items)
        self.batch_editor.items_changed.connect(lambda items: self.file_page.set_items(items, emit=False))
        self.file_page.open_batch_editor.connect(lambda: self._show_batch_page(self.batch_editor))
        self.file_page.open_ai_setup.connect(lambda: self._show_batch_page(self.ai_page))
        self.run_page.open_gem_btn.clicked.connect(self._open_gem)
        self.run_page.open_reup_btn.clicked.connect(self._open_reup_queue)
        self.run_page.start_requested.connect(self._start_all)
        self.run_page.stop_requested.connect(self._stop)

    def _show_batch_page(self, page):
        self.tabs.setCurrentWidget(self.batch_page)
        self.batch_tabs.setCurrentWidget(page)

    def _open_reup_queue(self):
        from videotrans.component.publish_queue_window import PublishQueueWindow

        output_dir = self.run_page.output_dir.text().strip() or str(DEFAULT_OUTPUT_DIR / "batch")
        queue_path = Path(output_dir) / "reup_queue.json"
        if not self.reup_window:
            self.reup_window = PublishQueueWindow(queue_path=queue_path)
        else:
            self.reup_window.set_queue_path(queue_path)
        self.reup_window.show()
        self.reup_window.activateWindow()
    def _open_gem(self):
        url = self.file_page.gem_url.text().strip() or "https://gemini.google.com/gem/90edf2642605"
        try:
            from videotrans.component.gemini_browser_automation import GeminiBrowserAutomation

            self.gemini_launcher = GeminiBrowserAutomation(gem_url=url, log=self.run_page.append_log)
            self.gemini_launcher.open_login()
            self.run_page.append_log(
                "Chrome profile cố định đã mở. Hãy đăng nhập Google/Gemini trong cửa sổ đó; "
                "khi đăng nhập xong bấm 'Bắt đầu xử lý tất cả' để tool mở Gem URL và dịch SRT."
            )
        except Exception as e:
            self.run_page.append_log(f"Lỗi mở Gemini tự động: {e}")
            QtWidgets.QMessageBox.warning(self, "Gemini Gem", str(e))

    def _start_all(self):
        if self.worker and self.worker.isRunning():
            return
        items = self.batch_editor.current_items()
        if not any(it.get("video") for it in items):
            QtWidgets.QMessageBox.warning(self, "Batch video", "Hãy chọn ít nhất một video hợp lệ trước.")
            return
        ai_config = self.ai_page.ai_config()
        ai_config["force_retranslate"] = self.run_page.force_retranslate.isChecked()
        ai_config["force_rebuild"] = self.run_page.force_rebuild.isChecked()
        if ai_config.get("translation_mode") == "chrome_gemini_gem":
            reply = QtWidgets.QMessageBox.question(
                self,
                "Xác nhận dùng Chrome",
                "Chrome + Gemini Gem sẽ dùng một Chrome profile cố định của tool.\n\n"
                "Trước khi chạy, hãy bấm 'Mở Gemini Gem' và đăng nhập Google/Gemini trong cửa sổ Chrome đó. "
                "Khi bấm Yes, tool sẽ mở Gem URL, upload từng SRT, tải kết quả thành *_vi.srt rồi render video.\n\n"
                "Nếu cửa sổ Chrome automation cũ bị lỗi, hãy đóng cửa sổ đó rồi bấm 'Mở Gemini Gem' lại.\n\n"
                "Tiếp tục?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.Yes,
            )
            if reply != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        output_dir = self.run_page.output_dir.text().strip() or str(DEFAULT_OUTPUT_DIR / "batch")
        self.run_page.set_busy(True)
        self.run_page.progress.setValue(0)
        self.worker = BatchWorkflowWorker(items, output_dir, ai_config, parent=self)
        self.worker.log.connect(self.run_page.append_log)
        self.worker.progress.connect(self.run_page.progress.setValue)
        self.worker.finished_ok.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(lambda: self.run_page.set_busy(False))
        self.worker.start()

    def _stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.run_page.append_log("Đang dừng worker...")

    def _done(self, output_dir):
        self.run_page.progress.setValue(100)
        self.run_page.append_log(f"Hoàn tất batch local: {output_dir}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(output_dir))

    def _failed(self, text):
        self.run_page.append_log(f"Lỗi: {text}")
        QtWidgets.QMessageBox.warning(self, "Batch video", text)
