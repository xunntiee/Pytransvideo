# -*- coding: utf-8 -*-
"""Reusable services for the all-in-one batch video pipeline.

This module intentionally has no Qt imports. The desktop worker, CLI and a
future WebUI can therefore share the exact same translation, dubbing and mux
behaviour.
"""

from dataclasses import asdict
from pathlib import Path


LANGUAGE_CODES = {
    "ti\u1ebfng trung": "zh-cn",
    "trung": "zh-cn",
    "chinese": "zh-cn",
    "simplified chinese": "zh-cn",
    "zh": "zh-cn",
    "zh-cn": "zh-cn",
    "ti\u1ebfng anh": "en",
    "anh": "en",
    "english": "en",
    "en": "en",
    "ti\u1ebfng vi\u1ec7t": "vi",
    "vi\u1ec7t": "vi",
    "vietnamese": "vi",
    "vi": "vi",
    "nh\u1eadt": "ja",
    "ti\u1ebfng nh\u1eadt": "ja",
    "japanese": "ja",
    "ja": "ja",
    "h\u00e0n": "ko",
    "ti\u1ebfng h\u00e0n": "ko",
    "korean": "ko",
    "ko": "ko",
}

NO_LANGUAGE_VALUES = {"", "-", "no", "none", "gi\u1eef nguy\u00ean", "kh\u00f4ng l\u1ed3ng ti\u1ebfng"}

SYNC_STRATEGIES = {
    "t\u1ef1 \u0111\u1ed9ng c\u00e2n b\u1eb1ng": "auto",
    "ch\u1ec9 \u00e9p audio": "audio",
    "k\u00e9o video": "video",
    "subtitle theo audio": "subtitle",
    "auto": "auto",
    "audio": "audio",
    "video": "video",
    "subtitle": "subtitle",
}

DEFAULT_EDGE_VOICES = {
    "vi": "vi-VN-HoaiMyNeural",
    "en": "en-US-GuyNeural",
    "zh-cn": "zh-CN-YunxiNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
}


def language_code(value):
    """Return a canonical language code for labels used by the batch UI."""
    text = str(value or "").strip()
    key = text.casefold()
    if key in NO_LANGUAGE_VALUES:
        return None
    if key in LANGUAGE_CODES:
        return LANGUAGE_CODES[key]

    # Preserve custom language codes supported by pyVideoTrans/newlang.txt.
    from videotrans.translator import get_code

    return get_code(show_text=text)


def sync_strategy(value):
    return SYNC_STRATEGIES.get(str(value or "").strip().casefold(), "auto")


def default_voice_role(language, tts_type, requested=""):
    """Use a safe Edge-TTS default; other providers require an explicit role."""
    requested = str(requested or "").strip()
    if requested and requested.casefold() not in {"no", "none", "-"}:
        return requested
    if int(tts_type or 0) == 0:
        return DEFAULT_EDGE_VOICES.get(language, "")
    return ""


def provider_index(provider_name, names, fallback=0):
    """Resolve a persisted provider label without depending on UI indices."""
    if isinstance(provider_name, int):
        return provider_name if 0 <= provider_name < len(names) else fallback
    try:
        return list(names).index(str(provider_name))
    except (ValueError, TypeError):
        return fallback


def task_file_fields(source_path, target_dir, cache_folder):
    """Build common TaskCfg file fields using the project's canonical helper."""
    from videotrans.util._ffmpeg_misc import format_video

    source = Path(source_path).resolve()
    item = asdict(format_video(str(source), None))
    item.update(
        {
            "name": source.as_posix(),
            "target_dir": Path(target_dir).resolve().as_posix(),
            "cache_folder": Path(cache_folder).resolve().as_posix(),
        }
    )
    return item


def translate_srt_api(
    source_srt,
    output_dir,
    *,
    source_language,
    target_language,
    translate_type=0,
    force=False,
):
    """Translate one SRT through the configured pyVideoTrans provider."""
    from videotrans.task.taskcfg import TaskCfgSTS
    from videotrans.task.translate_srt import TranslateSrt
    from videotrans.translator import is_allow_translate

    source_code = language_code(source_language)
    target_code = language_code(target_language)
    if not target_code or target_code == source_code:
        return None

    translate_type = int(translate_type or 0)
    allowed = is_allow_translate(
        translate_type=translate_type,
        show_target=target_code,
        return_str=True,
    )
    if allowed is not True:
        raise RuntimeError(str(allowed))

    output_dir = Path(output_dir)
    cache_dir = output_dir / ".translate-cache"
    fields = task_file_fields(source_srt, output_dir, cache_dir)
    fields.update(
        {
            "source_language_code": source_code or "auto",
            "target_language_code": target_code,
            "translate_type": translate_type,
        }
    )
    expected = output_dir / f"{fields['noextname']}.{target_code}.srt"
    if expected.is_file() and expected.stat().st_size > 0 and not force:
        return expected
    if force:
        expected.unlink(missing_ok=True)

    task = TranslateSrt(cfg=TaskCfgSTS(**fields), out_format=0)
    task.prepare()
    task.trans()
    result = Path(task.cfg.target_sub)
    if not result.is_file() or result.stat().st_size < 1:
        raise RuntimeError(f"Engine d\u1ecbch kh\u00f4ng t\u1ea1o \u0111\u01b0\u1ee3c SRT: {result}")
    return result


def transcribe_video(
    source_video,
    output_dir,
    *,
    source_language="auto",
    recogn_type=0,
    model_name="",
    remove_noise=False,
    enable_diariz=False,
    nums_diariz=0,
    rephrase=0,
    fix_punc=0,
    is_cuda=False,
    force=False,
):
    """Recognize speech from one video and return a source-language SRT."""
    from videotrans import recognition
    from videotrans.configure.config import app_cfg, params
    from videotrans.task.speech2text import SpeechToText
    from videotrans.task.taskcfg import TaskCfgSTT

    source_video = Path(source_video)
    if not source_video.is_file():
        raise RuntimeError(f"Video kh\u00f4ng t\u1ed3n t\u1ea1i: {source_video}")

    source_code = language_code(source_language) or "auto"
    recogn_type = int(recogn_type or 0)
    model_name = str(model_name or params.get("stt_model_name", "") or "").strip()
    allowed = recognition.is_allow_lang(
        langcode=source_code,
        recogn_type=recogn_type,
        model_name=model_name,
    )
    if allowed is not True:
        raise RuntimeError(str(allowed))
    configured = recognition.is_input_api(recogn_type=recogn_type, return_str=True)
    if configured is not True:
        raise RuntimeError(str(configured))

    output_dir = Path(output_dir)
    cache_dir = output_dir / ".stt-cache"
    fields = task_file_fields(source_video, output_dir, cache_dir)
    fields.update(
        {
            "detect_language": source_code,
            "recogn_type": recogn_type,
            "model_name": model_name,
            "remove_noise": bool(remove_noise),
            "enable_diariz": bool(enable_diariz),
            "nums_diariz": max(0, int(nums_diariz or 0)),
            "rephrase": int(rephrase or 0),
            "fix_punc": int(fix_punc or 0),
            "is_cuda": bool(is_cuda),
        }
    )
    expected = output_dir / f"{fields['noextname']}.srt"
    if expected.is_file() and expected.stat().st_size > 0 and not force:
        return expected
    if force:
        expected.unlink(missing_ok=True)

    app_cfg.rm_uuid(fields["uuid"])
    task = SpeechToText(cfg=TaskCfgSTT(**fields), out_format="srt")
    task.prepare()
    task.recogn()
    task.diariz()
    task.task_done()
    result = Path(task.cfg.target_sub)
    if not result.is_file() or result.stat().st_size < 1:
        raise RuntimeError(f"Engine nh\u1eadn d\u1ea1ng kh\u00f4ng t\u1ea1o \u0111\u01b0\u1ee3c SRT: {result}")
    return result


def dub_srt(
    source_srt,
    output_dir,
    *,
    target_language,
    tts_type=0,
    voice_role="",
    sync_mode="auto",
    threads=1,
    is_cuda=False,
    force=False,
):
    """Create a timeline-aligned WAV from an SRT using the configured TTS."""
    from videotrans import tts
    from videotrans.task.dubbing import DubbingSrt
    from videotrans.task.taskcfg import TaskCfgTTS

    target_code = language_code(target_language)
    if not target_code:
        return None

    tts_type = int(tts_type or 0)
    role = default_voice_role(target_code, tts_type, voice_role)
    if not role:
        raise RuntimeError("H\u00e3y nh\u1eadp voice/role cho engine TTS \u0111\u00e3 ch\u1ecdn.")

    allowed = tts.is_allow_lang(langcode=target_code, tts_type=tts_type)
    if allowed is not True:
        raise RuntimeError(str(allowed))
    configured = tts.is_input_api(tts_type=tts_type, return_str=True)
    if configured is not True:
        raise RuntimeError(str(configured))

    output_dir = Path(output_dir)
    cache_dir = output_dir / ".tts-cache"
    fields = task_file_fields(source_srt, output_dir, cache_dir)
    fields.update(
        {
            "target_language_code": target_code,
            "tts_type": tts_type,
            "voice_role": role,
            "voice_autorate": True,
            "align_sub_audio": True,
            "tts_sync_strategy": sync_strategy(sync_mode),
            "tts_threads": max(1, min(int(threads or 1), 16)),
            "is_cuda": bool(is_cuda),
        }
    )

    expected = output_dir / f"{fields['noextname']}.wav"
    if expected.is_file() and expected.stat().st_size > 0 and not force:
        return expected
    if force:
        expected.unlink(missing_ok=True)

    task = DubbingSrt(cfg=TaskCfgTTS(**fields), out_ext="wav")
    task.prepare()
    task.dubbing()
    task.align()
    result = Path(task.cfg.target_wav)
    if not result.is_file() or result.stat().st_size < 1:
        raise RuntimeError(f"Engine TTS kh\u00f4ng t\u1ea1o \u0111\u01b0\u1ee3c audio: {result}")
    return result


def build_vertical_reframe_command(
    video_path,
    output_path,
    *,
    strategy="blur",
    width=1080,
    height=1920,
):
    """Build a vertical 9:16 variant while preserving optional source audio."""
    video_path = str(video_path)
    output_path = str(output_path)
    width = max(2, int(width or 1080))
    height = max(2, int(height or 1920))
    strategy = str(strategy or "blur").strip().casefold()
    common = [
        "-i", video_path,
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
    ]

    if strategy == "crop":
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
        return ["-i", video_path, "-vf", vf, "-map", "0:v:0", *common[2:], output_path]

    if strategy == "pad":
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        )
        return ["-i", video_path, "-vf", vf, "-map", "0:v:0", *common[2:], output_path]

    graph = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=20:10[bg];"
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[v]"
    )
    return [
        "-i", video_path,
        "-filter_complex", graph,
        "-map", "[v]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]

def build_dub_mux_command(
    video_path,
    dubbed_audio,
    output_path,
    *,
    audio_mode="replace",
    has_original_audio=True,
    original_volume=0.15,
):
    """Build an FFmpeg command that never truncates video to short TTS audio."""
    video_path = str(video_path)
    dubbed_audio = str(dubbed_audio)
    output_path = str(output_path)
    mode = str(audio_mode or "replace").strip().casefold()

    if mode == "mix" and has_original_audio:
        volume = max(0.0, min(float(original_volume), 1.0))
        return [
            "-i", video_path,
            "-i", dubbed_audio,
            "-filter_complex",
            f"[0:a:0]volume={volume:.2f}[original];"
            "[1:a:0]apad[dubbed];"
            "[original][dubbed]amix=inputs=2:duration=first:dropout_transition=2[a]",
            "-map", "0:v:0",
            "-map", "[a]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            output_path,
        ]

    return [
        "-i", video_path,
        "-i", dubbed_audio,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-af", "apad",
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]
