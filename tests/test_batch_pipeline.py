from pathlib import Path


def test_language_sync_and_default_voice_mapping():
    from videotrans.process.batch_pipeline import (
        default_voice_role,
        language_code,
        provider_index,
        sync_strategy,
    )

    assert language_code("Ti\u1ebfng Trung") == "zh-cn"
    assert language_code("Ti\u1ebfng Vi\u1ec7t") == "vi"
    assert language_code("Kh\u00f4ng l\u1ed3ng ti\u1ebfng") is None
    assert sync_strategy("T\u1ef1 \u0111\u1ed9ng c\u00e2n b\u1eb1ng") == "auto"
    assert sync_strategy("Ch\u1ec9 \u00e9p audio") == "audio"
    assert default_voice_role("vi", 0, "") == "vi-VN-HoaiMyNeural"
    assert default_voice_role("vi", 1, "") == ""
    assert provider_index("B", ["A", "B", "C"]) == 1


def test_dub_mux_replace_pads_audio_instead_of_truncating_video():
    from videotrans.process.batch_pipeline import build_dub_mux_command

    command = build_dub_mux_command("video.mp4", "voice.wav", "out.mp4")

    assert command[-1] == "out.mp4"
    assert command[command.index("-af") + 1] == "apad"
    assert "-shortest" in command
    assert command[command.index("-map") + 1] == "0:v:0"


def test_dub_mux_mix_keeps_original_audio_at_requested_volume():
    from videotrans.process.batch_pipeline import build_dub_mux_command

    command = build_dub_mux_command(
        "video.mp4",
        "voice.wav",
        "out.mp4",
        audio_mode="mix",
        has_original_audio=True,
        original_volume=0.2,
    )

    graph = command[command.index("-filter_complex") + 1]
    assert "volume=0.20" in graph
    assert "amix=inputs=2:duration=first" in graph
    assert "-shortest" not in command


def test_translate_srt_api_reuses_project_task(monkeypatch, tmp_path):
    import videotrans.task.translate_srt as translate_module
    import videotrans.translator as translator
    from videotrans.process.batch_pipeline import translate_srt_api

    source = tmp_path / "sample.srt"
    source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    captured = {}

    class FakeTranslateTask:
        def __init__(self, cfg, out_format):
            captured["cfg"] = cfg
            captured["out_format"] = out_format
            self.cfg = cfg
            self.cfg.target_sub = f"{cfg.target_dir}/{cfg.noextname}.{cfg.target_language_code}.srt"

        def prepare(self):
            Path(self.cfg.target_dir).mkdir(parents=True, exist_ok=True)

        def trans(self):
            Path(self.cfg.target_sub).write_text("translated", encoding="utf-8")

    monkeypatch.setattr(translator, "is_allow_translate", lambda **kwargs: True)
    monkeypatch.setattr(translate_module, "TranslateSrt", FakeTranslateTask)

    result = translate_srt_api(
        source,
        tmp_path / "out",
        source_language="en",
        target_language="vi",
        translate_type=4,
    )

    assert result.read_text(encoding="utf-8") == "translated"
    assert captured["cfg"].source_language_code == "en"
    assert captured["cfg"].target_language_code == "vi"
    assert captured["cfg"].translate_type == 4


def test_tts_queue_thread_override_reaches_provider(monkeypatch):
    from videotrans import tts

    captured = {}

    class FakeProvider:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            return "ok"

    monkeypatch.setattr(tts, "get_class", lambda *args, **kwargs: FakeProvider)
    monkeypatch.setattr(tts.app_cfg, "exit_soft", False)

    result = tts.run(queue_tts=[{"tts_threads": 6}], tts_type=0)

    assert result == "ok"
    assert captured["dub_nums"] == 6


def test_batch_worker_runs_translate_subtitle_dub_mux_and_checkpoints(monkeypatch, tmp_path):
    import videotrans.process.batch_pipeline as pipeline
    from videotrans.component import video_batch_workflow as workflow

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    source_srt = tmp_path / "clip.srt"
    source_srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        encoding="utf-8",
    )

    def fake_translate(source, output_dir, **kwargs):
        target = Path(output_dir) / "clip.vi.srt"
        target.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nXin chao\n",
            encoding="utf-8",
        )
        return target

    def fake_dub(source, output_dir, **kwargs):
        target = Path(output_dir) / "clip.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"wav")
        return target

    monkeypatch.setattr(pipeline, "translate_srt_api", fake_translate)
    monkeypatch.setattr(pipeline, "dub_srt", fake_dub)
    monkeypatch.setattr(
        workflow.tools,
        "get_video_info",
        lambda path: {"width": 1080, "height": 1920, "streams_audio": 1},
    )

    config = {
        "translation_mode": "api_llm",
        "source_language": "Ti\u1ebfng Anh",
        "subtitle_language": "Ti\u1ebfng Vi\u1ec7t",
        "voice_language": "Ti\u1ebfng Vi\u1ec7t",
        "translate_type": 0,
        "tts_type": 0,
        "tts_threads": 2,
        "audio_mode": "replace",
    }
    item = {
        "video": video.as_posix(),
        "srt": source_srt.as_posix(),
        "edit": {
            "enabled": False,
            "region": [0, 1600, 1080, 320],
            "subtitle": {
                "enabled": True,
                "region": [100, 1500, 880, 250],
                "font_name": "Arial",
                "font_size": 48,
                "color": "white",
                "outline": 3,
            },
        },
    }
    worker = workflow.BatchWorkflowWorker([item], tmp_path / "output", config)

    def fake_ffmpeg(command):
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"rendered")

    monkeypatch.setattr(worker, "_run_ffmpeg", fake_ffmpeg)
    worker._process_item(worker.items[0], 1, 1)

    result = worker.items[0]
    assert Path(result["final_video"]).is_file()
    assert Path(result["dubbed_audio"]).is_file()
    assert result["status"] == "Ho\u00e0n t\u1ea5t"
    assert result["stages"]["translate"]["status"] == "completed"
    assert result["stages"]["dubbing"]["status"] == "completed"
    assert result["stages"]["complete"]["status"] == "completed"
    assert (tmp_path / "output" / "batch_vietsub_plan.json").is_file()


def test_single_edge_tts_segment_is_written_to_final_wav(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from videotrans import tts
    from videotrans.task.dubbing import DubbingSrt
    from videotrans.util import help_ffmpeg

    calls = []

    def fake_runffmpeg(command):
        calls.append(command)
        Path(command[-1]).write_bytes(b"wav")

    monkeypatch.setattr(help_ffmpeg, "runffmpeg", fake_runffmpeg)
    task = object.__new__(DubbingSrt)
    task.ignore_align = False
    task.queue_tts = [{"filename": (tmp_path / "segment.wav").as_posix()}]
    task.cfg = SimpleNamespace(
        tts_type=tts.EDGE_TTS,
        target_wav=(tmp_path / "final.wav").as_posix(),
    )

    task.align()
    assert calls and calls[0][-1] == task.cfg.target_wav
    assert Path(task.cfg.target_wav).is_file()


def test_batch_worker_real_ffmpeg_smoke(tmp_path):
    import shutil
    import subprocess

    import pytest

    from videotrans.component import video_batch_workflow as workflow

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not available")

    video = tmp_path / "smoke.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            video.as_posix(),
        ],
        check=True,
    )
    srt = tmp_path / "smoke.srt"
    srt.write_text(
        "1\n00:00:00,100 --> 00:00:00,800\nSmoke test\n",
        encoding="utf-8",
    )
    item = {
        "video": video.as_posix(),
        "srt": srt.as_posix(),
        "edit": {
            "enabled": True,
            "blur": True,
            "cover": False,
            "region": [0, 190, 320, 50],
            "blur_radius": 4,
            "blur_power": 1,
            "subtitle": {
                "enabled": True,
                "region": [20, 175, 280, 50],
                "font_name": "Arial",
                "font_size": 20,
                "color": "white",
                "outline": 2,
            },
        },
    }
    config = {
        "translation_mode": "disabled",
        "source_language": "en",
        "subtitle_language": "Gi\u1eef nguy\u00ean",
        "voice_language": "Kh\u00f4ng l\u1ed3ng ti\u1ebfng",
    }
    worker = workflow.BatchWorkflowWorker([item], tmp_path / "output", config)

    worker._process_item(worker.items[0], 1, 1)

    result = Path(worker.items[0]["final_video"])
    assert result.is_file() and result.stat().st_size > 0
    assert worker.items[0]["stages"]["edit"]["status"] == "completed"
    assert worker.items[0]["stages"]["subtitle"]["status"] == "completed"


def test_batch_worker_checkpoints_failed_stage(monkeypatch, tmp_path):
    import pytest

    from videotrans.component import video_batch_workflow as workflow

    item = {
        "video": (tmp_path / "broken.mp4").as_posix(),
        "srt": (tmp_path / "broken.srt").as_posix(),
        "edit": {},
    }
    worker = workflow.BatchWorkflowWorker([item], tmp_path / "output", {})

    def fail_pipeline(*args, **kwargs):
        raise RuntimeError("provider offline")

    monkeypatch.setattr(worker, "_process_item_pipeline", fail_pipeline)

    with pytest.raises(RuntimeError, match="provider offline"):
        worker._process_item(worker.items[0], 1, 1)

    result = worker.items[0]
    assert result["stages"]["failed"]["status"] == "failed"
    assert result["stages"]["failed"]["detail"] == "provider offline"
    assert "provider offline" in result["status"]
    assert (tmp_path / "output" / "batch_vietsub_plan.json").is_file()


def test_force_rebuild_disables_artifact_resume(tmp_path):
    from videotrans.component import video_batch_workflow as workflow

    artifact = tmp_path / "cached.mp4"
    artifact.write_bytes(b"cached")

    reuse_worker = workflow.BatchWorkflowWorker([], tmp_path / "reuse", {})
    rebuild_worker = workflow.BatchWorkflowWorker(
        [],
        tmp_path / "rebuild",
        {"force_rebuild": True},
    )

    assert reuse_worker._artifact_ready(artifact) is True
    assert rebuild_worker._artifact_ready(artifact) is False

def test_transcribe_video_reuses_project_speech_task(monkeypatch, tmp_path):
    import videotrans.recognition as recognition
    import videotrans.task.speech2text as speech_module
    from videotrans.process.batch_pipeline import transcribe_video

    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    captured = {}

    class FakeSpeechToText:
        def __init__(self, cfg, out_format):
            captured["cfg"] = cfg
            captured["out_format"] = out_format
            self.cfg = cfg
            self.cfg.target_sub = f"{cfg.target_dir}/{cfg.noextname}.srt"

        def prepare(self):
            Path(self.cfg.target_dir).mkdir(parents=True, exist_ok=True)

        def recogn(self):
            Path(self.cfg.target_sub).write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nNi hao\n",
                encoding="utf-8",
            )

        def diariz(self):
            captured["diariz"] = True

        def task_done(self):
            captured["done"] = True

    monkeypatch.setattr(recognition, "is_allow_lang", lambda **kwargs: True)
    monkeypatch.setattr(recognition, "is_input_api", lambda **kwargs: True)
    monkeypatch.setattr(speech_module, "SpeechToText", FakeSpeechToText)

    result = transcribe_video(
        video,
        tmp_path / "recognition",
        source_language="Ti\u1ebfng Trung",
        recogn_type=3,
        model_name="model-a",
        remove_noise=True,
        enable_diariz=True,
        nums_diariz=2,
        is_cuda=True,
    )

    assert result.is_file()
    assert captured["cfg"].detect_language == "zh-cn"
    assert captured["cfg"].recogn_type == 3
    assert captured["cfg"].model_name == "model-a"
    assert captured["cfg"].remove_noise is True
    assert captured["cfg"].enable_diariz is True
    assert captured["cfg"].nums_diariz == 2
    assert captured["cfg"].is_cuda is True
    assert captured["out_format"] == "srt"
    assert captured["diariz"] is True
    assert captured["done"] is True


def test_batch_worker_accepts_video_without_srt(monkeypatch, tmp_path):
    import videotrans.process.batch_pipeline as pipeline
    from videotrans.component import video_batch_workflow as workflow

    video = tmp_path / "video-only.mp4"
    video.write_bytes(b"video")
    calls = {}

    def fake_transcribe(source, output_dir, **kwargs):
        calls["source"] = Path(source)
        calls["kwargs"] = kwargs
        target = Path(output_dir) / "video-only.srt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nNi hao\n",
            encoding="utf-8",
        )
        return target

    monkeypatch.setattr(pipeline, "transcribe_video", fake_transcribe)
    monkeypatch.setattr(
        workflow.tools,
        "get_video_info",
        lambda path: {"width": 1080, "height": 1920, "streams_audio": 1},
    )
    item = {
        "video": video.as_posix(),
        "srt": "",
        "edit": {
            "enabled": False,
            "subtitle": {"enabled": False},
        },
    }
    config = {
        "auto_stt": True,
        "recogn_type": 0,
        "recogn_model": "small",
        "source_language": "Ti\u1ebfng Trung",
        "translation_mode": "disabled",
        "subtitle_language": "Gi\u1eef nguy\u00ean",
        "voice_language": "Kh\u00f4ng l\u1ed3ng ti\u1ebfng",
    }
    worker = workflow.BatchWorkflowWorker([item], tmp_path / "output", config)

    worker._process_item(worker.items[0], 1, 1)

    result = worker.items[0]
    assert calls["source"] == video
    assert calls["kwargs"]["model_name"] == "small"
    assert Path(result["recognized_srt"]).is_file()
    assert result["stages"]["recognition"]["status"] == "completed"
    assert result["stages"]["source"]["status"] == "completed"
    assert Path(result["final_video"]) == video

def test_batch_worker_prepares_idempotent_reup_job(monkeypatch, tmp_path):
    from videotrans.component import video_batch_workflow as workflow
    from videotrans.process.publish_queue import load_publish_queue

    video = tmp_path / "publish.mp4"
    video.write_bytes(b"video")
    srt = tmp_path / "publish.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        workflow.tools,
        "get_video_info",
        lambda path: {"width": 1080, "height": 1920, "streams_audio": 1},
    )
    item = {
        "video": video.as_posix(),
        "srt": srt.as_posix(),
        "source_url": "https://www.douyin.com/video/123",
        "edit": {"enabled": False, "subtitle": {"enabled": False}},
    }
    config = {
        "translation_mode": "disabled",
        "source_language": "en",
        "subtitle_language": "Gi\u1eef nguy\u00ean",
        "voice_language": "Kh\u00f4ng l\u1ed3ng ti\u1ebfng",
        "prepare_reup": True,
        "reup_platforms": ["Shorts", "TikTok", "Douyin"],
        "reup_title": "{stem} vietsub",
        "reup_description": "Translated clip",
        "reup_hashtags": "#vietsub #viral",
    }
    worker = workflow.BatchWorkflowWorker([item], tmp_path / "output", config)

    worker._process_item(worker.items[0], 1, 1)
    first_job_id = worker.items[0]["publish_job_id"]
    worker._process_item(worker.items[0], 1, 1)

    queue_path = tmp_path / "output" / "reup_queue.json"
    queue = load_publish_queue(queue_path)
    assert queue_path.is_file()
    assert len(queue["jobs"]) == 1
    assert queue["jobs"][0]["id"] == first_job_id
    assert queue["jobs"][0]["title"] == "publish vietsub"
    assert queue["jobs"][0]["source_url"] == "https://www.douyin.com/video/123"
    assert set(queue["jobs"][0]["platforms"]) == {"youtube", "tiktok", "douyin"}
    assert worker.items[0]["stages"]["publish_prepare"]["status"] == "completed"
    assert "1 job d\u00f9ng l\u1ea1i" in worker.items[0]["stages"]["publish_prepare"]["detail"]

def test_vertical_reframe_command_and_publish_job_split(monkeypatch, tmp_path):
    from videotrans.component import video_batch_workflow as workflow
    from videotrans.process.batch_pipeline import build_vertical_reframe_command
    from videotrans.process.publish_queue import load_publish_queue

    blur_command = build_vertical_reframe_command("wide.mp4", "vertical.mp4", strategy="blur")
    graph = blur_command[blur_command.index("-filter_complex") + 1]
    assert "crop=1080:1920" in graph
    assert "boxblur=20:10" in graph
    assert "overlay=(W-w)/2:(H-h)/2" in graph
    assert "0:a?" in blur_command

    video = tmp_path / "wide.mp4"
    video.write_bytes(b"wide-video")
    srt = tmp_path / "wide.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        workflow.tools,
        "get_video_info",
        lambda path: {"width": 1920, "height": 1080, "streams_audio": 1},
    )
    item = {
        "video": video.as_posix(),
        "srt": srt.as_posix(),
        "edit": {"enabled": False, "subtitle": {"enabled": False}},
    }
    config = {
        "translation_mode": "disabled",
        "source_language": "en",
        "subtitle_language": "Gi\u1eef nguy\u00ean",
        "voice_language": "Kh\u00f4ng l\u1ed3ng ti\u1ebfng",
        "prepare_reup": True,
        "reup_platforms": ["tiktok", "bilibili"],
        "reup_vertical": True,
        "reup_vertical_strategy": "blur",
    }
    worker = workflow.BatchWorkflowWorker([item], tmp_path / "output", config)

    def fake_ffmpeg(command):
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"vertical-video")

    monkeypatch.setattr(worker, "_run_ffmpeg", fake_ffmpeg)
    worker._process_item(worker.items[0], 1, 1)

    result = worker.items[0]
    queue = load_publish_queue(tmp_path / "output" / "reup_queue.json")
    assert Path(result["vertical_video"]).is_file()
    assert result["stages"]["vertical_reframe"]["status"] == "completed"
    assert len(result["publish_job_ids"]) == 2
    assert len(queue["jobs"]) == 2
    platform_sets = {frozenset(job["platforms"]) for job in queue["jobs"]}
    assert platform_sets == {frozenset({"tiktok"}), frozenset({"bilibili"})}
    vertical_job = next(job for job in queue["jobs"] if "tiktok" in job["platforms"])
    assert Path(vertical_job["video"]).name.endswith("_9x16.mp4")
    assert vertical_job["warnings"] == []

def test_vertical_reframe_real_ffmpeg_smoke(tmp_path):
    import shutil
    import subprocess

    import pytest

    from videotrans.process.batch_pipeline import build_vertical_reframe_command
    from videotrans.util import tools

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not available")
    source = tmp_path / "landscape.mp4"
    target = tmp_path / "portrait.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=320x180:d=0.5",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            source.as_posix(),
        ],
        check=True,
    )
    command = build_vertical_reframe_command(
        source,
        target,
        strategy="blur",
        width=180,
        height=320,
    )

    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *command],
        check=True,
    )

    info = tools.get_video_info(target.as_posix())
    assert target.is_file() and target.stat().st_size > 0
    assert info["width"] == 180
    assert info["height"] == 320
    assert info["streams_audio"] >= 1
