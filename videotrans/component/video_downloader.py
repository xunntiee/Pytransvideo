# -*- coding: utf-8 -*-
import base64
import hashlib
import http.cookiejar
import os
import random
import re
import string
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QTextCursor

from videotrans.configure.config import HOME_DIR, ROOT_DIR, app_cfg, settings


DEFAULT_DOWNLOAD_DIR = Path(ROOT_DIR) / "input"
DOUYIN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
ROLE_DOWNLOAD_URL = Qt.UserRole + 1
ROLE_COVER_URL = Qt.UserRole + 2
ROLE_FILE_PATH = Qt.UserRole + 3
ROLE_SOURCE = Qt.UserRole + 4

FORMAT_OPTIONS = {
    "MP4 tốt nhất": {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
    },
    "MP4 720p": {
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best",
        "merge_output_format": "mp4",
    },
    "Audio MP3": {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    },
    "Giữ định dạng gốc": {
        "format": "bestvideo+bestaudio/best",
    },
}

COOKIE_BROWSERS = {
    "Không dùng cookie": "",
    "Chrome": "chrome",
    "Edge": "edge",
    "Firefox": "firefox",
}


class DownloadCancelled(Exception):
    pass


def _is_http_url(text):
    return bool(re.match(r"^https?://", text.strip(), flags=re.I))


def _is_douyin_url(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    return host.endswith("douyin.com") or host.endswith("iesdouyin.com")


def _douyin_sec_user_id(url):
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "user":
        return parts[1]
    return ""


def _douyin_video_id(url):
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"video", "note"} and parts[1].isdigit():
        return parts[1]

    query = urllib.parse.parse_qs(parsed.query)
    for key in ("vid", "modal_id", "aweme_id", "item_ids"):
        value = (query.get(key) or [""])[0]
        if value.isdigit():
            return value
    return ""


def _normalize_douyin_video_url(url):
    video_id = _douyin_video_id(url)
    return f"https://www.douyin.com/video/{video_id}" if video_id else url


def _url_list(value):
    if not isinstance(value, dict):
        return []
    return [url for url in value.get("url_list") or [] if _is_http_url(url)]


def _douyin_media_url(aweme):
    video = aweme.get("video") or {}
    candidates = []

    bit_rates = video.get("bit_rate") or []
    bit_rates = sorted(bit_rates, key=lambda item: item.get("bit_rate") or 0, reverse=True)
    for item in bit_rates:
        candidates.extend(_url_list(item.get("play_addr")))

    for key in ("play_addr_h264", "play_addr", "play_addr_265"):
        candidates.extend(_url_list(video.get(key)))

    return candidates[0] if candidates else ""


def _douyin_cover_url(aweme):
    video = aweme.get("video") or {}
    for key in ("cover", "origin_cover", "dynamic_cover"):
        urls = _url_list(video.get(key))
        if urls:
            return urls[0]
    return ""


def _safe_filename(value, fallback="video", max_length=160):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "").strip(" .")
    value = re.sub(r"\s+", " ", value)
    if not value:
        value = fallback
    return value[:max_length].strip(" .") or fallback


def _extension_from_response(response, url, default=".mp4"):
    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
    if content_type == "video/mp4":
        return ".mp4"
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".mp3", ".m4a"}:
        return suffix
    return default


def _random_token(length=120):
    alphabet = string.ascii_letters + string.digits + "_-"
    return "".join(random.choice(alphabet) for _ in range(length))


def _cookie_header_from_jar(cookie_jar, domain_keyword="douyin"):
    pairs = []
    for cookie in cookie_jar:
        if domain_keyword in cookie.domain:
            pairs.append(f"{cookie.name}={cookie.value}")
    return "; ".join(pairs)


def _load_browser_cookie_header(browser):
    if not browser:
        return ""
    try:
        from yt_dlp.cookies import load_cookies

        cookie_jar = load_cookies(None, (browser,), None)
    except Exception as e:
        raise ValueError(
            f"Không đọc được cookie từ {browser}. Nếu trình duyệt đang mở, Edge/Chrome có thể khóa file cookie. "
            "Hãy đóng trình duyệt rồi thử lại, chạy app bằng quyền Admin, hoặc export cookies ra file cookies.txt."
        ) from e
    return _cookie_header_from_jar(cookie_jar)


def _load_cookie_file_header(cookie_file, domain_keyword="douyin"):
    if not cookie_file:
        return ""
    path = Path(cookie_file)
    if not path.exists():
        raise ValueError(f"File cookies không tồn tại: {cookie_file}")
    cookie_jar = http.cookiejar.MozillaCookieJar()
    cookie_jar.load(str(path), ignore_discard=True, ignore_expires=True)
    return _cookie_header_from_jar(cookie_jar, domain_keyword=domain_keyword)


def _load_cookie_header(browser, cookie_file):
    if cookie_file:
        header = _load_cookie_file_header(cookie_file)
        if not header:
            raise ValueError("File cookies không có cookie Douyin. Hãy export cookies khi đang mở trang douyin.com.")
        return header
    if browser:
        header = _load_browser_cookie_header(browser)
        if not header:
            raise ValueError(
                f"Không tìm thấy cookie Douyin trong {browser}. Hãy mở douyin.com trong đúng profile hoặc dùng file cookies.txt."
            )
        return header
    return ""


def _cookie_header_values(cookie_header):
    values = {}
    for part in (cookie_header or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        values[name.strip()] = value.strip()
    return values


def _generate_douyin_webid():
    seed = f"{time.time_ns()}-{random.random()}-{os.getpid()}"
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:19]


def _douyin_apply_cookie_params(params, cookie_header):
    cookies = _cookie_header_values(cookie_header)
    ms_token = cookies.get("msToken") or cookies.get("xmst")
    if ms_token:
        params["msToken"] = ms_token
    params["webid"] = cookies.get("webid") or _generate_douyin_webid()
    verify_fp = cookies.get("s_v_web_id")
    if verify_fp:
        params["verifyFp"] = verify_fp
        params["fp"] = verify_fp
    return params


def _default_cookie_file():
    saved = settings.get("video_downloader_cookie_file", "")
    candidates = [
        saved,
        "D:/www.douyin.com_cookies.txt",
        "D:/douyin_cookies.txt",
        str(Path(HOME_DIR) / "www.douyin.com_cookies.txt"),
        str(Path.home() / "Downloads" / "www.douyin.com_cookies.txt"),
        str(Path.home() / "Downloads" / "douyin_cookies.txt"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate).as_posix()
    downloads = Path.home() / "Downloads"
    if downloads.exists():
        for pattern in ("*douyin*cookie*.txt", "*www.douyin.com*.txt"):
            matches = sorted(downloads.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
            if matches:
                return matches[0].as_posix()
    return ""


def _strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text or "")


class _DouyinXBogus:
    # Adapted from F2's XBogus implementation (Apache-2.0).
    def __init__(self, user_agent):
        self.array = [
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None,
            0, 1, 2, 3, 4, 5, 6, 7, 8, 9, None, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None, None, 10, 11, 12, 13, 14, 15,
        ]
        self.character = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
        self.ua_key = b"\x00\x01\x0c"
        self.user_agent = user_agent

    def md5_str_to_array(self, md5_str):
        if isinstance(md5_str, str) and len(md5_str) > 32:
            return [ord(char) for char in md5_str]
        array = []
        idx = 0
        while idx < len(md5_str):
            array.append((self.array[ord(md5_str[idx])] << 4) | self.array[ord(md5_str[idx + 1])])
            idx += 2
        return array

    def md5(self, input_data):
        if isinstance(input_data, str):
            array = self.md5_str_to_array(input_data)
        elif isinstance(input_data, list):
            array = input_data
        else:
            raise ValueError("Invalid input type. Expected str or list.")
        md5_hash = hashlib.md5()
        md5_hash.update(bytes(array))
        return md5_hash.hexdigest()

    def md5_encrypt(self, url_params):
        return self.md5_str_to_array(self.md5(self.md5_str_to_array(self.md5(url_params))))

    @staticmethod
    def encoding_conversion(a, b, c, e, d, t, f, r, n, o, i, _, x, u, s, l, v, h, p):
        y = [a]
        y.append(int(i))
        y.extend([b, _, c, x, e, u, d, s, t, l, f, v, r, h, n, p, o])
        return bytes(y).decode("ISO-8859-1")

    @staticmethod
    def encoding_conversion2(a, b, c):
        return chr(a) + chr(b) + c

    @staticmethod
    def rc4_encrypt(key, data):
        s = list(range(256))
        j = 0
        encrypted_data = bytearray()
        for i in range(256):
            j = (j + s[i] + key[i % len(key)]) % 256
            s[i], s[j] = s[j], s[i]
        i = j = 0
        for byte in data:
            i = (i + 1) % 256
            j = (j + s[i]) % 256
            s[i], s[j] = s[j], s[i]
            encrypted_data.append(byte ^ s[(s[i] + s[j]) % 256])
        return encrypted_data

    def calculation(self, a1, a2, a3):
        x3 = ((a1 & 255) << 16) | ((a2 & 255) << 8) | a3
        return (
            self.character[(x3 & 16515072) >> 18]
            + self.character[(x3 & 258048) >> 12]
            + self.character[(x3 & 4032) >> 6]
            + self.character[x3 & 63]
        )

    def sign(self, url_params):
        array1 = self.md5_str_to_array(
            self.md5(
                base64.b64encode(
                    self.rc4_encrypt(self.ua_key, self.user_agent.encode("ISO-8859-1"))
                ).decode("ISO-8859-1")
            )
        )
        array2 = self.md5_str_to_array(self.md5(self.md5_str_to_array("d41d8cd98f00b204e9800998ecf8427e")))
        url_params_array = self.md5_encrypt(url_params)

        timer = int(time.time())
        ct = 536919696
        new_array = [
            64, 0.00390625, 1, 12,
            url_params_array[14], url_params_array[15], array2[14], array2[15], array1[14], array1[15],
            timer >> 24 & 255, timer >> 16 & 255, timer >> 8 & 255, timer & 255,
            ct >> 24 & 255, ct >> 16 & 255, ct >> 8 & 255, ct & 255,
        ]
        xor_result = new_array[0]
        for value in new_array[1:]:
            xor_result ^= int(value)
        new_array.append(xor_result)

        array3 = []
        array4 = []
        idx = 0
        while idx < len(new_array):
            array3.append(new_array[idx])
            if idx + 1 < len(new_array):
                array4.append(new_array[idx + 1])
            idx += 2
        merge_array = array3 + array4
        garbled_code = self.encoding_conversion2(
            2,
            255,
            self.rc4_encrypt(
                bytes([255]),
                self.encoding_conversion(*merge_array).encode("ISO-8859-1"),
            ).decode("ISO-8859-1"),
        )
        xbogus = ""
        idx = 0
        while idx < len(garbled_code):
            xbogus += self.calculation(ord(garbled_code[idx]), ord(garbled_code[idx + 1]), ord(garbled_code[idx + 2]))
            idx += 3
        return xbogus


def _douyin_base_api_params():
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "version_code": "290100",
        "version_name": "29.1.0",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "130.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "130.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "12",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "from_user_page": "1",
        "locate_query": "false",
        "need_time_list": "1",
        "pc_libra_divert": "Windows",
        "publish_video_strategy_type": "2",
        "round_trip_time": "0",
        "show_live_replay_strategy": "1",
        "time_list_query": "0",
        "whale_cut_token": "",
        "update_version_code": "170400",
        "msToken": "",
    }


def _douyin_post_api_params(sec_user_id, max_cursor, count):
    params = _douyin_base_api_params()
    params.update(
        {
            "sec_user_id": sec_user_id,
            "max_cursor": str(max_cursor),
            "count": str(count),
        }
    )
    return params


def _douyin_detail_api_params(aweme_id):
    params = _douyin_base_api_params()
    params["aweme_id"] = str(aweme_id)
    return params


def _douyin_aweme_url(aweme):
    aweme_id = str(aweme.get("aweme_id") or aweme.get("id") or "")
    return f"https://www.douyin.com/video/{aweme_id}" if aweme_id else ""


def _douyin_aweme_title(aweme):
    return aweme.get("desc") or aweme.get("share_info", {}).get("share_title") or _douyin_aweme_url(aweme)


def _to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _format_count(value):
    value = _to_int(value)
    if value >= 100000000:
        return f"{value / 100000000:.1f}亿"
    if value >= 10000:
        return f"{value / 10000:.1f}万"
    return str(value)


def _douyin_aweme_stats(aweme):
    stats = aweme.get("statistics") or {}
    return {
        "digg": _to_int(stats.get("digg_count") or stats.get("digg")),
        "comment": _to_int(stats.get("comment_count") or stats.get("comment")),
        "share": _to_int(stats.get("share_count") or stats.get("share")),
        "collect": _to_int(stats.get("collect_count") or stats.get("collect")),
        "play": _to_int(
            stats.get("play_count")
            or stats.get("play")
            or aweme.get("play_count")
            or aweme.get("video", {}).get("play_count")
        ),
    }


def _douyin_viral_score(aweme):
    stats = _douyin_aweme_stats(aweme)
    engagement = (
        stats["digg"]
        + stats["comment"] * 4
        + stats["share"] * 6
        + stats["collect"] * 3
        + stats["play"] * 0.02
    )
    create_time = _to_int(aweme.get("create_time"))
    age_hours = max((time.time() - create_time) / 3600, 1) if create_time else 24 * 30
    return int(engagement / (age_hours ** 0.35))


def _douyin_stats_text(item):
    return (
        f"Like {_format_count(item.get('digg', 0))} | "
        f"Cmt {_format_count(item.get('comment', 0))} | "
        f"Share {_format_count(item.get('share', 0))} | "
        f"Save {_format_count(item.get('collect', 0))}"
    )


def _douyin_signed_endpoint(base_endpoint, params):
    from videotrans.component.douyin_abogus import ABogus

    signed_params = dict(params)
    signed_params["msToken"] = ""
    query = urllib.parse.urlencode(signed_params)
    a_bogus = urllib.parse.quote(ABogus().get_value(signed_params), safe="")
    return f"{base_endpoint}?{query}&a_bogus={a_bogus}"


def _douyin_headers(referer, cookie_browser, cookie_file):
    headers = {
        "User-Agent": DOUYIN_USER_AGENT,
        "Referer": referer or "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    cookie_header = _load_cookie_header(cookie_browser, cookie_file)
    if not cookie_header:
        raise ValueError(
            "Douyin cần cookie đăng nhập để đọc metadata video. Hãy dùng file cookies.txt hoặc chọn Edge/Chrome đã đăng nhập."
        )
    headers["Cookie"] = cookie_header
    return headers


def _douyin_aweme_item(aweme, index=1):
    aweme_url = _douyin_aweme_url(aweme)
    author = aweme.get("author") or {}
    stats = _douyin_aweme_stats(aweme)
    viral_score = _douyin_viral_score(aweme)
    return {
        "index": index,
        "title": _douyin_aweme_title(aweme),
        "uploader": author.get("nickname") or author.get("unique_id") or "Douyin",
        "url": aweme_url,
        "download_url": _douyin_media_url(aweme),
        "cover_url": _douyin_cover_url(aweme),
        "source": "douyin",
        "viral_score": viral_score,
        "digg": stats["digg"],
        "comment": stats["comment"],
        "share": stats["share"],
        "collect": stats["collect"],
        "play": stats["play"],
        "create_time": _to_int(aweme.get("create_time")),
    }


def _douyin_search_api_params(keyword, offset, count, search_id="", search_channel="aweme_general", sort_type=0, publish_time=0):
    import json

    params = _douyin_base_api_params()
    params.update(
        {
            "search_channel": search_channel,
            "enable_history": "1",
            "keyword": keyword,
            "search_source": "tab_search",
            "query_correct_type": "1",
            "is_filter_search": "0",
            "from_group_id": "7378810571505847586",
            "offset": str(offset),
            "count": str(count),
            "need_filter_settings": "1",
            "list_type": "multi",
            "search_id": search_id,
        }
    )
    if sort_type or publish_time:
        params["filter_selected"] = json.dumps(
            {"sort_type": str(sort_type), "publish_time": str(publish_time)},
            separators=(",", ":"),
        )
        params["is_filter_search"] = "1"
    return params


def _iter_douyin_search_awemes(search_items):
    for item in search_items or []:
        aweme = item.get("aweme_info")
        if isinstance(aweme, dict):
            yield aweme

        for aweme in item.get("aweme_list") or []:
            if isinstance(aweme, dict):
                yield aweme

        mix_info = item.get("aweme_mix_info") or {}
        for aweme in mix_info.get("mix_items") or []:
            if isinstance(aweme, dict):
                yield aweme


def _fetch_douyin_keyword_search(
    keyword,
    *,
    limit,
    cookie_browser,
    cookie_file,
    stopped,
    sort_type=0,
    publish_time=0,
):
    import requests

    keyword = keyword.strip()
    if not keyword:
        raise ValueError("Hãy nhập từ khóa Douyin.")

    session = requests.Session()
    referer = f"https://www.douyin.com/search/{urllib.parse.quote(keyword)}?type=general"
    session.headers.update(_douyin_headers(referer, cookie_browser, cookie_file))

    max_items = max(1, int(limit or 30))
    page_size = min(15, max_items)
    seen = set()
    found = []
    search_id = ""
    blocked_reason = ""
    endpoint = "https://www.douyin.com/aweme/v1/web/general/search/single/"

    for search_channel in ("aweme_video_web", "aweme_general"):
        offset = 0
        search_id = ""
        while len(found) < max_items:
            if stopped():
                raise DownloadCancelled("Đã dừng tìm kiếm Douyin.")

            params = _douyin_search_api_params(
                keyword,
                offset,
                page_size,
                search_id=search_id,
                search_channel=search_channel,
                sort_type=sort_type,
                publish_time=publish_time,
            )
            _douyin_apply_cookie_params(params, session.headers.get("Cookie", ""))
            response = session.get(f"{endpoint}?{urllib.parse.urlencode(params)}", timeout=25)
            response.raise_for_status()
            if not response.text.strip():
                break

            data = response.json()
            if data.get("status_code") not in (0, None):
                raise ValueError(data.get("status_msg") or data.get("message") or "Douyin search API trả lỗi.")

            raw_items = data.get("data") or []
            nil_info = data.get("search_nil_info") or {}
            nil_type = nil_info.get("search_nil_type") or nil_info.get("search_nil_item")
            if nil_type == "verify_check":
                blocked_reason = (
                    "Douyin search đang yêu cầu verify_check/risk-control. "
                    "Hãy mở Douyin trên cùng tài khoản để xác minh, giảm số lượng/tần suất tìm, "
                    "hoặc dùng proxy/IP phù hợp rồi thử lại."
                )
            page_count = 0
            for aweme in _iter_douyin_search_awemes(raw_items):
                aweme_url = _douyin_aweme_url(aweme)
                if not aweme_url or aweme_url in seen:
                    continue
                seen.add(aweme_url)
                found.append(_douyin_aweme_item(aweme, index=len(found) + 1))
                page_count += 1
                if len(found) >= max_items:
                    break

            search_id = data.get("extra", {}).get("logid", "") or search_id
            if not data.get("has_more") or not raw_items:
                break
            next_offset = _to_int(data.get("cursor"), offset + page_size)
            offset = next_offset if next_offset > offset else offset + page_size

            if page_count == 0 and offset > max_items * 3:
                break

        if found:
            break

    found.sort(key=lambda item: item.get("viral_score", 0), reverse=True)
    for index, item in enumerate(found, start=1):
        item["index"] = index
    if not found and blocked_reason:
        raise ValueError(blocked_reason)
    return found[:max_items]


def _fetch_douyin_video_detail(url, *, cookie_browser, cookie_file):
    import requests

    aweme_id = _douyin_video_id(url)
    if not aweme_id:
        raise ValueError("URL Douyin không có aweme_id/modal_id/vid.")

    params = _douyin_detail_api_params(aweme_id)
    endpoint = _douyin_signed_endpoint("https://www.douyin.com/aweme/v1/web/aweme/detail/", params)
    response = requests.get(endpoint, headers=_douyin_headers(url, cookie_browser, cookie_file), timeout=25)
    response.raise_for_status()
    if not response.text.strip():
        raise ValueError("Douyin trả dữ liệu rỗng khi đọc chi tiết video. Hãy kiểm tra cookie.")

    data = response.json()
    if data.get("status_code") not in (0, None):
        raise ValueError(data.get("status_msg") or data.get("message") or "Douyin API trả lỗi khi đọc chi tiết video.")

    aweme = data.get("aweme_detail") or data.get("aweme") or {}
    if not aweme:
        raise ValueError("Douyin không trả metadata video.")
    item = _douyin_aweme_item(aweme)
    if not item.get("download_url"):
        raise ValueError("Douyin không trả link CDN video.")
    return item


def _fetch_douyin_user_posts(url, *, limit, cookie_browser, cookie_file, stopped):
    import requests

    sec_user_id = _douyin_sec_user_id(url)
    if not sec_user_id:
        raise ValueError("URL Douyin không có sec_user_id. Hãy dán URL dạng https://www.douyin.com/user/...")

    session = requests.Session()
    session.headers.update(_douyin_headers(url, cookie_browser, cookie_file))
    max_cursor = 0
    count_per_page = min(18, max(1, limit or 18))
    total = 0
    seen = set()

    while True:
        if stopped():
            raise DownloadCancelled("Đã dừng tải danh sách.")

        params = _douyin_post_api_params(sec_user_id, max_cursor, count_per_page)
        endpoint = _douyin_signed_endpoint("https://www.douyin.com/aweme/v1/web/aweme/post/", params)
        response = session.get(endpoint, timeout=25)
        response.raise_for_status()

        if not response.text.strip():
            raise ValueError(
                "Douyin trả dữ liệu rỗng. Nếu Edge/Chrome đang mở, app thường không đọc được cookie trực tiếp. "
                "Hãy export cookie Douyin ra file cookies.txt rồi chọn ở ô File cookie."
            )

        data = response.json()
        if data.get("status_code") not in (0, None):
            raise ValueError(data.get("status_msg") or data.get("message") or "Douyin API trả lỗi.")

        aweme_list = data.get("aweme_list") or []
        for aweme in aweme_list:
            aweme_url = _douyin_aweme_url(aweme)
            if not aweme_url or aweme_url in seen:
                continue
            seen.add(aweme_url)
            total += 1
            yield _douyin_aweme_item(aweme, index=total)
            if limit and total >= limit:
                return

        if not data.get("has_more"):
            return
        next_cursor = data.get("max_cursor")
        if next_cursor in (None, max_cursor):
            return
        max_cursor = next_cursor


def _format_bytes(value):
    if not value:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _entry_url(entry):
    url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url") or ""
    if _is_http_url(url):
        return url

    extractor = (entry.get("ie_key") or entry.get("extractor_key") or "").lower()
    if url and "youtube" in extractor:
        return f"https://www.youtube.com/watch?v={url}"
    return url


class _YtdlpLogger:
    def __init__(self, emit):
        self.emit = emit

    @staticmethod
    def _clean(msg):
        msg = _strip_ansi(msg)
        if "Deprecated Feature: Support for Python version 3.10" in msg:
            return ""
        return msg

    def debug(self, msg):
        msg = self._clean(msg)
        if msg and "Downloading webpage" in msg:
            self.emit({"type": "log", "text": msg})

    def warning(self, msg):
        msg = self._clean(msg)
        if msg:
            self.emit({"type": "log", "text": f"Cảnh báo: {msg}"})

    def error(self, msg):
        msg = self._clean(msg)
        if msg:
            self.emit({"type": "log", "text": f"Lỗi: {msg}"})


def _build_ydl_options(
    *,
    output_dir,
    format_name,
    threads,
    cookie_browser,
    cookie_file,
    use_archive,
    playlist_mode,
    playlist_limit,
    progress_hook=None,
    emit=None,
):
    fmt = FORMAT_OPTIONS[format_name]
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    if playlist_mode:
        outtmpl = output_path / "%(playlist_title).100B" / "%(playlist_index)05d - %(title).200B [%(id)s].%(ext)s"
    else:
        outtmpl = output_path / "%(title).200B [%(id)s].%(ext)s"

    opts = {
        "format": fmt["format"],
        "outtmpl": str(outtmpl),
        "continuedl": True,
        "ignoreerrors": True,
        "no_warnings": False,
        "noplaylist": not playlist_mode,
        "overwrites": False,
        "prefer_ffmpeg": True,
        "quiet": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": max(1, int(threads)),
        "windowsfilenames": os.name == "nt",
    }

    if emit:
        opts["logger"] = _YtdlpLogger(emit)
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if fmt.get("merge_output_format"):
        opts["merge_output_format"] = fmt["merge_output_format"]
    if fmt.get("postprocessors"):
        opts["postprocessors"] = fmt["postprocessors"]
    if cookie_file:
        opts["cookiefile"] = cookie_file
    elif cookie_browser:
        opts["cookiesfrombrowser"] = (cookie_browser,)
    if app_cfg.proxy:
        opts["proxy"] = app_cfg.proxy
    if use_archive:
        opts["download_archive"] = str(output_path / ".pyvideotrans-download-archive.txt")
    if playlist_mode and playlist_limit > 0:
        opts["playlistend"] = int(playlist_limit)
    return opts


class ChannelLoadWorker(QThread):
    event = Signal(dict)

    def __init__(self, *, url, limit=0, cookie_browser="", cookie_file="", parent=None):
        super().__init__(parent)
        self.url = url
        self.limit = limit
        self.cookie_browser = cookie_browser
        self.cookie_file = cookie_file
        self._cancel = False

    def stop(self):
        self._cancel = True

    def run(self):
        if _is_douyin_url(self.url) and _douyin_sec_user_id(self.url):
            try:
                self.event.emit({"type": "log", "text": "Đang đọc danh sách video Douyin..."})
                count = 0
                for entry in _fetch_douyin_user_posts(
                    self.url,
                    limit=self.limit,
                    cookie_browser=self.cookie_browser,
                    cookie_file=self.cookie_file,
                    stopped=lambda: self._cancel,
                ):
                    count += 1
                    self.event.emit({"type": "entry", **entry})
                if count == 0:
                    self.event.emit(
                        {
                            "type": "error",
                            "text": "Không lấy được video Douyin. Nếu Edge/Chrome đang mở, hãy export cookies.txt và chọn ở ô File cookie rồi Load lại.",
                        }
                    )
                    return
                self.event.emit({"type": "load_done", "count": count, "title": self.url})
            except DownloadCancelled as e:
                self.event.emit({"type": "canceled", "text": str(e)})
            except Exception as e:
                self.event.emit({"type": "error", "text": str(e)})
            return

        try:
            import yt_dlp
        except ModuleNotFoundError:
            self.event.emit(
                {
                    "type": "error",
                    "text": "Thiếu thư viện yt-dlp. Cài bằng: pip install yt-dlp",
                }
            )
            return

        try:
            opts = {
                "extract_flat": "in_playlist",
                "ignoreerrors": True,
                "quiet": True,
                "skip_download": True,
                "logger": _YtdlpLogger(self.event.emit),
            }
            if self.limit > 0:
                opts["playlistend"] = int(self.limit)
            if self.cookie_file:
                opts["cookiefile"] = self.cookie_file
            elif self.cookie_browser:
                opts["cookiesfrombrowser"] = (self.cookie_browser,)
            if app_cfg.proxy:
                opts["proxy"] = app_cfg.proxy

            self.event.emit({"type": "log", "text": "Đang đọc danh sách video của kênh..."})
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)

            entries = []
            if info:
                entries = info.get("entries") or [info]

            count = 0
            for entry in entries:
                if self._cancel:
                    self.event.emit({"type": "canceled", "text": "Đã dừng tải danh sách."})
                    return
                if not entry:
                    continue
                url = _entry_url(entry)
                if not url:
                    continue
                count += 1
                self.event.emit(
                    {
                        "type": "entry",
                        "index": count,
                        "title": entry.get("title") or url,
                        "uploader": entry.get("uploader") or entry.get("channel") or entry.get("extractor_key") or "",
                        "url": url,
                    }
                )

            title = info.get("title") if info else ""
            self.event.emit({"type": "load_done", "count": count, "title": title or self.url})
        except Exception as e:
            self.event.emit({"type": "error", "text": str(e)})


class DouyinSearchWorker(QThread):
    event = Signal(dict)

    def __init__(
        self,
        *,
        keyword,
        limit=30,
        cookie_browser="",
        cookie_file="",
        sort_type=0,
        publish_time=0,
        parent=None,
    ):
        super().__init__(parent)
        self.keyword = keyword
        self.limit = limit
        self.cookie_browser = cookie_browser
        self.cookie_file = cookie_file
        self.sort_type = sort_type
        self.publish_time = publish_time
        self._cancel = False

    def stop(self):
        self._cancel = True

    def run(self):
        try:
            self.event.emit({"type": "log", "text": f"Đang tìm Douyin theo từ khóa: {self.keyword}"})
            items = _fetch_douyin_keyword_search(
                self.keyword,
                limit=self.limit,
                cookie_browser=self.cookie_browser,
                cookie_file=self.cookie_file,
                stopped=lambda: self._cancel,
                sort_type=self.sort_type,
                publish_time=self.publish_time,
            )
            for item in items:
                self.event.emit({"type": "entry", **item})
            if items:
                self.event.emit({"type": "load_done", "count": len(items), "title": self.keyword})
            else:
                self.event.emit(
                    {
                        "type": "error",
                        "text": "Douyin search trả rỗng. Thường do cookie/search bị risk-control; thử từ khóa khác, giảm tần suất hoặc dùng proxy phù hợp.",
                    }
                )
        except DownloadCancelled as e:
            self.event.emit({"type": "canceled", "text": str(e)})
        except Exception as e:
            self.event.emit({"type": "error", "text": str(e)})


class VideoDownloadWorker(QThread):
    event = Signal(dict)

    def __init__(
        self,
        *,
        urls,
        items=None,
        output_dir,
        format_name,
        threads=3,
        cookie_browser="",
        cookie_file="",
        use_archive=True,
        playlist_mode=False,
        playlist_limit=0,
        parent=None,
    ):
        super().__init__(parent)
        self.items = items or [{"url": url} for url in urls]
        self.urls = [item.get("url", "") for item in self.items if item.get("url")]
        self.output_dir = output_dir
        self.format_name = format_name
        self.threads = threads
        self.cookie_browser = cookie_browser
        self.cookie_file = cookie_file
        self.use_archive = use_archive
        self.playlist_mode = playlist_mode
        self.playlist_limit = playlist_limit
        self._cancel = False

    def stop(self):
        self._cancel = True

    def _progress_hook(self, data):
        if self._cancel:
            raise DownloadCancelled("Đã dừng tải.")

        status = data.get("status")
        info = data.get("info_dict") or {}
        title = info.get("title") or Path(data.get("filename") or "").name
        url = _entry_url(info)
        filename = data.get("filename") or info.get("_filename") or ""

        if status == "downloading":
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            percent = int(downloaded * 100 / total) if total else 0
            speed = data.get("speed") or 0
            eta = data.get("eta")
            self.event.emit(
                {
                    "type": "progress",
                    "url": url,
                    "title": title,
                    "percent": max(0, min(percent, 100)),
                    "text": f"{title} - {_format_bytes(downloaded)}/{_format_bytes(total)}"
                    + (f" - {_format_bytes(speed)}/s" if speed else "")
                    + (f" - còn {eta}s" if eta else ""),
                }
            )
        elif status == "finished":
            self.event.emit(
                {
                    "type": "item_done",
                    "url": url,
                    "title": title,
                    "filepath": filename,
                    "text": f"Đã tải xong: {title}",
                }
            )

    def _direct_headers(self, referer):
        headers = {
            "User-Agent": DOUYIN_USER_AGENT,
            "Referer": referer or "https://www.douyin.com/",
        }
        try:
            cookie_header = _load_cookie_header(self.cookie_browser, self.cookie_file)
        except Exception as e:
            self.event.emit({"type": "log", "text": f"Không đọc được cookie cho tải trực tiếp, thử tải không cookie: {e}"})
            cookie_header = ""
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    def _download_direct_item(self, item):
        import requests

        if self._cancel:
            raise DownloadCancelled("Đã dừng tải.")

        page_url = item.get("url") or ""
        download_url = item.get("download_url") or ""
        if not download_url:
            return False

        video_id = _douyin_video_id(page_url) or str(abs(hash(download_url)))
        title = item.get("title") or page_url or video_id
        output_path = Path(self.output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        headers = self._direct_headers(page_url)
        self.event.emit({"type": "item_status", "url": page_url, "status": "Đang tải"})
        with requests.get(download_url, headers=headers, stream=True, timeout=60) as response:
            response.raise_for_status()
            extension = _extension_from_response(response, download_url)
            target = output_path / f"{_safe_filename(title, video_id)} [{video_id}]{extension}"

            if target.exists():
                self.event.emit(
                    {
                        "type": "item_done",
                        "url": page_url,
                        "title": title,
                        "filepath": str(target),
                        "text": f"Đã có sẵn: {target.name}",
                    }
                )
                return True

            temp_target = target.with_suffix(target.suffix + ".part")
            downloaded = 0
            total = int(response.headers.get("content-length") or 0)
            with open(temp_target, "wb") as file_obj:
                for chunk in response.iter_content(chunk_size=1024 * 512):
                    if self._cancel:
                        raise DownloadCancelled("Đã dừng tải.")
                    if not chunk:
                        continue
                    file_obj.write(chunk)
                    downloaded += len(chunk)
                    percent = int(downloaded * 100 / total) if total else 0
                    self.event.emit(
                        {
                            "type": "progress",
                            "url": page_url,
                            "title": title,
                            "percent": max(0, min(percent, 100)),
                            "text": f"{title} - {_format_bytes(downloaded)}/{_format_bytes(total)}",
                        }
                    )
            temp_target.replace(target)

        self.event.emit(
            {
                "type": "item_done",
                "url": page_url,
                "title": title,
                "filepath": str(target),
                "text": f"Đã tải xong: {target.name}",
            }
        )
        return True

    def run(self):
        try:
            normal_urls = []
            direct_items = []
            direct_count = 0
            for item in self.items:
                if item.get("download_url"):
                    direct_items.append(item)
                elif _is_douyin_url(item.get("url", "")) and _douyin_video_id(item.get("url", "")):
                    try:
                        self.event.emit({"type": "log", "text": f"Đang lấy CDN Douyin: {item.get('url')}"})
                        detail_item = _fetch_douyin_video_detail(
                            item["url"],
                            cookie_browser=self.cookie_browser,
                            cookie_file=self.cookie_file,
                        )
                        detail_item["url"] = _normalize_douyin_video_url(item["url"])
                        direct_items.append(detail_item)
                    except DownloadCancelled:
                        raise
                    except Exception as e:
                        self.event.emit({"type": "item_status", "url": item.get("url", ""), "status": "Lỗi"})
                        self.event.emit({"type": "log", "text": f"Lỗi tải Douyin {item.get('url')}: {e}"})
                elif item.get("source") == "douyin":
                    self.event.emit({"type": "item_status", "url": item.get("url", ""), "status": "Không có link tải"})
                    self.event.emit({"type": "log", "text": f"Douyin không trả link media cho: {item.get('title') or item.get('url')}"})
                elif item.get("url"):
                    normal_urls.append(item["url"])

            if direct_items:
                workers = max(1, min(int(self.threads or 1), len(direct_items)))
                self.event.emit({"type": "log", "text": f"Tải nhanh {len(direct_items)} video Douyin bằng {workers} luồng CDN..."})
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {executor.submit(self._download_direct_item, item): item for item in direct_items}
                    for future in as_completed(futures):
                        item = futures[future]
                        try:
                            if future.result():
                                direct_count += 1
                        except DownloadCancelled:
                            for pending in futures:
                                pending.cancel()
                            raise
                        except Exception as e:
                            self.event.emit({"type": "item_status", "url": item.get("url", ""), "status": "Lỗi"})
                            self.event.emit({"type": "log", "text": f"Lỗi tải {item.get('title') or item.get('url')}: {e}"})

            if not normal_urls:
                text = f"Hoàn tất tải {direct_count} video." if direct_count else "Hoàn tất tác vụ tải video."
                self.event.emit({"type": "done", "text": text})
                return

            try:
                import yt_dlp
            except ModuleNotFoundError:
                self.event.emit(
                    {
                        "type": "error",
                        "text": "Thiếu thư viện yt-dlp. Cài bằng: pip install yt-dlp",
                    }
                )
                return

            opts = _build_ydl_options(
                output_dir=self.output_dir,
                format_name=self.format_name,
                threads=self.threads,
                cookie_browser=self.cookie_browser,
                cookie_file=self.cookie_file,
                use_archive=self.use_archive,
                playlist_mode=self.playlist_mode,
                playlist_limit=self.playlist_limit,
                progress_hook=self._progress_hook,
                emit=self.event.emit,
            )
            self.event.emit({"type": "log", "text": f"Bắt đầu tải {len(normal_urls)} URL bằng yt-dlp..."})
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download(normal_urls)
            total = direct_count + len(normal_urls)
            self.event.emit({"type": "done", "text": f"Hoàn tất tải {total} video."})
        except DownloadCancelled as e:
            self.event.emit({"type": "canceled", "text": str(e)})
        except Exception as e:
            self.event.emit({"type": "error", "text": str(e)})


class VideoDownloadWindow(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.load_worker = None
        self.search_worker = None
        self._download_after_load = None
        self._channel_url_rows = {}
        self._search_url_rows = {}
        self.setWindowTitle("Trình tải video")
        self.setMinimumSize(920, 620)
        self._build_ui()
        self._restore_saved_options()

    def set_mode(self, mode):
        if mode == "search":
            self.tabs.setCurrentIndex(2)
        else:
            self.tabs.setCurrentIndex(1 if mode == "channel" else 0)

    def closeEvent(self, event):
        if self._has_running_task():
            QtWidgets.QMessageBox.warning(self, "Trình tải video", "Đang có tác vụ chạy. Hãy bấm Dừng trước khi đóng.")
            event.ignore()
            return
        self.hide()
        event.ignore()

    def _style_table_header(self, table):
        header_style = """
            QHeaderView::section {
                background-color: #f8fafc;
                color: #111827;
                font-size: 14px;
                font-weight: 800;
                padding: 7px 8px;
                border: 0;
                border-right: 1px solid #cbd5e1;
                border-bottom: 1px solid #94a3b8;
            }
        """
        table.horizontalHeader().setStyleSheet(header_style)
        table.verticalHeader().setStyleSheet(header_style)

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        title = QtWidgets.QLabel("Trình tải video")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        root.addWidget(title)

        self.tabs = QtWidgets.QTabWidget()
        self.url_tab = QtWidgets.QWidget()
        self.channel_tab = QtWidgets.QWidget()
        self.search_tab = QtWidgets.QWidget()
        self.tabs.addTab(self.url_tab, "Tải theo URL")
        self.tabs.addTab(self.channel_tab, "Tải video kênh")
        self.tabs.addTab(self.search_tab, "Tìm Douyin")
        root.addWidget(self.tabs, 1)

        self._build_url_tab()
        self._build_channel_tab()
        self._build_search_tab()

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        root.addWidget(self.progress)

        self.status_label = QtWidgets.QLabel("Sẵn sàng.")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.finished_table = QtWidgets.QTableWidget(0, 3)
        self._style_table_header(self.finished_table)
        self.finished_table.setHorizontalHeaderLabels(["Tiêu đề", "File", "Preview"])
        self.finished_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.finished_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.finished_table.setColumnWidth(2, 90)
        self.finished_table.setMaximumHeight(150)
        self.finished_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.finished_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        root.addWidget(self.finished_table)

        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(140)
        self.log_edit.setPlaceholderText("Log tải video")
        root.addWidget(self.log_edit)

    def _build_url_tab(self):
        layout = QtWidgets.QVBoxLayout(self.url_tab)
        layout.setSpacing(8)

        layout.addWidget(QtWidgets.QLabel("URLs (mỗi dòng một URL video):"))
        self.url_input = QtWidgets.QPlainTextEdit()
        self.url_input.setPlaceholderText("https://www.youtube.com/watch?v=...\nhttps://www.tiktok.com/@.../video/...")
        self.url_input.setMinimumHeight(160)
        layout.addWidget(self.url_input)

        options = self._create_common_options(prefix="url")
        layout.addLayout(options)

        buttons = QtWidgets.QHBoxLayout()
        self.url_paste_btn = QtWidgets.QPushButton("Dán URL")
        self.url_clear_btn = QtWidgets.QPushButton("Xóa danh sách")
        self.url_start_btn = QtWidgets.QPushButton("Tải video")
        self.url_stop_btn = QtWidgets.QPushButton("Dừng")
        self.url_stop_btn.setDisabled(True)
        buttons.addWidget(self.url_paste_btn)
        buttons.addWidget(self.url_clear_btn)
        buttons.addStretch()
        buttons.addWidget(self.url_stop_btn)
        buttons.addWidget(self.url_start_btn)
        layout.addLayout(buttons)

        self.url_paste_btn.clicked.connect(self._paste_urls)
        self.url_clear_btn.clicked.connect(self.url_input.clear)
        self.url_start_btn.clicked.connect(self._start_url_download)
        self.url_stop_btn.clicked.connect(self._stop_current_task)

    def _build_channel_tab(self):
        layout = QtWidgets.QVBoxLayout(self.channel_tab)
        layout.setSpacing(8)

        layout.addWidget(QtWidgets.QLabel("URL kênh / profile / playlist:"))
        self.channel_input = QtWidgets.QPlainTextEdit()
        self.channel_input.setPlaceholderText("Dán URL kênh tại đây. Để giới hạn = 0 nếu muốn tải toàn bộ video của kênh.")
        self.channel_input.setMaximumHeight(80)
        layout.addWidget(self.channel_input)

        options = self._create_common_options(prefix="channel")
        layout.addLayout(options)

        tools = QtWidgets.QHBoxLayout()
        self.channel_limit = QtWidgets.QSpinBox()
        self.channel_limit.setRange(0, 999999)
        self.channel_limit.setValue(0)
        self.channel_limit.setToolTip("0 = toàn bộ video có thể đọc được từ kênh")
        self.channel_load_btn = QtWidgets.QPushButton("Load danh sách")
        self.channel_select_all = QtWidgets.QCheckBox("Chọn tất cả")
        self.channel_filter = QtWidgets.QLineEdit()
        self.channel_filter.setPlaceholderText("Tìm theo tiêu đề hoặc URL...")
        tools.addWidget(QtWidgets.QLabel("Giới hạn video"))
        tools.addWidget(self.channel_limit)
        tools.addWidget(self.channel_load_btn)
        tools.addWidget(self.channel_select_all)
        tools.addWidget(self.channel_filter, 1)
        layout.addLayout(tools)

        self.channel_table = QtWidgets.QTableWidget(0, 8)
        self._style_table_header(self.channel_table)
        self.channel_table.setHorizontalHeaderLabels(["Chọn", "#", "Tiêu đề", "Nguồn", "URL", "Trạng thái", "File", "Preview"])
        self.channel_table.horizontalHeader().setStretchLastSection(False)
        self.channel_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self.channel_table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)
        self.channel_table.setColumnWidth(0, 60)
        self.channel_table.setColumnWidth(1, 55)
        self.channel_table.setColumnWidth(5, 110)
        self.channel_table.setColumnWidth(7, 90)
        self.channel_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.channel_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.channel_table, 1)

        buttons = QtWidgets.QHBoxLayout()
        self.channel_paste_btn = QtWidgets.QPushButton("Dán URL kênh")
        self.channel_download_all_btn = QtWidgets.QPushButton("Tải toàn bộ kênh")
        self.channel_download_selected_btn = QtWidgets.QPushButton("Tải video đã chọn")
        self.channel_stop_btn = QtWidgets.QPushButton("Dừng")
        self.channel_stop_btn.setDisabled(True)
        buttons.addWidget(self.channel_paste_btn)
        buttons.addStretch()
        buttons.addWidget(self.channel_stop_btn)
        buttons.addWidget(self.channel_download_selected_btn)
        buttons.addWidget(self.channel_download_all_btn)
        layout.addLayout(buttons)

        self.channel_paste_btn.clicked.connect(self._paste_channel_url)
        self.channel_load_btn.clicked.connect(self._load_channel)
        self.channel_select_all.toggled.connect(self._set_all_channel_rows)
        self.channel_filter.textChanged.connect(self._filter_channel_rows)
        self.channel_download_all_btn.clicked.connect(self._start_channel_download_all)
        self.channel_download_selected_btn.clicked.connect(self._start_channel_download_selected)
        self.channel_stop_btn.clicked.connect(self._stop_current_task)

    def _build_search_tab(self):
        layout = QtWidgets.QVBoxLayout(self.search_tab)
        layout.setSpacing(8)

        tools = QtWidgets.QHBoxLayout()
        self.search_keyword = QtWidgets.QLineEdit()
        self.search_keyword.setPlaceholderText("Nhập từ khóa Douyin...")
        self.search_limit = QtWidgets.QSpinBox()
        self.search_limit.setRange(1, 500)
        self.search_limit.setValue(30)
        self.search_sort = QtWidgets.QComboBox()
        self.search_sort.addItem("Viral nhất", 0)
        self.search_sort.addItem("Nhiều like", 1)
        self.search_sort.addItem("Mới nhất", 2)
        self.search_publish_time = QtWidgets.QComboBox()
        self.search_publish_time.addItem("Mọi thời gian", 0)
        self.search_publish_time.addItem("24 giờ", 1)
        self.search_publish_time.addItem("7 ngày", 7)
        self.search_publish_time.addItem("6 tháng", 180)
        self.search_btn = QtWidgets.QPushButton("Tìm")
        self.search_select_all = QtWidgets.QCheckBox("Chọn tất cả")
        self.search_filter = QtWidgets.QLineEdit()
        self.search_filter.setPlaceholderText("Lọc theo tiêu đề hoặc URL...")

        tools.addWidget(QtWidgets.QLabel("Từ khóa"))
        tools.addWidget(self.search_keyword, 1)
        tools.addWidget(QtWidgets.QLabel("Số video"))
        tools.addWidget(self.search_limit)
        tools.addWidget(self.search_sort)
        tools.addWidget(self.search_publish_time)
        tools.addWidget(self.search_btn)
        layout.addLayout(tools)

        options = self._create_common_options(prefix="search")
        layout.addLayout(options)

        filter_row = QtWidgets.QHBoxLayout()
        filter_row.addWidget(self.search_select_all)
        filter_row.addWidget(self.search_filter, 1)
        layout.addLayout(filter_row)

        self.search_table = QtWidgets.QTableWidget(0, 10)
        self._style_table_header(self.search_table)
        self.search_table.setHorizontalHeaderLabels(
            ["Chọn", "#", "Viral", "Tiêu đề", "Nguồn", "Tương tác", "URL", "Trạng thái", "File", "Preview"]
        )
        self.search_table.horizontalHeader().setStretchLastSection(False)
        self.search_table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        self.search_table.horizontalHeader().setSectionResizeMode(6, QtWidgets.QHeaderView.Stretch)
        self.search_table.setColumnWidth(0, 60)
        self.search_table.setColumnWidth(1, 55)
        self.search_table.setColumnWidth(2, 90)
        self.search_table.setColumnWidth(5, 230)
        self.search_table.setColumnWidth(7, 110)
        self.search_table.setColumnWidth(9, 90)
        self.search_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.search_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.search_table, 1)

        buttons = QtWidgets.QHBoxLayout()
        self.search_download_selected_btn = QtWidgets.QPushButton("Tải video đã chọn")
        self.search_download_all_btn = QtWidgets.QPushButton("Tải tất cả")
        self.search_stop_btn = QtWidgets.QPushButton("Dừng")
        self.search_stop_btn.setDisabled(True)
        buttons.addStretch()
        buttons.addWidget(self.search_stop_btn)
        buttons.addWidget(self.search_download_selected_btn)
        buttons.addWidget(self.search_download_all_btn)
        layout.addLayout(buttons)

        self.search_btn.clicked.connect(self._start_douyin_search)
        self.search_keyword.returnPressed.connect(self._start_douyin_search)
        self.search_select_all.toggled.connect(self._set_all_search_rows)
        self.search_filter.textChanged.connect(self._filter_search_rows)
        self.search_download_selected_btn.clicked.connect(self._start_search_download_selected)
        self.search_download_all_btn.clicked.connect(self._start_search_download_all)
        self.search_stop_btn.clicked.connect(self._stop_current_task)

    def _create_common_options(self, *, prefix):
        layout = QtWidgets.QGridLayout()
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(8)

        format_combo = QtWidgets.QComboBox()
        format_combo.addItems(FORMAT_OPTIONS.keys())

        threads_spin = QtWidgets.QSpinBox()
        threads_spin.setRange(1, 16)
        threads_spin.setValue(3)
        threads_spin.setToolTip("Số luồng tải fragment của yt-dlp")

        output_dir = QtWidgets.QLineEdit(str(DEFAULT_DOWNLOAD_DIR))
        browse_btn = QtWidgets.QPushButton("Chọn thư mục")
        open_btn = QtWidgets.QPushButton("Mở")

        cookie_combo = QtWidgets.QComboBox()
        cookie_combo.addItems(COOKIE_BROWSERS.keys())
        cookie_combo.setToolTip("Chọn trình duyệt nếu nền tảng cần cookie đăng nhập")

        cookie_file = QtWidgets.QLineEdit()
        cookie_file.setPlaceholderText("cookies.txt (khuyên dùng cho Douyin khi Edge/Chrome đang mở)")
        cookie_file_btn = QtWidgets.QPushButton("Chọn cookie")

        archive_check = QtWidgets.QCheckBox("Bỏ qua video đã tải")
        archive_check.setChecked(True)

        setattr(self, f"{prefix}_format_combo", format_combo)
        setattr(self, f"{prefix}_threads_spin", threads_spin)
        setattr(self, f"{prefix}_output_dir", output_dir)
        setattr(self, f"{prefix}_cookie_combo", cookie_combo)
        setattr(self, f"{prefix}_cookie_file", cookie_file)
        setattr(self, f"{prefix}_archive_check", archive_check)

        browse_btn.clicked.connect(lambda: self._choose_output_dir(output_dir))
        open_btn.clicked.connect(lambda: self._open_output_dir(output_dir.text()))
        cookie_file_btn.clicked.connect(lambda: self._choose_cookie_file(cookie_file))

        layout.addWidget(QtWidgets.QLabel("Định dạng"), 0, 0)
        layout.addWidget(format_combo, 0, 1)
        layout.addWidget(QtWidgets.QLabel("Luồng"), 0, 2)
        layout.addWidget(threads_spin, 0, 3)
        layout.addWidget(QtWidgets.QLabel("Thư mục lưu"), 1, 0)
        layout.addWidget(output_dir, 1, 1, 1, 3)
        layout.addWidget(browse_btn, 1, 4)
        layout.addWidget(open_btn, 1, 5)
        layout.addWidget(QtWidgets.QLabel("Cookies"), 2, 0)
        layout.addWidget(cookie_combo, 2, 1)
        layout.addWidget(archive_check, 2, 2, 1, 2)
        layout.addWidget(QtWidgets.QLabel("File cookie"), 3, 0)
        layout.addWidget(cookie_file, 3, 1, 1, 3)
        layout.addWidget(cookie_file_btn, 3, 4)
        return layout

    def _common_values(self, prefix):
        cookie_label = getattr(self, f"{prefix}_cookie_combo").currentText()
        return {
            "output_dir": getattr(self, f"{prefix}_output_dir").text().strip() or str(DEFAULT_DOWNLOAD_DIR),
            "format_name": getattr(self, f"{prefix}_format_combo").currentText(),
            "threads": getattr(self, f"{prefix}_threads_spin").value(),
            "cookie_browser": COOKIE_BROWSERS.get(cookie_label, ""),
            "cookie_file": getattr(self, f"{prefix}_cookie_file").text().strip(),
            "use_archive": getattr(self, f"{prefix}_archive_check").isChecked(),
        }

    def _restore_saved_options(self):
        output_dir = settings.get("video_downloader_output_dir", "")
        legacy_output = (Path(HOME_DIR) / "downloads").as_posix().rstrip("/").lower()
        if not output_dir or Path(output_dir).as_posix().rstrip("/").lower() == legacy_output:
            output_dir = DEFAULT_DOWNLOAD_DIR.as_posix()
        cookie_file = _default_cookie_file()
        cookie_browser = settings.get("video_downloader_cookie_browser", "")
        channel_url = settings.get("video_downloader_channel_url", "")
        search_keyword = settings.get("video_downloader_search_keyword", "")

        for prefix in ("url", "channel", "search"):
            if output_dir:
                getattr(self, f"{prefix}_output_dir").setText(output_dir)
            if cookie_file and Path(cookie_file).exists():
                getattr(self, f"{prefix}_cookie_file").setText(cookie_file)
            if cookie_browser:
                self._set_combo_value(getattr(self, f"{prefix}_cookie_combo"), cookie_browser)

        if channel_url:
            self.channel_input.setPlainText(channel_url)
        if search_keyword:
            self.search_keyword.setText(search_keyword)

    def _set_combo_value(self, combo, value):
        for index in range(combo.count()):
            if combo.itemText(index) == value or COOKIE_BROWSERS.get(combo.itemText(index), "") == value:
                combo.setCurrentIndex(index)
                return

    def _save_current_options(self, prefix):
        values = self._common_values(prefix)
        settings["video_downloader_output_dir"] = values["output_dir"]
        settings["video_downloader_cookie_file"] = values["cookie_file"]
        settings["video_downloader_cookie_browser"] = values["cookie_browser"]
        if hasattr(self, "channel_input"):
            settings["video_downloader_channel_url"] = self._first_channel_url()
        if hasattr(self, "search_keyword"):
            settings["video_downloader_search_keyword"] = self.search_keyword.text().strip()
        settings.save()

    def _choose_output_dir(self, target_line):
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu", target_line.text())
        if directory:
            target_line.setText(directory.replace("\\", "/"))
            self.url_output_dir.setText(target_line.text())
            self.channel_output_dir.setText(target_line.text())
            self.search_output_dir.setText(target_line.text())
            settings["video_downloader_output_dir"] = target_line.text()
            settings.save()

    def _choose_cookie_file(self, target_line):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Chọn file cookies.txt",
            target_line.text() or str(Path.home()),
            "Cookie files (*.txt);;All files (*)",
        )
        if filename:
            target_line.setText(filename.replace("\\", "/"))
            self.url_cookie_file.setText(target_line.text())
            self.channel_cookie_file.setText(target_line.text())
            self.search_cookie_file.setText(target_line.text())
            settings["video_downloader_cookie_file"] = target_line.text()
            settings.save()

    def _open_output_dir(self, output_dir):
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(output_dir))

    def _append_log(self, text):
        if not text:
            return
        self.log_edit.moveCursor(QTextCursor.End)
        self.log_edit.insertPlainText(f"{text}\n")
        self.log_edit.moveCursor(QTextCursor.End)

    def _set_status(self, text):
        self.status_label.setText(text)
        self._append_log(text)

    def _paste_urls(self):
        text = QtWidgets.QApplication.clipboard().text().strip()
        if text:
            current = self.url_input.toPlainText().strip()
            self.url_input.setPlainText((current + "\n" + text).strip() if current else text)

    def _paste_channel_url(self):
        text = QtWidgets.QApplication.clipboard().text().strip()
        if text:
            self.channel_input.setPlainText(text)
            settings["video_downloader_channel_url"] = text
            settings.save()

    def _parse_urls(self, text):
        urls = []
        for line in text.splitlines():
            line = line.strip()
            if line and _is_http_url(line):
                urls.append(line)
        return urls

    def _normalize_download_urls(self, urls):
        normalized = []
        invalid = []
        for url in urls:
            if _is_douyin_url(url):
                if _douyin_video_id(url):
                    normalized.append(_normalize_douyin_video_url(url))
                    continue
                if _douyin_sec_user_id(url):
                    invalid.append(url)
                    continue
            normalized.append(url)
        return normalized, invalid

    def _first_channel_url(self):
        urls = self._parse_urls(self.channel_input.toPlainText())
        return urls[0] if urls else ""

    def _has_running_task(self):
        return (
            (self.worker and self.worker.isRunning())
            or (self.load_worker and self.load_worker.isRunning())
            or (self.search_worker and self.search_worker.isRunning())
        )

    def _set_download_busy(self, busy):
        self.url_start_btn.setDisabled(busy)
        self.channel_download_all_btn.setDisabled(busy)
        self.channel_download_selected_btn.setDisabled(busy)
        self.channel_load_btn.setDisabled(busy)
        self.search_btn.setDisabled(busy)
        self.search_download_all_btn.setDisabled(busy)
        self.search_download_selected_btn.setDisabled(busy)
        self.url_stop_btn.setDisabled(not busy)
        self.channel_stop_btn.setDisabled(not busy)
        self.search_stop_btn.setDisabled(not busy)

    def _start_url_download(self):
        if self._has_running_task():
            return
        urls = self._parse_urls(self.url_input.toPlainText())
        if not urls:
            QtWidgets.QMessageBox.warning(self, "Trình tải video", "Hãy nhập ít nhất một URL hợp lệ.")
            return
        self._save_current_options("url")
        self._start_download(urls=urls, playlist_mode=False, prefix="url", playlist_limit=0)

    def _start_channel_download_all(self):
        if self._has_running_task():
            return
        url = self._first_channel_url()
        if not url:
            QtWidgets.QMessageBox.warning(self, "Trình tải video", "Hãy nhập URL kênh/profile/playlist hợp lệ.")
            return
        if _is_douyin_url(url) and _douyin_sec_user_id(url):
            if self.channel_table.rowCount() == 0:
                self._load_channel(download_after="all")
                return
            items = self._all_channel_items()
            self._start_download(
                urls=[item["url"] for item in items],
                items=items,
                playlist_mode=False,
                prefix="channel",
                playlist_limit=0,
            )
            return
        self._start_download(
            urls=[url],
            playlist_mode=True,
            prefix="channel",
            playlist_limit=self.channel_limit.value(),
        )

    def _start_channel_download_selected(self):
        if self._has_running_task():
            return
        items = self._selected_channel_items()
        if not items:
            QtWidgets.QMessageBox.warning(self, "Trình tải video", "Hãy load danh sách và chọn ít nhất một video.")
            return
        self._start_download(
            urls=[item["url"] for item in items],
            items=items,
            playlist_mode=False,
            prefix="channel",
            playlist_limit=0,
        )

    def _start_download(self, *, urls, playlist_mode, prefix, playlist_limit, items=None):
        urls, invalid = self._normalize_download_urls(urls)
        if invalid:
            QtWidgets.QMessageBox.warning(
                self,
                "Trình tải video",
                "URL Douyin dạng kênh không thể tải trực tiếp bằng yt-dlp. Hãy dùng tab Tải video kênh và bấm Load danh sách trước.",
            )
            return

        values = self._common_values(prefix)
        self._save_current_options(prefix)
        self.progress.setValue(0)
        self._set_download_busy(True)
        self.worker = VideoDownloadWorker(
            urls=urls,
            items=items,
            output_dir=values["output_dir"],
            format_name=values["format_name"],
            threads=values["threads"],
            cookie_browser=values["cookie_browser"],
            cookie_file=values["cookie_file"],
            use_archive=values["use_archive"],
            playlist_mode=playlist_mode,
            playlist_limit=playlist_limit,
            parent=self,
        )
        self.worker.event.connect(self._on_download_event)
        self.worker.finished.connect(lambda: self._set_download_busy(False))
        self.worker.start()

    def _load_channel(self, download_after=None):
        if download_after is False:
            download_after = None
        if self._has_running_task():
            return
        url = self._first_channel_url()
        if not url:
            QtWidgets.QMessageBox.warning(self, "Trình tải video", "Hãy nhập URL kênh/profile/playlist hợp lệ.")
            return
        self._save_current_options("channel")
        self._download_after_load = download_after
        self._channel_url_rows = {}
        self.channel_table.setRowCount(0)
        self.progress.setValue(0)
        values = self._common_values("channel")
        self.channel_load_btn.setDisabled(True)
        self.channel_download_all_btn.setDisabled(True)
        self.channel_download_selected_btn.setDisabled(True)
        self.channel_stop_btn.setDisabled(False)
        self.load_worker = ChannelLoadWorker(
            url=url,
            limit=self.channel_limit.value(),
            cookie_browser=values["cookie_browser"],
            cookie_file=values["cookie_file"],
            parent=self,
        )
        self.load_worker.event.connect(self._on_channel_load_event)
        self.load_worker.finished.connect(self._load_finished)
        self.load_worker.start()

    def _load_finished(self):
        self.channel_load_btn.setDisabled(False)
        self.channel_download_all_btn.setDisabled(False)
        self.channel_download_selected_btn.setDisabled(False)
        self.channel_stop_btn.setDisabled(True)
        pending = self._download_after_load
        self._download_after_load = None
        if pending == "all" and self.channel_table.rowCount() > 0:
            QtCore.QTimer.singleShot(0, self._start_channel_download_all)

    def _start_douyin_search(self):
        if self._has_running_task():
            return
        keyword = self.search_keyword.text().strip()
        if not keyword:
            QtWidgets.QMessageBox.warning(self, "Trình tải video", "Hãy nhập từ khóa Douyin.")
            return
        self._save_current_options("search")
        self.search_table.setRowCount(0)
        self._search_url_rows = {}
        self.progress.setValue(0)
        values = self._common_values("search")
        self.search_btn.setDisabled(True)
        self.search_download_all_btn.setDisabled(True)
        self.search_download_selected_btn.setDisabled(True)
        self.search_stop_btn.setDisabled(False)
        self.search_worker = DouyinSearchWorker(
            keyword=keyword,
            limit=self.search_limit.value(),
            cookie_browser=values["cookie_browser"],
            cookie_file=values["cookie_file"],
            sort_type=self.search_sort.currentData() or 0,
            publish_time=self.search_publish_time.currentData() or 0,
            parent=self,
        )
        self.search_worker.event.connect(self._on_search_event)
        self.search_worker.finished.connect(self._search_finished)
        self.search_worker.start()

    def _search_finished(self):
        self.search_btn.setDisabled(False)
        self.search_download_all_btn.setDisabled(False)
        self.search_download_selected_btn.setDisabled(False)
        self.search_stop_btn.setDisabled(True)

    def _on_search_event(self, event):
        event_type = event.get("type")
        if event_type == "entry":
            self._add_search_row(event)
            count = self.search_table.rowCount()
            self.progress.setValue(min(100, int(count * 100 / max(1, self.search_limit.value()))))
        elif event_type == "load_done":
            self.progress.setValue(100)
            self._set_status(f"Đã tìm được {event.get('count', 0)} video Douyin cho từ khóa {event.get('title', '')}.")
        elif event_type in {"error", "canceled"}:
            self.progress.setValue(0)
            self._set_status(event.get("text", ""))
        elif event_type == "log":
            self._append_log(event.get("text", ""))

    def _start_search_download_selected(self):
        if self._has_running_task():
            return
        items = self._selected_search_items()
        if not items:
            QtWidgets.QMessageBox.warning(self, "Trình tải video", "Hãy tìm và chọn ít nhất một video.")
            return
        self._start_download(
            urls=[item["url"] for item in items],
            items=items,
            playlist_mode=False,
            prefix="search",
            playlist_limit=0,
        )

    def _start_search_download_all(self):
        if self._has_running_task():
            return
        items = self._all_search_items()
        if not items:
            QtWidgets.QMessageBox.warning(self, "Trình tải video", "Hãy tìm video trước.")
            return
        self._start_download(
            urls=[item["url"] for item in items],
            items=items,
            playlist_mode=False,
            prefix="search",
            playlist_limit=0,
        )

    def _on_channel_load_event(self, event):
        event_type = event.get("type")
        if event_type == "entry":
            self._add_channel_row(event)
            count = self.channel_table.rowCount()
            self.progress.setValue(0 if self.channel_limit.value() == 0 else min(100, int(count * 100 / self.channel_limit.value())))
        elif event_type == "load_done":
            self.progress.setValue(100)
            self._set_status(f"Đã load {event.get('count', 0)} video từ {event.get('title', '')}.")
        elif event_type in {"error", "canceled"}:
            self._download_after_load = None
            self.progress.setValue(0)
            self._set_status(event.get("text", ""))
        elif event_type == "log":
            self._append_log(event.get("text", ""))

    def _on_download_event(self, event):
        event_type = event.get("type")
        if event_type == "progress":
            self.progress.setValue(event.get("percent", 0))
            self.status_label.setText(event.get("text", ""))
            self._set_channel_row_status(event.get("url", ""), f"Đang tải {event.get('percent', 0)}%")
            self._set_search_row_status(event.get("url", ""), f"Đang tải {event.get('percent', 0)}%")
        elif event_type == "item_status":
            self._set_channel_row_status(event.get("url", ""), event.get("status", ""))
            self._set_search_row_status(event.get("url", ""), event.get("status", ""))
        elif event_type == "item_done":
            self._mark_channel_row_done(event.get("url", ""), event.get("filepath", ""))
            self._mark_search_row_done(event.get("url", ""), event.get("filepath", ""))
            self._add_finished_file(event.get("title", ""), event.get("filepath", ""))
            self._append_log(event.get("text", ""))
        elif event_type == "log":
            self._append_log(event.get("text", ""))
        elif event_type == "done":
            self.progress.setValue(100)
            self._set_status(event.get("text", "Hoàn tất."))
        elif event_type in {"error", "canceled"}:
            self.progress.setValue(0)
            self._set_status(event.get("text", ""))

    def _add_channel_row(self, data):
        row = self.channel_table.rowCount()
        self.channel_table.insertRow(row)

        check_item = QtWidgets.QTableWidgetItem()
        check_item.setCheckState(Qt.Checked)
        check_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.channel_table.setItem(row, 0, check_item)

        values = [
            str(data.get("index", row + 1)),
            data.get("title", ""),
            data.get("uploader", ""),
            data.get("url", ""),
            "Chờ tải",
            "",
        ]
        for col, value in enumerate(values, start=1):
            item = QtWidgets.QTableWidgetItem(value)
            item.setToolTip(value)
            self.channel_table.setItem(row, col, item)

        url_item = self.channel_table.item(row, 4)
        if url_item:
            url_item.setData(ROLE_DOWNLOAD_URL, data.get("download_url", ""))
            url_item.setData(ROLE_COVER_URL, data.get("cover_url", ""))
            url_item.setData(ROLE_SOURCE, data.get("source", ""))
            self._channel_url_rows[url_item.text()] = row

        preview_btn = QtWidgets.QPushButton("Xem")
        preview_btn.setEnabled(False)
        preview_btn.setToolTip("Tải xong sẽ mở được video")
        preview_btn.clicked.connect(lambda _checked=False, table_row=row: self._open_channel_preview(table_row))
        self.channel_table.setCellWidget(row, 7, preview_btn)

    def _set_all_channel_rows(self, checked):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.channel_table.rowCount()):
            item = self.channel_table.item(row, 0)
            if item:
                item.setCheckState(state)

    def _filter_channel_rows(self, text):
        keyword = text.strip().lower()
        for row in range(self.channel_table.rowCount()):
            title = self.channel_table.item(row, 2).text().lower() if self.channel_table.item(row, 2) else ""
            url = self.channel_table.item(row, 4).text().lower() if self.channel_table.item(row, 4) else ""
            self.channel_table.setRowHidden(row, bool(keyword and keyword not in title and keyword not in url))

    def _channel_item_from_row(self, row):
        url_item = self.channel_table.item(row, 4)
        if not url_item:
            return None
        url = url_item.text().strip()
        if not url:
            return None
        title_item = self.channel_table.item(row, 2)
        uploader_item = self.channel_table.item(row, 3)
        return {
            "url": url,
            "title": title_item.text().strip() if title_item else url,
            "uploader": uploader_item.text().strip() if uploader_item else "",
            "download_url": url_item.data(ROLE_DOWNLOAD_URL) or "",
            "cover_url": url_item.data(ROLE_COVER_URL) or "",
            "source": url_item.data(ROLE_SOURCE) or "",
        }

    def _selected_channel_items(self):
        items = []
        for row in range(self.channel_table.rowCount()):
            check = self.channel_table.item(row, 0)
            if check and check.checkState() == Qt.Checked:
                item = self._channel_item_from_row(row)
                if item:
                    items.append(item)
        return items

    def _all_channel_items(self):
        items = []
        for row in range(self.channel_table.rowCount()):
            item = self._channel_item_from_row(row)
            if item:
                items.append(item)
        return items

    def _channel_row_for_url(self, url):
        if not url:
            return -1
        row = self._channel_url_rows.get(url)
        if row is not None:
            return row
        normalized = _normalize_douyin_video_url(url) if _is_douyin_url(url) else url
        row = self._channel_url_rows.get(normalized)
        if row is not None:
            return row
        for index in range(self.channel_table.rowCount()):
            url_item = self.channel_table.item(index, 4)
            if url_item and url_item.text().strip() == normalized:
                self._channel_url_rows[normalized] = index
                return index
        return -1

    def _set_channel_row_status(self, url, status):
        row = self._channel_row_for_url(url)
        if row < 0 or not status:
            return
        item = self.channel_table.item(row, 5)
        if item:
            item.setText(status)
            item.setToolTip(status)

    def _mark_channel_row_done(self, url, filepath):
        row = self._channel_row_for_url(url)
        if row < 0:
            return
        self._set_channel_row_status(url, "Hoàn tất")
        if filepath:
            file_item = self.channel_table.item(row, 6)
            if file_item:
                path = str(Path(filepath))
                file_item.setText(Path(path).name)
                file_item.setToolTip(path)
                file_item.setData(ROLE_FILE_PATH, path)
            preview_btn = self.channel_table.cellWidget(row, 7)
            if preview_btn:
                preview_btn.setEnabled(Path(filepath).exists())
                preview_btn.setToolTip(str(filepath))

    def _add_finished_file(self, title, filepath):
        if not filepath or not Path(filepath).exists():
            return
        path = str(Path(filepath))
        for row in range(self.finished_table.rowCount()):
            item = self.finished_table.item(row, 1)
            if item and item.data(ROLE_FILE_PATH) == path:
                return

        row = self.finished_table.rowCount()
        self.finished_table.insertRow(row)
        title_item = QtWidgets.QTableWidgetItem(title or Path(path).stem)
        title_item.setToolTip(title_item.text())
        self.finished_table.setItem(row, 0, title_item)

        file_item = QtWidgets.QTableWidgetItem(Path(path).name)
        file_item.setToolTip(path)
        file_item.setData(ROLE_FILE_PATH, path)
        self.finished_table.setItem(row, 1, file_item)

        preview_btn = QtWidgets.QPushButton("Xem")
        preview_btn.setToolTip(path)
        preview_btn.clicked.connect(lambda _checked=False, file_path=path: self._open_file_path(file_path))
        self.finished_table.setCellWidget(row, 2, preview_btn)

    def _open_file_path(self, filepath):
        if not filepath or not Path(filepath).exists():
            QtWidgets.QMessageBox.information(self, "Trình tải video", "File không còn tồn tại.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(filepath))))

    def _open_channel_preview(self, row):
        file_item = self.channel_table.item(row, 6)
        filepath = file_item.data(ROLE_FILE_PATH) if file_item else ""
        if not filepath or not Path(filepath).exists():
            QtWidgets.QMessageBox.information(self, "Trình tải video", "Video chưa tải xong hoặc file không còn tồn tại.")
            return
        self._open_file_path(filepath)

    def _add_search_row(self, data):
        row = self.search_table.rowCount()
        self.search_table.insertRow(row)

        check_item = QtWidgets.QTableWidgetItem()
        check_item.setCheckState(Qt.Checked)
        check_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.search_table.setItem(row, 0, check_item)

        values = [
            str(data.get("index", row + 1)),
            str(data.get("viral_score", 0)),
            data.get("title", ""),
            data.get("uploader", ""),
            _douyin_stats_text(data),
            data.get("url", ""),
            "Chờ tải",
            "",
        ]
        for col, value in enumerate(values, start=1):
            item = QtWidgets.QTableWidgetItem(value)
            item.setToolTip(value)
            if col == 2:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.search_table.setItem(row, col, item)

        url_item = self.search_table.item(row, 6)
        if url_item:
            url_item.setData(ROLE_DOWNLOAD_URL, data.get("download_url", ""))
            url_item.setData(ROLE_COVER_URL, data.get("cover_url", ""))
            url_item.setData(ROLE_SOURCE, data.get("source", "douyin"))
            self._search_url_rows[url_item.text()] = row

        preview_btn = QtWidgets.QPushButton("Xem")
        preview_btn.setEnabled(False)
        preview_btn.setToolTip("Tải xong sẽ mở được video")
        preview_btn.clicked.connect(lambda _checked=False, table_row=row: self._open_search_preview(table_row))
        self.search_table.setCellWidget(row, 9, preview_btn)

    def _set_all_search_rows(self, checked):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.search_table.rowCount()):
            item = self.search_table.item(row, 0)
            if item:
                item.setCheckState(state)

    def _filter_search_rows(self, text):
        keyword = text.strip().lower()
        for row in range(self.search_table.rowCount()):
            title = self.search_table.item(row, 3).text().lower() if self.search_table.item(row, 3) else ""
            url = self.search_table.item(row, 6).text().lower() if self.search_table.item(row, 6) else ""
            self.search_table.setRowHidden(row, bool(keyword and keyword not in title and keyword not in url))

    def _search_item_from_row(self, row):
        url_item = self.search_table.item(row, 6)
        if not url_item:
            return None
        url = url_item.text().strip()
        if not url:
            return None
        title_item = self.search_table.item(row, 3)
        uploader_item = self.search_table.item(row, 4)
        return {
            "url": url,
            "title": title_item.text().strip() if title_item else url,
            "uploader": uploader_item.text().strip() if uploader_item else "",
            "download_url": url_item.data(ROLE_DOWNLOAD_URL) or "",
            "cover_url": url_item.data(ROLE_COVER_URL) or "",
            "source": url_item.data(ROLE_SOURCE) or "douyin",
        }

    def _selected_search_items(self):
        items = []
        for row in range(self.search_table.rowCount()):
            check = self.search_table.item(row, 0)
            if check and check.checkState() == Qt.Checked:
                item = self._search_item_from_row(row)
                if item:
                    items.append(item)
        return items

    def _all_search_items(self):
        items = []
        for row in range(self.search_table.rowCount()):
            item = self._search_item_from_row(row)
            if item:
                items.append(item)
        return items

    def _search_row_for_url(self, url):
        if not url:
            return -1
        row = self._search_url_rows.get(url)
        if row is not None:
            return row
        normalized = _normalize_douyin_video_url(url) if _is_douyin_url(url) else url
        row = self._search_url_rows.get(normalized)
        if row is not None:
            return row
        for index in range(self.search_table.rowCount()):
            url_item = self.search_table.item(index, 6)
            if url_item and url_item.text().strip() == normalized:
                self._search_url_rows[normalized] = index
                return index
        return -1

    def _set_search_row_status(self, url, status):
        row = self._search_row_for_url(url)
        if row < 0 or not status:
            return
        item = self.search_table.item(row, 7)
        if item:
            item.setText(status)
            item.setToolTip(status)

    def _mark_search_row_done(self, url, filepath):
        row = self._search_row_for_url(url)
        if row < 0:
            return
        self._set_search_row_status(url, "Hoàn tất")
        if filepath:
            file_item = self.search_table.item(row, 8)
            if file_item:
                path = str(Path(filepath))
                file_item.setText(Path(path).name)
                file_item.setToolTip(path)
                file_item.setData(ROLE_FILE_PATH, path)
            preview_btn = self.search_table.cellWidget(row, 9)
            if preview_btn:
                preview_btn.setEnabled(Path(filepath).exists())
                preview_btn.setToolTip(str(filepath))

    def _open_search_preview(self, row):
        file_item = self.search_table.item(row, 8)
        filepath = file_item.data(ROLE_FILE_PATH) if file_item else ""
        if not filepath or not Path(filepath).exists():
            QtWidgets.QMessageBox.information(self, "Trình tải video", "Video chưa tải xong hoặc file không còn tồn tại.")
            return
        self._open_file_path(filepath)

    def _stop_current_task(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self._append_log("Đang dừng tác vụ tải...")
        if self.load_worker and self.load_worker.isRunning():
            self.load_worker.stop()
            self._append_log("Đang dừng tác vụ load danh sách...")
        if self.search_worker and self.search_worker.isRunning():
            self.search_worker.stop()
            self._append_log("Đang dừng tác vụ tìm Douyin...")
