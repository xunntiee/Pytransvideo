from videotrans.process.download_sources import (
    build_download_jobs,
    detect_platform,
    extract_urls,
    platform_catalog,
    platform_options,
)


def test_extract_urls_from_chinese_share_text_and_deduplicates():
    text = (
        "复制打开抖音，看看【作者的作品】 https://v.douyin.com/abc123/ 03/15\n"
        "小红书分享：https://xhslink.com/a1b2c3，欢迎查看。\n"
        "重复 https://v.douyin.com/abc123/"
    )

    assert extract_urls(text) == [
        "https://v.douyin.com/abc123/",
        "https://xhslink.com/a1b2c3",
    ]


def test_detects_major_chinese_video_platforms():
    cases = {
        "https://www.douyin.com/video/123": "douyin",
        "https://v.kuaishou.com/abc": "kuaishou",
        "https://www.bilibili.com/video/BV1xx": "bilibili",
        "https://www.xiaohongshu.com/explore/123": "xiaohongshu",
        "https://weibo.com/tv/show/123": "weibo",
        "https://www.ixigua.com/123": "xigua",
        "https://v.youku.com/v_show/id_x.html": "youku",
        "https://www.iqiyi.com/v_abc.html": "iqiyi",
        "https://v.qq.com/x/cover/a/b.html": "tencent",
        "https://www.acfun.cn/v/ac123": "acfun",
        "https://www.douyu.com/123": "douyu",
        "https://www.huya.com/123": "huya",
    }

    assert {url: detect_platform(url).id for url in cases} == cases


def test_unknown_site_uses_generic_ytdlp_fallback():
    platform = detect_platform("https://videos.example.org/watch/1")

    assert platform.id == "generic"
    assert platform.adapter == "yt-dlp"
    assert platform_options(platform.id) == {}


def test_platform_options_supply_platform_referer():
    options = platform_options("xiaohongshu")

    assert options["http_headers"]["Referer"] == "https://www.xiaohongshu.com/"
    assert "Mozilla/5.0" in options["http_headers"]["User-Agent"]


def test_download_jobs_include_adapter_and_cookie_requirements():
    jobs = build_download_jobs(
        [
            "https://www.douyin.com/video/123",
            "https://www.bilibili.com/video/BV1xx",
        ]
    )

    assert jobs[0]["platform_name"] == "Douyin"
    assert jobs[0]["adapter"] == "native+yt-dlp"
    assert jobs[0]["needs_cookie"] is True
    assert jobs[1]["platform_id"] == "bilibili"
    assert jobs[1]["needs_cookie"] is False


def test_catalog_has_broad_chinese_platform_coverage():
    ids = {item["id"] for item in platform_catalog()}

    assert len(ids) >= 17
    assert {
        "douyin",
        "kuaishou",
        "bilibili",
        "xiaohongshu",
        "weibo",
        "xigua",
        "youku",
        "iqiyi",
        "tencent",
        "mgtv",
        "acfun",
        "haokan",
        "douyu",
        "huya",
    }.issubset(ids)

def test_downloader_applies_platform_headers_and_parses_share_text(tmp_path):
    from videotrans.component.video_downloader import (
        FORMAT_OPTIONS,
        VideoDownloadWindow,
        _build_ydl_options,
    )

    options = _build_ydl_options(
        output_dir=tmp_path,
        format_name=next(iter(FORMAT_OPTIONS)),
        threads=2,
        cookie_browser="",
        cookie_file="",
        use_archive=False,
        playlist_mode=False,
        playlist_limit=0,
        platform_id="bilibili",
    )
    urls = VideoDownloadWindow._parse_urls(
        None,
        "分享视频 https://www.bilibili.com/video/BV1xx，复制后打开。",
    )

    assert options["http_headers"]["Referer"] == "https://www.bilibili.com/"
    assert urls == ["https://www.bilibili.com/video/BV1xx"]

def test_download_worker_groups_platforms_and_keeps_job_metadata(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    from videotrans.component.video_downloader import FORMAT_OPTIONS, VideoDownloadWorker

    calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def download(self, urls):
            calls.append((self.options, list(urls)))

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    urls = [
        "https://www.bilibili.com/video/BV1xx",
        "https://www.xiaohongshu.com/explore/123",
    ]
    worker = VideoDownloadWorker(
        urls=urls,
        output_dir=tmp_path,
        format_name=next(iter(FORMAT_OPTIONS)),
        cookie_browser="",
        cookie_file="",
        use_archive=False,
        playlist_mode=False,
        playlist_limit=0,
    )

    worker.run()

    assert [item["platform_id"] for item in worker.items] == ["bilibili", "xiaohongshu"]
    assert len(calls) == 2
    referers = {options["http_headers"]["Referer"] for options, _urls in calls}
    assert referers == {"https://www.bilibili.com/", "https://www.xiaohongshu.com/"}
    assert [group_urls for _options, group_urls in calls] == [[urls[0]], [urls[1]]]

def test_finished_downloads_flow_directly_into_batch(tmp_path):
    from PySide6 import QtWidgets

    from videotrans.component.video_batch_workflow import VideoBatchWorkflowWindow
    from videotrans.component.video_downloader import VideoDownloadWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    video = tmp_path / "downloaded.mp4"
    video.write_bytes(b"video")
    downloader = VideoDownloadWindow()
    batch = VideoBatchWorkflowWindow()
    downloader.pipeline_requested.connect(batch.add_video_files)
    downloader._add_finished_file("Downloaded", video, "https://www.douyin.com/video/123")

    downloader._send_finished_to_pipeline()
    app.processEvents()

    assert batch.file_page.video_files == [video.resolve().as_posix()]
    assert len(batch.file_page.items) == 1
    assert batch.file_page.items[0]["video"] == video.resolve().as_posix()
    assert batch.file_page.items[0]["srt"] == ""
    assert batch.file_page.items[0]["source_url"] == "https://www.douyin.com/video/123"
    batch.file_page._rematch()
    assert batch.file_page.items[0]["source_url"] == "https://www.douyin.com/video/123"
    assert "tự lấy SRT" in batch.file_page.items[0]["status"]
