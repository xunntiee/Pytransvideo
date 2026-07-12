# -*- coding: utf-8 -*-
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websocket

from videotrans.configure.config import ROOT_DIR


DEFAULT_GEM_URL = "https://gemini.google.com/gem/90edf2642605"
LOGIN_URL = "https://accounts.google.com/"
DEFAULT_CDP_PORT = int(os.environ.get("PYVIDEOTRANS_GEMINI_CDP_PORT", "9333"))
AUTOMATION_PROFILE_DIR = Path(ROOT_DIR) / "tmp" / "gemini_chrome_profile"


class GeminiAutomationError(RuntimeError):
    pass


class CdpPage:
    def __init__(self, websocket_url):
        self.ws = websocket.create_connection(websocket_url, timeout=1)
        self._next_id = 0
        self._events = []

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def send(self, method, params=None, timeout=30):
        self._next_id += 1
        call_id = self._next_id
        self.ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            msg = json.loads(raw)
            if msg.get("id") == call_id:
                if "error" in msg:
                    raise GeminiAutomationError(f"CDP {method} lỗi: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self._events.append(msg)
        raise GeminiAutomationError(f"CDP timeout: {method}")

    def wait_event(self, method, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for index, event in enumerate(self._events):
                if event.get("method") == method:
                    return self._events.pop(index)
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            msg = json.loads(raw)
            if msg.get("method") == method:
                return msg
            if "method" in msg:
                self._events.append(msg)
        return None

    def evaluate(self, expression, timeout=30, await_promise=False):
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        value = result.get("result", {})
        if "exceptionDetails" in result:
            raise GeminiAutomationError(str(result["exceptionDetails"]))
        return value.get("value")

    def click_point(self, x, y):
        x = int(x)
        y = int(y)
        for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
            params = {
                "type": event_type,
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            }
            if event_type == "mouseMoved":
                params.pop("button")
                params.pop("clickCount")
            self.send("Input.dispatchMouseEvent", params, timeout=5)


def _json_request(url, method="GET", timeout=5):
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _is_port_free(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _find_browser_executable():
    env_path = os.environ.get("PYVIDEOTRANS_CHROME_PATH")
    candidates = []
    if env_path:
        candidates.append(env_path)
    if sys.platform == "win32":
        chrome_candidates = [
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
            shutil.which("chrome"),
            shutil.which("chrome.exe"),
        ]
        candidates.extend(chrome_candidates)
    else:
        candidates.extend([shutil.which("google-chrome"), shutil.which("chrome"), shutil.which("chromium")])
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise GeminiAutomationError("Không tìm thấy Chrome. Có thể set biến môi trường PYVIDEOTRANS_CHROME_PATH.")


def _browser_process_running(browser_path):
    if sys.platform != "win32":
        return False
    name = Path(browser_path).name.lower()
    if name not in {"chrome.exe", "msedge.exe"}:
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        return False
    return name in (result.stdout or "").lower()


def _automation_profile_process_ids():
    if sys.platform != "win32":
        return []
    profile = str(AUTOMATION_PROFILE_DIR).lower().replace("/", "\\")
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { ($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') -and "
        f"$_.CommandLine -and $_.CommandLine.ToLower().Contains('{profile}') }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    ids = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            ids.append(int(line))
    return ids


def _cdp_uses_fixed_chrome_profile(port):
    if sys.platform != "win32":
        return True
    profile = str(AUTOMATION_PROFILE_DIR).lower().replace("/", "\\")
    needle = f"--remote-debugging-port={int(port)}"
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -and "
        f"$_.CommandLine.ToLower().Contains('{needle}') }} | "
        "Select-Object -ExpandProperty CommandLine"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        return False
    command_lines = (result.stdout or "").lower().replace("/", "\\")
    return bool(command_lines and profile in command_lines)


def _terminate_automation_profile_processes(log):
    ids = _automation_profile_process_ids()
    if not ids:
        return
    log("Đang đóng cửa sổ Chrome Gemini automation cũ của tool để mở lại với DevTools...")
    for pid in ids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    time.sleep(1)


def _js(value):
    return json.dumps(str(value), ensure_ascii=False)


def _safe_output_srt(srt_path):
    srt = Path(srt_path)
    return srt.with_name(f"{srt.stem}_vi.srt")


class GeminiBrowserAutomation:
    def __init__(self, gem_url=None, log=None, timeout=900, port=None):
        self.gem_url = gem_url or DEFAULT_GEM_URL
        self.log = log or (lambda _text: None)
        self.timeout = timeout
        self.port = int(port or DEFAULT_CDP_PORT)
        self.browser_process = None
        self.page = None

    def start(self, open_url=None):
        open_url = open_url or LOGIN_URL
        version_url = f"http://127.0.0.1:{self.port}/json/version"
        try:
            _json_request(version_url)
            if _cdp_uses_fixed_chrome_profile(self.port):
                self.log("Đang dùng lại Chrome profile cố định đã mở với quyền tự động.")
                return
            _terminate_automation_profile_processes(self.log)
            time.sleep(0.5)
            try:
                _json_request(version_url)
                raise GeminiAutomationError(
                    f"Cổng tự động {self.port} đang do một trình duyệt khác chiếm, không phải Chrome profile cố định của tool. "
                    "Hãy đóng cửa sổ Chrome/Edge automation cũ hoặc đổi PYVIDEOTRANS_GEMINI_CDP_PORT."
                )
            except GeminiAutomationError:
                raise
            except Exception:
                pass
        except GeminiAutomationError:
            raise
        except Exception:
            _terminate_automation_profile_processes(self.log)

        browser = _find_browser_executable()
        if not _is_port_free(self.port):
            raise GeminiAutomationError(
                f"Cổng tự động {self.port} đang bị chương trình khác chiếm. "
                "Hãy đóng cửa sổ Chrome automation cũ hoặc đổi PYVIDEOTRANS_GEMINI_CDP_PORT."
            )
        AUTOMATION_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        cmd = [
            browser,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={AUTOMATION_PROFILE_DIR}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            "--new-window",
            open_url,
        ]
        self.log(f"Đang mở Chrome profile cố định: {AUTOMATION_PROFILE_DIR}")
        self.browser_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                _json_request(version_url)
                return
            except Exception:
                time.sleep(0.4)
        raise GeminiAutomationError(
            "Không kết nối được Chrome DevTools. Hãy đóng cửa sổ Gemini automation cũ rồi bấm 'Mở Gemini Gem' lại."
        )

    def open_login(self):
        self.start(open_url=LOGIN_URL)
        page = self._connect_page(prefer_gemini=False, new_url=LOGIN_URL)
        page.send("Page.navigate", {"url": LOGIN_URL})
        page.wait_event("Page.loadEventFired", timeout=45)
        self.log("Đã mở trang đăng nhập Google trong Chrome profile cố định. Đăng nhập xong hãy bấm 'Bắt đầu xử lý tất cả'.")

    def _connect_page(self, prefer_gemini=True, new_url=None):
        if self.page:
            return self.page
        tabs = _json_request(f"http://127.0.0.1:{self.port}/json/list")
        page_info = None
        if prefer_gemini:
            for tab in tabs:
                if tab.get("type") == "page" and "gemini.google.com" in tab.get("url", ""):
                    page_info = tab
                    break
        if not page_info:
            for tab in tabs:
                if tab.get("type") == "page":
                    page_info = tab
                    break
        if not page_info:
            encoded = urllib.parse.quote(new_url or self.gem_url, safe="")
            page_info = _json_request(f"http://127.0.0.1:{self.port}/json/new?{encoded}", method="PUT")
        self.page = CdpPage(page_info["webSocketDebuggerUrl"])
        self.page.send("Page.enable")
        self.page.send("Runtime.enable")
        self.page.send("DOM.enable")
        try:
            self.page.send("Page.bringToFront")
        except Exception:
            pass
        return self.page

    def _navigate_gem(self):
        page = self._connect_page()
        self.log(f"Đang mở Gem URL: {self.gem_url}")
        page.send("Page.bringToFront")
        page.send("Page.navigate", {"url": self.gem_url})
        page.wait_event("Page.loadEventFired", timeout=45)
        page.send("Page.bringToFront")
        self._wait_for_gem_ready(page)
        self.log("Gem đã sẵn sàng, bắt đầu upload SRT.")
        return page

    def _wait_for_gem_ready(self, page):
        deadline = time.time() + 90
        while time.time() < deadline:
            state = page.evaluate(
                """
                (() => {
                    const body = document.body ? document.body.innerText : "";
                    const input = document.querySelector('textarea, div[contenteditable="true"], rich-textarea');
                    const loginText = /sign in|đăng nhập|dang nhap/i.test(body);
                    return {ready: !!input, loginText, body: body.slice(0, 500)};
                })()
                """
            )
            if state and state.get("ready"):
                return
            if state and state.get("loginText"):
                self.log("Chrome đã mở Gemini nhưng chưa thấy trạng thái đăng nhập. Hãy đăng nhập Google trong Chrome profile cố định.")
            time.sleep(1)
        raise GeminiAutomationError("Gemini chưa sẵn sàng. Hãy bấm 'Mở Gemini Gem', đăng nhập trong Chrome profile cố định rồi chạy lại.")

    def translate_srt(self, srt_path, output_path=None, force=False):
        srt_path = Path(srt_path)
        if not srt_path.exists():
            raise GeminiAutomationError(f"Không thấy file SRT: {srt_path}")
        output_path = Path(output_path or _safe_output_srt(srt_path))
        if output_path.exists() and output_path.stat().st_size > 0 and not force:
            self.log(f"Đã có bản dịch, dùng lại: {output_path}")
            return output_path
        if output_path.exists() and force:
            self.log(f"Dịch lại, ghi đè bản cũ: {output_path}")

        if not self.browser_process:
            self.start(open_url=LOGIN_URL)
        page = self._navigate_gem()
        self._set_download_dir(page, output_path.parent)
        before_files = self._file_snapshot(output_path.parent)
        self._upload_file(page, srt_path)
        self._submit_prompt(page, srt_path.name)
        self._wait_for_result(page)
        clicked = self._click_download_code(page)
        if clicked:
            downloaded = self._wait_downloaded_file(output_path.parent, before_files)
            if downloaded:
                self._rename_download(downloaded, output_path)
                return output_path
            self.log("Đã bấm tải code nhưng không thấy file tải xuống, sẽ lấy nội dung code block trực tiếp.")
        else:
            self.log("Không tìm thấy nút tải code, sẽ lấy nội dung code block trực tiếp.")

        self.log("Không bắt được file tải xuống, fallback lưu nội dung code block trực tiếp.")
        code_text = self._extract_last_code_block(page)
        if not code_text:
            raise GeminiAutomationError("Gemini đã trả lời nhưng không tìm thấy nội dung SRT/code block để lưu.")
        output_path.write_text(self._normalize_srt_text(code_text), encoding="utf-8")
        return output_path

    def _set_download_dir(self, page, folder):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        params = {"behavior": "allow", "downloadPath": str(folder)}
        try:
            page.send("Browser.setDownloadBehavior", params)
        except Exception:
            page.send("Page.setDownloadBehavior", params)

    def _click_rect(self, page, rect):
        if not rect:
            return False
        x = rect.get("x", 0) + rect.get("width", 0) / 2
        y = rect.get("y", 0) + rect.get("height", 0) / 2
        page.click_point(x, y)
        return True

    def _find_upload_menu_rect(self, page):
        return page.evaluate(
            """
            (() => {
                const textOf = el => [
                    el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title')
                ].filter(Boolean).join(' ').toLowerCase();
                const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const nodes = [...document.querySelectorAll('button,[role="button"],[role="menuitem"],li,div,span')]
                    .filter(visible);
                const matches = nodes
                    .filter(el => /tải tệp lên|tai tep len|tải lên từ máy|upload file|upload files|upload from computer/.test(textOf(el)))
                    .map(el => {
                        const r = el.getBoundingClientRect();
                        return {x: r.left, y: r.top, width: r.width, height: r.height, text: textOf(el)};
                    })
                    .filter(r => r.width > 20 && r.height > 12 && r.width < 520 && r.height < 96);
                if (!matches.length) return null;
                matches.sort((a, b) => (a.y - b.y) || ((a.width * a.height) - (b.width * b.height)));
                return matches[0];
            })()
            """
        )

    def _find_attach_button_rect(self, page):
        return page.evaluate(
            """
            (() => {
                const textOf = el => [
                    el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title')
                ].filter(Boolean).join(' ').toLowerCase();
                const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const buttons = [...document.querySelectorAll('button,[role="button"]')].filter(visible);
                let target = buttons.find(el => /nội dung tải lên|noi dung tai len|tải lên và công cụ|tai len va cong cu|tải tệp lên|tai tep len|upload file|upload|attach|đính kèm|dinh kem|them tep|thêm tệp/.test(textOf(el)));
                if (!target) {
                    const plusLike = buttons
                        .map(el => {
                            const r = el.getBoundingClientRect();
                            return {el, r, text: textOf(el)};
                        })
                        .filter(x => x.r.bottom > window.innerHeight - 140 && x.r.left < 260 && x.r.width >= 24 && x.r.height >= 24);
                    target = (plusLike[0] || {}).el;
                }
                if (!target) return null;
                const r = target.getBoundingClientRect();
                return {x: r.left, y: r.top, width: r.width, height: r.height, text: textOf(target)};
            })()
            """
        )

    def _upload_file(self, page, srt_path):
        self.log(f"Upload SRT lên Gemini: {Path(srt_path).name}")
        page.send("Page.setInterceptFileChooserDialog", {"enabled": True})
        self._click_attach_or_upload(page)
        event = page.wait_event("Page.fileChooserOpened", timeout=8)
        if not event:
            self._click_upload_menu_item(page)
            event = page.wait_event("Page.fileChooserOpened", timeout=12)
        if not event:
            self._click_upload_menu_item(page)
            event = page.wait_event("Page.fileChooserOpened", timeout=6)
        if not event:
            self._click_file_input(page)
            event = page.wait_event("Page.fileChooserOpened", timeout=8)
        if event and event.get("params", {}).get("backendNodeId"):
            page.send(
                "DOM.setFileInputFiles",
                {"backendNodeId": event["params"]["backendNodeId"], "files": [str(Path(srt_path).resolve())]},
            )
        else:
            document = page.send("DOM.getDocument", {"depth": 2, "pierce": True})
            node_id = page.send("DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": "input[type=file]"}).get("nodeId")
            if not node_id:
                raise GeminiAutomationError("Không tìm thấy ô input upload file trong Gemini.")
            page.send("DOM.setFileInputFiles", {"nodeId": node_id, "files": [str(Path(srt_path).resolve())]})
        time.sleep(4)

    def _click_attach_or_upload(self, page):
        rect = self._find_upload_menu_rect(page)
        if rect:
            self.log("Menu upload đang mở, bấm trực tiếp 'Tải tệp lên'.")
            self._click_rect(page, rect)
            time.sleep(0.8)
            return True
        rect = self._find_attach_button_rect(page)
        if not rect:
            return False
        self._click_rect(page, rect)
        time.sleep(0.8)
        return True

    def _click_upload_menu_item(self, page):
        rect = self._find_upload_menu_rect(page)
        return self._click_rect(page, rect)

    def _click_file_input(self, page):
        return page.evaluate(
            """
            (() => {
                const input = [...document.querySelectorAll('input[type=file]')].pop();
                if (!input) return false;
                input.click();
                return true;
            })()
            """
        )

    def _find_prompt_box_rect(self, page):
        return page.evaluate(
            """
            (() => {
                const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const boxes = [...document.querySelectorAll(
                    'textarea, div[role="textbox"][contenteditable="true"], rich-textarea div[contenteditable="true"]'
                )].filter(visible).map(el => {
                    const r = el.getBoundingClientRect();
                    return {x: r.left, y: r.top, width: r.width, height: r.height, tag: el.tagName};
                }).filter(r => r.width > 20 && r.height > 12);
                if (!boxes.length) return null;
                boxes.sort((a, b) => b.y - a.y);
                return boxes[0];
            })()
            """
        )

    def _find_send_button_rect(self, page):
        return page.evaluate(
            """
            (() => {
                const textOf = el => [
                    el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title')
                ].filter(Boolean).join(' ').toLowerCase();
                const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const buttons = [...document.querySelectorAll('button,[role="button"]')]
                    .filter(el => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
                    .map(el => {
                        const r = el.getBoundingClientRect();
                        return {x: r.left, y: r.top, width: r.width, height: r.height, text: textOf(el)};
                    })
                    .filter(r => r.width >= 24 && r.height >= 24);
                const named = buttons.filter(r => /gửi tin nhắn|gui tin nhan|send message|send|submit/.test(r.text));
                const candidates = named.length ? named : buttons.filter(r => r.y > window.innerHeight - 180 && r.x > window.innerWidth - 180);
                if (!candidates.length) return null;
                candidates.sort((a, b) => (b.x + b.y) - (a.x + a.y));
                return candidates[0];
            })()
            """
        )

    def _insert_prompt_text(self, page, prompt):
        rect = self._find_prompt_box_rect(page)
        if not rect:
            return False
        self._click_rect(page, rect)
        time.sleep(0.2)
        page.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Control", "code": "ControlLeft", "windowsVirtualKeyCode": 17})
        page.send(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 2},
        )
        page.send(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 2},
        )
        page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Control", "code": "ControlLeft", "windowsVirtualKeyCode": 17})
        page.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
        page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
        page.send("Input.insertText", {"text": prompt})
        time.sleep(0.8)
        return True

    def _submit_prompt(self, page, filename):
        prompt = (
            f"Dịch file SRT vừa upload ({filename}) sang tiếng Việt. "
            "Giữ nguyên số thứ tự, timestamp và định dạng .srt. "
            "Chỉ trả về nội dung SRT trong một code block, không giải thích thêm."
        )
        self.log("Gửi yêu cầu dịch cho Gemini...")
        inserted = self._insert_prompt_text(page, prompt)
        if not inserted:
            raise GeminiAutomationError("Không tìm thấy ô nhập prompt của Gemini.")
        sent = False
        for _ in range(8):
            rect = self._find_send_button_rect(page)
            if rect:
                self._click_rect(page, rect)
                sent = True
                break
            time.sleep(0.5)
        if not sent:
            page.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
            page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
        time.sleep(1)

    def _wait_for_result(self, page):
        self.log("Đang chờ Gemini dịch xong...")
        deadline = time.time() + self.timeout
        stable_since = None
        last_len = -1
        while time.time() < deadline:
            state = page.evaluate(
                """
                (() => {
                    const body = document.body ? document.body.innerText : "";
                    const buttons = [...document.querySelectorAll('button,[role="button"]')];
                    const texts = buttons.map(el => [
                        el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title')
                    ].filter(Boolean).join(' ').toLowerCase());
                    const hasDownload = texts.some(t => /tải mã xuống|tải xuống|download code|download/.test(t));
                    const stopping = texts.some(t => /stop|ngừng tạo|ngung tao|dừng tạo|dung tao|dừng/.test(t));
                    const codeText = [...document.querySelectorAll('pre code, pre, code')]
                        .map(n => n.innerText || n.textContent || '').join('\\n');
                    const hasSrt = /\\d+\\s*\\n\\s*\\d\\d:\\d\\d:\\d\\d[,\\.]\\d+\\s*-->/.test(codeText);
                    return {len: body.length, hasDownload, stopping, hasSrt};
                })()
                """
            )
            text_len = int((state or {}).get("len") or 0)
            ready = bool((state or {}).get("hasDownload") or (state or {}).get("hasSrt"))
            stopping = bool((state or {}).get("stopping"))
            if text_len != last_len:
                last_len = text_len
                stable_since = time.time()
            if ready and not stopping and stable_since and time.time() - stable_since >= 4:
                self.log("Gemini đã trả kết quả dịch.")
                return
            time.sleep(1.5)
        raise GeminiAutomationError("Quá thời gian chờ Gemini dịch xong.")

    def _click_download_code(self, page):
        self.log("Bấm nút tải code từ Gemini...")
        return bool(
            page.evaluate(
                """
                (() => {
                    const textOf = el => [
                        el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title')
                    ].filter(Boolean).join(' ').toLowerCase();
                    const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const buttons = [...document.querySelectorAll('button,[role="button"]')].filter(visible);
                    const matches = buttons.filter(el => /tải mã xuống|tải xuống|download code|download/.test(textOf(el)));
                    const target = matches[matches.length - 1];
                    if (!target) return false;
                    target.click();
                    return true;
                })()
                """
            )
        )

    def _extract_last_code_block(self, page):
        return page.evaluate(
            """
            (() => {
                const nodes = [...document.querySelectorAll('pre code, pre, code')];
                const texts = nodes.map(n => n.innerText || n.textContent || '').filter(t => /-->/.test(t));
                return texts.length ? texts[texts.length - 1] : '';
            })()
            """
        )

    def _file_snapshot(self, folder):
        folder = Path(folder)
        return {p.resolve(): p.stat().st_mtime for p in folder.glob("*") if p.is_file()}

    def _wait_downloaded_file(self, folder, before_files):
        folder = Path(folder)
        deadline = time.time() + 90
        candidate = None
        while time.time() < deadline:
            active = list(folder.glob("*.crdownload")) + list(folder.glob("*.tmp"))
            files = [p for p in folder.glob("*") if p.is_file() and p.suffix.lower() not in {".crdownload", ".tmp"}]
            changed = [p for p in files if p.resolve() not in before_files or p.stat().st_mtime > before_files.get(p.resolve(), 0)]
            if changed and not active:
                newest = max(changed, key=lambda p: p.stat().st_mtime)
                if candidate == newest:
                    return newest
                candidate = newest
            time.sleep(0.8)
        return None

    def _rename_download(self, downloaded, output_path):
        downloaded = Path(downloaded)
        output_path = Path(output_path)
        text = downloaded.read_text(encoding="utf-8-sig", errors="ignore")
        output_path.write_text(self._normalize_srt_text(text), encoding="utf-8")
        if downloaded.resolve() != output_path.resolve():
            try:
                downloaded.unlink()
            except OSError:
                pass
        self.log(f"Đã lưu SRT dịch: {output_path}")

    def _normalize_srt_text(self, text):
        text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        text = re_sub_code_fence(text)
        return text.strip() + "\n"


def re_sub_code_fence(text):
    import re

    text = re.sub(r"^\s*```(?:srt|txt|text)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```\s*$", "", text)
    return text
