"""使用本地 HTML 验证实际浏览器定位；不访问小红书、不使用账号或提交笔记。"""
import os

import pytest

browser_api = pytest.importorskip("playwright.sync_api")

from xhs_agent.platforms.xiaohongshu import BROWSER_VIEWPORT, XiaohongshuConnector


@pytest.fixture(scope="module")
def browser():
    channel = os.environ.get("XHS_BROWSER_CHANNEL") or ("chrome" if os.name == "nt" else None)
    with browser_api.sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(channel=channel, headless=True)
        except browser_api.Error as exc:
            if "Executable doesn't exist" in str(exc) or "distribution" in str(exc) and "not found" in str(exc):
                pytest.skip("未安装测试浏览器")
            raise
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport=BROWSER_VIEWPORT, reduced_motion="reduce")
    context.route("**/*", lambda route: route.abort())
    instance = context.new_page()
    instance.set_content("""
      <style>.creator-tab {width:160px;height:40px;line-height:40px;cursor:pointer;}</style>
      <script>window.clicked=[];</script>
    """)
    yield instance
    context.close()


def test_selects_in_viewport_tab_instead_of_first_offscreen_copy(page):
    page.evaluate("""() => {
      document.body.insertAdjacentHTML('beforeend', `
        <div class="creator-tab" style="position:fixed;left:-10000px" onclick="clicked.push('offscreen')">上传图文</div>
        <div class="creator-tab" style="display:none" onclick="clicked.push('hidden')">上传图文</div>
        <div class="creator-tab" onclick="clicked.push('wrong-tab')">上传图文教程</div>
        <div class="creator-tab" onclick="clicked.push('image')"><span>上传图文</span></div>`);
    }""")
    XiaohongshuConnector()._select_image_tab(page, timeout_ms=1500)
    assert page.evaluate("clicked") == ["image"]


def test_scrolls_legitimate_tab_into_view_before_click(page):
    page.evaluate("""() => {
      document.body.insertAdjacentHTML('beforeend', `
        <div style="height:1600px"></div>
        <div class="creator-tab" onclick="clicked.push('scrolled')">上传图文</div>`);
    }""")
    XiaohongshuConnector()._select_image_tab(page, timeout_ms=1500)
    assert page.evaluate("clicked") == ["scrolled"]


@pytest.mark.parametrize("kind", ["offscreen", "covered", "disabled"])
def test_unusable_tabs_fail_without_dispatching_a_click(page, kind):
    style = "position:fixed;left:-10000px" if kind == "offscreen" else ""
    disabled = 'aria-disabled="true"' if kind == "disabled" else ""
    page.evaluate("""({style,disabled,covered}) => {
      document.body.insertAdjacentHTML('beforeend', `
        <div class="creator-tab" style="${style}" ${disabled} onclick="clicked.push('tab')">上传图文</div>
        <button onclick="clicked.push('publish')">发布</button>
        ${covered ? '<div style="position:fixed;inset:0;background:white;z-index:999"></div>' : ''}`);
    }""", {"style": style, "disabled": disabled, "covered": kind == "covered"})
    with pytest.raises(ValueError, match="无法点击.*上传图文"):
        XiaohongshuConnector()._select_image_tab(page, timeout_ms=450)
    assert page.evaluate("clicked") == []


def test_waits_for_tab_mounted_after_navigation(page):
    page.evaluate("""() => setTimeout(() => document.body.insertAdjacentHTML('beforeend',
      `<div class="creator-tab" onclick="clicked.push('late')">上传图文</div>`), 150)""")
    XiaohongshuConnector()._select_image_tab(page, timeout_ms=1500)
    assert page.evaluate("clicked") == ["late"]
