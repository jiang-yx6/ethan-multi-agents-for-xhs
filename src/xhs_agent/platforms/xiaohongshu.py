"""小红书图文连接器：普通 Playwright 持久会话，不处理验证码或风控绕过。

页面地址、选择器及创作者身份接口参考 Easel 的 xhs_publish.py。
该接口与 DOM 均为网站内部实现，并非官方开放 API，可能随改版变化。
来源与许可证见 docs/platforms.md 和 skills/LICENSE。
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from .base import UnsupportedCapability
from .models import (
    AccountIdentity, PlatformCapabilities, PlatformContent, PublishJob,
    PublishReceipt, PublishRequest, ValidationReport,
)

HOME_URL = "https://creator.xiaohongshu.com/new/home"
LOGIN_URL = "https://creator.xiaohongshu.com/login"
ACCOUNT_URL = "https://creator.xiaohongshu.com/api/galaxy/user/info"
PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
BROWSER_VIEWPORT = {"width": 1440, "height": 1000}

# 集中维护。自定义组件可能使用闭合 Shadow DOM，单独处理其按钮位置。
SELECTORS = {
    "tab": "div.creator-tab",
    "upload": "input[type=file]",
    "preview": ".img-preview-area .pr",
    "title": "div.d-input input, input[placeholder*='标题']",
    "body": "div.ql-editor, div.editor-container [contenteditable='true']",
    "topic": "#creator-editor-topic-container .item",
    "overflow": "div.title-container div.max_suffix, div.edit-container div.length-error",
    "button": ".publish-page-publish-btn button.bg-red",
    "button_host": "xhs-publish-btn",
    "toast": ".d-message, .d-toast, [class*=toast]",
}


@contextmanager
def profile_lock(profile_dir: Path):
    """OS 文件锁在进程退出后自动释放，避免两个浏览器同时使用同一会话。"""
    profile_dir.mkdir(parents=True, exist_ok=True)
    handle = (profile_dir.parent / ".browser.lock").open("a+b")
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("该账号的浏览器会话正在使用，请关闭其他登录或发布进程") from exc
        yield
    finally:
        handle.close()


@contextmanager
def browser_session(profile_dir: Path):
    channel = os.environ.get("XHS_BROWSER_CHANNEL", "").strip()
    if channel not in {"", "chromium", "chrome", "msedge"}:
        raise ValueError("XHS_BROWSER_CHANNEL 仅支持 chromium、chrome 或 msedge")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("请先安装浏览器支持：uv sync --extra browser；uv run playwright install chromium") from exc
    with profile_lock(profile_dir), sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir), headless=False, locale="zh-CN", channel=channel or None,
            viewport=BROWSER_VIEWPORT,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            yield context, page
        finally:
            context.close()


def identity_from_payload(payload: object, status: int = 200) -> AccountIdentity:
    if status == 401:
        return AccountIdentity(state="logged_out", message="登录已失效，请重新登录")
    if status == 200 and isinstance(payload, dict) and payload.get("success") is True:
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("userId"), str) and data["userId"].strip():
            nickname = data.get("userName") or data.get("nickname") or ""
            return AccountIdentity(state="authenticated", user_id=data["userId"].strip(),
                                   nickname=nickname if isinstance(nickname, str) else "")
    return AccountIdentity(state="unknown", message="创作者身份接口暂未确认登录状态，请检查浏览器中的登录或验证提示")


def rendered_body(content: PlatformContent) -> str:
    tags = ["#" + tag.lstrip("#").strip() for tag in content.tags]
    return content.body + ("\n" + " ".join(tags) if tags else "")


class XiaohongshuConnector:
    def __init__(self, session_factory=browser_session):
        # 注入会话工厂供离线测试，不启动真实浏览器。
        self._session = session_factory

    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(platform="xiaohongshu", image_publish=True)

    @staticmethod
    def _identity(context) -> AccountIdentity:
        try:
            response = context.request.get(ACCOUNT_URL, headers={"Referer": HOME_URL}, timeout=8000)
            payload = response.json() if response.status == 200 else None
            return identity_from_payload(payload, response.status)
        except Exception:
            return AccountIdentity(state="unknown", message="身份校验请求失败，未修改已绑定的账号身份")

    def whoami(self, profile_dir: Path) -> AccountIdentity:
        with self._session(profile_dir) as (context, page):
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            return self._identity(context)

    def login(self, profile_dir: Path, *, timeout: int = 180) -> AccountIdentity:
        if not 1 <= timeout <= 600:
            raise ValueError("登录等待时间应在 1 到 600 秒之间")
        with self._session(profile_dir) as (context, page):
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            identity = self._identity(context)
            if identity.state == "authenticated":
                return identity
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                identity = self._identity(context)
                if identity.state == "authenticated":
                    return identity
                page.wait_for_timeout(1500)
            return AccountIdentity(state="unknown", message="登录等待结束；可重新运行 login，在窗口内完成扫码或其他验证")

    def validate(self, content: PlatformContent) -> ValidationReport:
        errors = []
        # 这是首版连接器支持范围；页面中的实时校验仍是最终约束。
        if not content.title.strip() or len(content.title) > 20:
            errors.append("标题须为 1～20 个字符，请修改后重新审核")
        if not content.body.strip() or len(rendered_body(content)) > 1000:
            errors.append("正文不能为空，正文与话题合计暂限 1000 个字符")
        if len(content.tags) > 10 or any(not t.lstrip("#").strip() or "#" in t.lstrip("#")
                                       or any(c.isspace() for c in t.lstrip("#")) for t in content.tags):
            errors.append("话题暂限 10 个，每个话题须为连续文字")
        if not 1 <= len(content.images) <= 9:
            errors.append("首版图文发布支持 1～9 张本地 JPG、PNG 或 WebP 图片")
        for asset in content.images:
            path = Path(asset.path)
            if not path.is_file():
                errors.append(f"图片不存在：{path}")
                continue
            with path.open("rb") as handle:
                header = handle.read(12)
            valid_header = ((path.suffix.lower() in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff"))
                            or (path.suffix.lower() == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
                            or (path.suffix.lower() == ".webp" and header[:4] == b"RIFF" and header[8:12] == b"WEBP"))
            if not valid_header:
                errors.append(f"图片格式不支持或文件头不匹配：{path.name}")
            if path.stat().st_size > 20 * 1024 * 1024:
                errors.append(f"首版图片大小暂限 20 MB：{path.name}")
        return ValidationReport(errors=errors, warnings=["提交后可能进入平台审核；当前需人工核实笔记发布结果"])

    @staticmethod
    def _visible(page, selector):
        return next((el for el in page.locator(selector).all() if el.is_visible()), None)

    @staticmethod
    def _in_viewport(page, element, *, timeout_ms: int = 500) -> bool:
        box = element.bounding_box(timeout=timeout_ms)
        viewport = page.viewport_size
        if not viewport:
            viewport = page.evaluate("({width: innerWidth, height: innerHeight})")
        return bool(box and box["width"] > 0 and box["height"] > 0
                    and box["x"] < viewport["width"] and box["y"] < viewport["height"]
                    and box["x"] + box["width"] > 0 and box["y"] + box["height"] > 0)

    def _select_image_tab(self, page, *, timeout_ms: int = 15000) -> None:
        try:
            from playwright.sync_api import Error as BrowserError
        except ImportError:
            BrowserError = TimeoutError  # 注入离线页面进行测试时无需安装 browser extra。

        deadline = time.monotonic() + timeout_ms / 1000
        tabs = page.locator(SELECTORS["tab"]).filter(has_text=re.compile(r"^\s*上传图文\s*$"))
        matched = 0

        def budget(limit):
            return max(1, min(limit, int((deadline - time.monotonic()) * 1000)))

        while time.monotonic() < deadline:
            if "/login" in page.url:
                raise ValueError("发布页要求重新登录")
            candidates = tabs.all()  # 每轮重新读取，允许页面异步挂载或替换标签。
            matched = len(candidates)
            for allow_scroll in (False, True):
                for tab in candidates:
                    if time.monotonic() >= deadline:
                        break
                    try:
                        if (not tab.is_visible() or not tab.is_enabled(timeout=budget(250))
                                or tab.get_attribute("aria-disabled", timeout=budget(250)) == "true"):
                            continue
                        in_viewport = self._in_viewport(page, tab, timeout_ms=budget(250))
                        if not allow_scroll and not in_viewport:
                            continue
                        if allow_scroll:
                            # 先处理屏幕内的标签，避免离屏副本拖动页面或耗尽等待时间。
                            if in_viewport:
                                continue
                            tab.scroll_into_view_if_needed(timeout=budget(500))
                            if not self._in_viewport(page, tab, timeout_ms=budget(250)):
                                continue
                        # trial 检查稳定性和遮挡，不强制触发 DOM 事件。
                        tab.click(trial=True, timeout=budget(600))
                        tab.click(timeout=budget(1200))
                        return
                    except BrowserError:
                        if page.is_closed():
                            raise ValueError("发布窗口已关闭，请重新准备发布") from None
            if time.monotonic() < deadline:
                page.wait_for_timeout(budget(150))
        raise ValueError(f"无法点击“上传图文”标签：匹配到 {matched} 个标签，但仍在视口外、被遮挡或不可用。"
                         "请检查发布窗口是否有登录验证、弹窗或布局异常，再重新准备发布")

    def _prepare(self, page, content: PlatformContent):
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
        if "/login" in page.url:
            raise ValueError("发布页要求重新登录")
        self._select_image_tab(page)
        for index, asset in enumerate(content.images):
            path = Path(asset.path)
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != asset.sha256:
                raise ValueError("图片在审核后发生变化，请重新审核")
            page.locator(SELECTORS["upload"]).first.set_input_files({
                "name": path.name, "mimeType": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "buffer": data,
            })
            page.locator(SELECTORS["preview"]).nth(index).wait_for(state="visible", timeout=60000)
        page.locator(SELECTORS["title"]).first.fill(content.title)
        editor = page.locator(SELECTORS["body"]).first
        editor.fill(content.body)
        # 只选择与用户话题完全匹配的联想项，避免把相近话题自动替换进去。
        for raw_tag in content.tags:
            tag = raw_tag.lstrip("#").strip()
            editor.press("Control+End")
            page.keyboard.insert_text(" #" + tag)
            try:
                page.locator(SELECTORS["topic"]).first.wait_for(state="visible", timeout=3000)
            except Exception:
                pass  # 无联想时保留 #文字；不选择无关话题。
            exact = next((el for el in page.locator(SELECTORS["topic"]).all()
                          if el.is_visible() and el.inner_text().strip().lstrip("#") == tag), None)
            if exact:
                exact.click()
            else:
                page.keyboard.press("Escape")
                page.keyboard.insert_text(" ")
        page.locator(SELECTORS["title"]).first.click()
        overflow = self._visible(page, SELECTORS["overflow"])
        if overflow:
            raise ValueError("平台长度校验未通过：" + overflow.inner_text())
        button = self._visible(page, SELECTORS["button"])
        if button:
            if not button.is_enabled() or button.get_attribute("aria-disabled") == "true":
                raise ValueError("平台发布按钮暂不可用，请检查素材与页面提示")
            button.scroll_into_view_if_needed()
            return button.click
        host = self._visible(page, SELECTORS["button_host"])
        if not host or host.get_attribute("is-publish") == "false" or host.get_attribute("submit-disabled") == "true":
            raise ValueError("未找到可用发布按钮，页面可能已改版")
        host.scroll_into_view_if_needed()
        box = host.bounding_box()
        if not box:
            raise ValueError("无法定位发布组件")
        return lambda: page.mouse.click(box["x"] + box["width"] * .62, box["y"] + box["height"] / 2)

    def _observe(self, page) -> PublishReceipt:
        # 只把明确的发布提示记为已提交；跳转、空白表单等不能证明发布。
        for _ in range(40):
            for element in page.locator(SELECTORS["toast"]).all():
                if element.is_visible() and "发布成功" in element.inner_text():
                    return PublishReceipt(state="submitted", message="页面提示发布成功，需到创作者后台核实笔记")
            page.wait_for_timeout(500)
        return PublishReceipt(state="unknown", message="已执行一次提交，未获得明确回执；请核实后台，勿重复发布")

    def submit(self, request: PublishRequest, profile_dir: Path) -> PublishReceipt:
        committing = False
        receipt = None
        try:
            self.validate(request.content).require_valid()
            with self._session(profile_dir) as (context, page):
                identity = self._identity(context)
                if identity.state != "authenticated" or identity.user_id != request.expected_user_id:
                    raise ValueError("登录身份未确认或与目标账号不同，请重新执行 accounts whoami")
                click = self._prepare(page, request.content)
                # 从这一刻开始任何异常都不能断言提交失败，也不能换个选择器再点。
                committing = True
                click()
                receipt = self._observe(page)
            return receipt
        except Exception as exc:
            # 关闭浏览器失败不能覆盖已经观测到的回执。
            if receipt:
                return receipt
            return PublishReceipt(state="unknown" if committing else "failed",
                                  message=("提交结果未核实，禁止直接重试：" if committing else "提交前失败：") + str(exc))

    def create_draft(self, request: PublishRequest, profile_dir: Path) -> PublishReceipt:
        raise UnsupportedCapability("首版小红书连接器尚不支持平台草稿箱")

    def reconcile(self, job: PublishJob, profile_dir: Path) -> PublishReceipt:
        raise UnsupportedCapability("尚未接入自动回查；请核实后台后使用 confirm-published 登记人工确认")
