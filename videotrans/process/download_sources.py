# -*- coding: utf-8 -*-
"""Platform registry and URL normalization for the unified downloader."""

from dataclasses import asdict, dataclass
import re
from urllib.parse import urlparse


@dataclass(frozen=True)
class PlatformSpec:
    id: str
    name: str
    domains: tuple[str, ...]
    adapter: str = "yt-dlp"
    referer: str = ""
    needs_cookie: bool = False
    note: str = ""


PLATFORMS = (
    PlatformSpec("douyin", "Douyin", ("douyin.com", "iesdouyin.com"), "native+yt-dlp", "https://www.douyin.com/", True),
    PlatformSpec("kuaishou", "Kuaishou", ("kuaishou.com", "gifshow.com"), referer="https://www.kuaishou.com/", needs_cookie=True),
    PlatformSpec("bilibili", "Bilibili", ("bilibili.com", "b23.tv"), referer="https://www.bilibili.com/"),
    PlatformSpec("xiaohongshu", "Xiaohongshu", ("xiaohongshu.com", "xhslink.com"), referer="https://www.xiaohongshu.com/", needs_cookie=True),
    PlatformSpec("weibo", "Weibo", ("weibo.com", "weibo.cn"), referer="https://weibo.com/", needs_cookie=True),
    PlatformSpec("xigua", "Xigua Video", ("ixigua.com", "xigua.com"), referer="https://www.ixigua.com/"),
    PlatformSpec("toutiao", "Toutiao", ("toutiao.com",), referer="https://www.toutiao.com/"),
    PlatformSpec("youku", "Youku", ("youku.com",), referer="https://v.youku.com/"),
    PlatformSpec("iqiyi", "iQIYI", ("iqiyi.com",), referer="https://www.iqiyi.com/"),
    PlatformSpec("tencent", "Tencent Video", ("v.qq.com",), referer="https://v.qq.com/"),
    PlatformSpec("mgtv", "Mango TV", ("mgtv.com",), referer="https://www.mgtv.com/"),
    PlatformSpec("acfun", "AcFun", ("acfun.cn",), referer="https://www.acfun.cn/"),
    PlatformSpec("haokan", "Haokan", ("haokan.baidu.com",), referer="https://haokan.baidu.com/"),
    PlatformSpec("sohu", "Sohu Video", ("tv.sohu.com", "my.tv.sohu.com"), referer="https://tv.sohu.com/"),
    PlatformSpec("pearvideo", "Pear Video", ("pearvideo.com",), referer="https://www.pearvideo.com/"),
    PlatformSpec("douyu", "Douyu", ("douyu.com",), referer="https://www.douyu.com/", needs_cookie=True),
    PlatformSpec("huya", "Huya", ("huya.com",), referer="https://www.huya.com/", needs_cookie=True),
    PlatformSpec("youtube", "YouTube", ("youtube.com", "youtu.be"), referer="https://www.youtube.com/"),
    PlatformSpec("tiktok", "TikTok", ("tiktok.com",), referer="https://www.tiktok.com/"),
)

GENERIC_PLATFORM = PlatformSpec("generic", "Website", (), note="Use the yt-dlp generic extractor.")
_URL_RE = re.compile(r"https?://[^\s<>\"'，。；：！？）》】」』、]+", flags=re.I)
_TRAILING_PUNCTUATION = ".,;:!?)]}，。；：！？）》】」』"


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def detect_platform(url: str) -> PlatformSpec:
    host = (urlparse(str(url or "").strip()).hostname or "").lower()
    for platform in PLATFORMS:
        if any(_host_matches(host, domain) for domain in platform.domains):
            return platform
    return GENERIC_PLATFORM


def extract_urls(text: str) -> list[str]:
    """Extract and de-duplicate URLs from plain lines or Chinese share text."""
    urls = []
    seen = set()
    for match in _URL_RE.findall(str(text or "")):
        url = match.rstrip(_TRAILING_PUNCTUATION)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def platform_options(platform_or_url) -> dict:
    platform = (
        detect_platform(platform_or_url)
        if isinstance(platform_or_url, str) and "://" in platform_or_url
        else next((item for item in PLATFORMS if item.id == platform_or_url), GENERIC_PLATFORM)
    )
    if not platform.referer:
        return {}
    return {
        "http_headers": {
            "Referer": platform.referer,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        }
    }


def build_download_jobs(urls) -> list[dict]:
    jobs = []
    for url in urls:
        platform = detect_platform(url)
        jobs.append(
            {
                "url": str(url).strip(),
                "platform_id": platform.id,
                "platform_name": platform.name,
                "adapter": platform.adapter,
                "needs_cookie": platform.needs_cookie,
            }
        )
    return jobs


def platform_catalog() -> list[dict]:
    return [asdict(platform) for platform in PLATFORMS]
