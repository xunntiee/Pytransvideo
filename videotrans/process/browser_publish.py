# -*- coding: utf-8 -*-
"""Browser-assisted publishing adapters.

The adapters stage a video and metadata in the creator page. They deliberately
do not click the final Publish/Post button; the user reviews each platform and
confirms the consequential action in the browser.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import time

from videotrans.process.publish_queue import (
    load_publish_queue,
    update_platform_status,
)


class PublishAdapterError(RuntimeError):
    pass


class PublishLoginRequired(PublishAdapterError):
    pass


@dataclass(frozen=True)
class BrowserPublisherSpec:
    id: str
    name: str
    upload_url: str
    file_selectors: tuple[str, ...] = ("input[type=file]",)
    upload_button_words: tuple[str, ...] = (
        "upload",
        "select file",
        "choose file",
        "t\u1ea3i l\u00ean",
        "ch\u1ecdn t\u1ec7p",
        "\u4e0a\u4f20",
        "\u53d1\u5e03\u89c6\u9891",
    )


BROWSER_PUBLISHERS = {
    "youtube": BrowserPublisherSpec(
        "youtube",
        "YouTube Studio",
        "https://studio.youtube.com/",
    ),
    "tiktok": BrowserPublisherSpec(
        "tiktok",
        "TikTok Studio",
        "https://www.tiktok.com/tiktokstudio/upload",
    ),
    "douyin": BrowserPublisherSpec(
        "douyin",
        "Douyin Creator",
        "https://creator.douyin.com/creator-micro/content/upload",
    ),
    "bilibili": BrowserPublisherSpec(
        "bilibili",
        "Bilibili Studio",
        "https://member.bilibili.com/platform/upload/video/frame",
    ),
    "kuaishou": BrowserPublisherSpec(
        "kuaishou",
        "Kuaishou Creator",
        "https://cp.kuaishou.com/article/publish/video",
    ),
    "xiaohongshu": BrowserPublisherSpec(
        "xiaohongshu",
        "Xiaohongshu Creator",
        "https://creator.xiaohongshu.com/publish/publish",
    ),
    "facebook": BrowserPublisherSpec(
        "facebook",
        "Meta Business Suite",
        "https://business.facebook.com/latest/content_calendar",
    ),
}


def publisher_spec(platform):
    spec = BROWSER_PUBLISHERS.get(str(platform or "").strip().casefold())
    if not spec:
        raise PublishAdapterError(f"Ch\u01b0a c\u00f3 browser adapter cho n\u1ec1n t\u1ea3ng: {platform}")
    return spec


def _job_by_id(queue_path, job_id):
    queue = load_publish_queue(queue_path)
    job = next((item for item in queue["jobs"] if item.get("id") == job_id), None)
    if not job:
        raise KeyError(job_id)
    return job


def formatted_caption(job):
    parts = [str(job.get("title") or "").strip(), str(job.get("description") or "").strip()]
    hashtags = " ".join(f"#{tag}" for tag in job.get("hashtags") or [])
    if hashtags:
        parts.append(hashtags)
    return "\n\n".join(part for part in parts if part)


class CdpPublishBrowser:
    """Small CDP client backed by the project's persistent Chrome profile."""

    def __init__(self, log=None, timeout=120):
        self.log = log or (lambda _message: None)
        self.timeout = int(timeout)
        self.automation = None
        self.page = None

    def open(self, url):
        from videotrans.component.gemini_browser_automation import GeminiBrowserAutomation

        self.automation = GeminiBrowserAutomation(gem_url=url, log=self.log, timeout=self.timeout)
        self.automation.start(open_url=url)
        self.page = self.automation._connect_page(prefer_gemini=False, new_url=url)
        self.page.send("Page.bringToFront")
        self.page.send("Page.navigate", {"url": url})
        self.page.wait_event("Page.loadEventFired", timeout=60)
        self.page.send("Page.bringToFront")
        return self.page

    def _click_upload_button(self, words):
        expression = """
        ((words) => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
          };
          const textOf = (el) => [
            el.innerText, el.textContent, el.getAttribute('aria-label'),
            el.getAttribute('title'), el.getAttribute('data-e2e')
          ].filter(Boolean).join(' ').toLowerCase();
          const nodes = [...document.querySelectorAll('button, [role=button], label, a')];
          const target = nodes.find(el => visible(el) && words.some(word => textOf(el).includes(word)));
          if (!target) return false;
          target.click();
          return true;
        })(%s)
        """ % json.dumps([word.casefold() for word in words], ensure_ascii=False)
        return bool(self.page.evaluate(expression))

    def _set_file(self, selectors, video_path):
        document = self.page.send("DOM.getDocument", {"depth": -1, "pierce": True})
        root_id = document["root"]["nodeId"]
        for selector in selectors:
            result = self.page.send(
                "DOM.querySelector",
                {"nodeId": root_id, "selector": selector},
            )
            node_id = result.get("nodeId")
            if node_id:
                self.page.send(
                    "DOM.setFileInputFiles",
                    {"nodeId": node_id, "files": [str(Path(video_path).resolve())]},
                )
                return True
        return False

    def wait_and_set_file(self, spec, video_path):
        deadline = time.time() + self.timeout
        clicked = False
        while time.time() < deadline:
            if self._set_file(spec.file_selectors, video_path):
                return
            if not clicked:
                clicked = self._click_upload_button(spec.upload_button_words)
            time.sleep(1)
        raise PublishLoginRequired(
            f"Kh\u00f4ng t\u00ecm th\u1ea5y \u00f4 upload tr\u00ean {spec.name}. "
            "H\u00e3y \u0111\u0103ng nh\u1eadp/m\u1edf trang upload trong Chrome profile c\u1ee7a tool r\u1ed3i Retry."
        )

    def fill_metadata(self, job):
        title = str(job.get("title") or "").strip()
        description = str(job.get("description") or "").strip()
        caption = formatted_caption(job)
        expression = """
        ((data) => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
          };
          const labelOf = (el) => [
            el.getAttribute('placeholder'), el.getAttribute('aria-label'),
            el.getAttribute('name'), el.getAttribute('data-e2e')
          ].filter(Boolean).join(' ').toLowerCase();
          const setValue = (el, value) => {
            el.focus();
            if (el.isContentEditable) {
              el.innerText = value;
              el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
            } else {
              const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              if (setter) setter.call(el, value); else el.value = value;
              el.dispatchEvent(new Event('input', {bubbles: true}));
              el.dispatchEvent(new Event('change', {bubbles: true}));
            }
          };
          const fields = [...document.querySelectorAll('input, textarea, [contenteditable=true]')].filter(visible);
          const titleField = fields.find(el => /title|ti\u00eau \u0111\u1ec1|\u6807\u9898/.test(labelOf(el)));
          const descriptionField = fields.find(el => /description|m\u00f4 t\u1ea3|\u7b80\u4ecb/.test(labelOf(el)));
          const captionField = fields.find(el => /caption|ch\u00fa th\u00edch|describe|\u6587\u6848|\u4f5c\u54c1\u63cf\u8ff0/.test(labelOf(el)));
          if (titleField && data.title) setValue(titleField, data.title);
          if (descriptionField && data.description) setValue(descriptionField, data.description);
          if (captionField && data.caption) setValue(captionField, data.caption);
          return {title: !!titleField, description: !!descriptionField, caption: !!captionField};
        })(%s)
        """ % json.dumps(
            {"title": title, "description": description, "caption": caption},
            ensure_ascii=False,
        )
        return self.page.evaluate(expression) or {}

    def stage(self, spec, job):
        video_path = Path(job.get("video") or "")
        if not video_path.is_file():
            raise PublishAdapterError(f"Video kh\u00f4ng t\u1ed3n t\u1ea1i: {video_path}")
        self.log(f"M\u1edf {spec.name}: {spec.upload_url}")
        self.open(spec.upload_url)
        self.wait_and_set_file(spec, video_path)
        self.log(f"\u0110\u00e3 ch\u1ecdn video cho {spec.name}: {video_path.name}")
        time.sleep(3)
        fields = self.fill_metadata(job)
        self.log(
            f"\u0110\u00e3 staging {spec.name}; metadata t\u00ecm th\u1ea5y: "
            f"title={fields.get('title')}, description={fields.get('description')}, caption={fields.get('caption')}."
        )
        return {
            "platform": spec.id,
            "platform_name": spec.name,
            "upload_url": spec.upload_url,
            "metadata_fields": fields,
            "requires_final_confirmation": True,
        }


def stage_publish_job(queue_path, job_id, platform, *, browser=None, log=None):
    """Upload one queued video into a creator form without final publishing."""
    queue_path = Path(queue_path)
    platform = str(platform or "").strip().casefold()
    spec = publisher_spec(platform)
    job = _job_by_id(queue_path, job_id)
    state = (job.get("platforms") or {}).get(platform)
    if state is None:
        raise PublishAdapterError(f"Job {job_id} kh\u00f4ng c\u00f3 n\u1ec1n t\u1ea3ng {platform}.")
    if state.get("status") == "published":
        raise PublishAdapterError(
            f"Job {job_id} \u0111\u00e3 \u0111\u00e1nh d\u1ea5u published tr\u00ean {platform}; kh\u00f4ng staging l\u1ea1i."
        )

    browser = browser or CdpPublishBrowser(log=log)
    update_platform_status(queue_path, job_id, platform, "publishing")
    try:
        result = browser.stage(spec, job)
        update_platform_status(queue_path, job_id, platform, "staged")
        return result
    except Exception as exc:
        update_platform_status(queue_path, job_id, platform, "failed", error=str(exc))
        raise
