"""对隔离的 --mock-ideation Web 服务执行真实浏览器冒烟检查。"""
import argparse
import base64
import json
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8011")
    args = parser.parse_args()
    health = httpx.get(args.url + "/api/health", trust_env=False).json()
    if health.get("ideation_backend") != "mock":
        raise RuntimeError("请使用隔离 data-dir 和显式 --mock-ideation 启动测试服务")
    output = Path(__file__).resolve().parents[1] / "data" / "web-review"
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
        page.set_default_timeout(12000)
        page.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url, wait_until="domcontentloaded")
        page.get_by_role("button", name="新建创作", exact=True).click()
        page.locator("#new-goal").fill("浏览器验收：周末慢行路线")
        page.locator("#new-materials").fill("周末走过江边步道，希望记录真实的出行体验。")
        page.locator("#new-task-form button[type=submit], #new-task-form button:not([type])").last.click()
        page.locator(".idea-card").first.wait_for()
        assert page.locator(".idea-card").count() == 3
        expect(page.locator(".workflow-notice")).to_contain_text("等待你选择选题")
        # 刷新读取同一检查点，不重新生成选题或自动恢复人工暂停。
        page.reload(wait_until="domcontentloaded")
        expect(page.locator(".idea-card")).to_have_count(3)
        expect(page.locator(".workflow-notice")).to_contain_text("等待你选择选题")
        page.get_by_role("button", name="采用这个方向", exact=False).click()
        expect(page.locator(".workflow-notice")).to_contain_text("等待你审核当前草稿")
        page.locator("#draft-title").fill("沿着江边，慢慢过周末")
        page.locator("#draft-body").fill("这次分享自己走过的江边步道。\n\n出门前确认交通和天气，穿舒适的鞋，给自己留出慢慢走的时间。")
        page.locator("#draft-tags").fill("周末散步, 城市生活")
        assert page.get_by_role("button", name="批准当前版本", exact=True).is_disabled()
        page.get_by_role("button", name="保存修改", exact=True).click()
        page.get_by_role("button", name="批准当前版本", exact=True).wait_for(state="visible")
        fixture = page.evaluate("""() => {
          const canvas = document.createElement('canvas'); canvas.width=640; canvas.height=800;
          const ctx=canvas.getContext('2d'); ctx.fillStyle='#e8eadf'; ctx.fillRect(0,0,640,800);
          ctx.strokeStyle='#c4d1bc'; ctx.lineWidth=145; ctx.beginPath(); ctx.moveTo(420,850);
          ctx.bezierCurveTo(110,550,720,560,370,260); ctx.stroke();
          ctx.fillStyle='#354e3a';ctx.font='54px Georgia';ctx.fillText('SLOW',46,105);ctx.fillText('WEEKEND',46,174);
          ctx.font='24px Georgia';ctx.fillText('WALK & FEEL',46,730);
          ctx.fillStyle='#bd5b40';ctx.beginPath();ctx.arc(373,480,10,0,Math.PI*2);ctx.fill();
          return canvas.toDataURL('image/png').split(',')[1];
        }""")
        png = base64.b64decode(fixture)
        page.locator('input[type="file"]').set_input_files({"name": "walk.png", "mimeType": "image/png", "buffer": png})
        page.locator(".media-gallery img").wait_for()
        page.get_by_role("button", name="保存修改", exact=True).click()
        page.get_by_role("button", name="批准当前版本", exact=True).click()
        page.get_by_role("button", name="准备发布", exact=False).wait_for()
        expect(page.locator(".workflow-notice")).to_contain_text("等待你确认发布")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("#toast").evaluate("el=>el.classList.remove('visible')")
        page.screenshot(path=str(output / "desktop.png"), full_page=True)
        page.get_by_role("button", name="准备发布", exact=False).click()
        page.locator("#publish-form").wait_for()
        assert page.get_by_role("button", name="确认真实发布", exact=True).is_disabled()
        page.get_by_role("button", name="关闭弹窗", exact=True).click()
        # 审核 -> 发布门禁不增加版本，但 interrupt ID 已改变；编辑器应绑定新的暂停。
        page.locator("#draft-tags").fill("周末散步, 城市生活, 慢生活")
        expect(page.get_by_role("button", name="保存修改", exact=True)).to_be_enabled()
        page.get_by_role("button", name="保存修改", exact=True).click()
        expect(page.locator(".workflow-notice")).to_contain_text("等待你审核当前草稿")
        page.get_by_role("button", name="批准当前版本", exact=True).click()
        expect(page.locator(".workflow-notice")).to_contain_text("等待你确认发布")
        page.get_by_role("button", name="版本记录", exact=True).click()
        assert page.locator(".history-item").count() >= 3
        page.get_by_role("button", name="查看该版本", exact=False).first.click()
        assert "只读" in page.locator("#modal-title").inner_text()
        page.keyboard.press("Escape")
        page.get_by_role("button", name="正文编辑", exact=True).click()
        page.locator("#draft-title").fill("这段修改尚未保存")
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.get_by_role("button", name="创作画像", exact=True).click()
        assert page.locator("#draft-title").input_value() == "这段修改尚未保存"
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="创作画像", exact=True).click()
        page.locator("#profile-name").fill("浏览器验收画像")
        page.get_by_role("button", name="保存创作画像", exact=True).click()
        page.get_by_role("button", name="技能与模型", exact=True).click()
        page.get_by_role("button", name="资料", exact=True).click()
        expect(page.locator(".skill-row")).to_have_count(1)
        page.locator("#credential-source").select_option("environment")
        page.locator("#provider").fill("openai")
        expect(page.locator("#key-env")).to_have_value("OPENAI_API_KEY")
        page.locator("#provider").fill("deepseek")
        expect(page.locator("#key-env")).to_have_value("DEEPSEEK_API_KEY")
        page.locator("#model-id").fill("ui-smoke-model")
        page.locator("#credential-source").select_option("api_key")
        expect(page.locator("#model-api-key")).to_have_attribute("type", "password")
        page.locator("#model-api-key").fill("ui-only-placeholder-key")
        page.get_by_role("button", name="保存模型配置", exact=True).click()
        expect(page.locator("#credential-status")).to_contain_text("已保存密钥")
        expect(page.locator("#model-api-key")).to_have_value("")
        page.reload(wait_until="domcontentloaded")
        page.locator("#model-id").wait_for()
        assert page.locator("#model-id").input_value() == "ui-smoke-model"
        expect(page.locator("#model-api-key")).to_have_value("")
        expect(page.locator("#credential-status")).to_contain_text("已保存密钥")
        # 留空保存保留密钥，再验证明确清除。
        page.get_by_role("button", name="保存模型配置", exact=True).click()
        expect(page.locator("#credential-status")).to_contain_text("已保存密钥")
        page.get_by_role("checkbox", name="清除当前接口已保存的密钥").check()
        page.get_by_role("button", name="保存模型配置", exact=True).click()
        expect(page.locator("#credential-status")).to_contain_text("尚未保存")
        page.screenshot(path=str(output / "settings.png"), full_page=True)
        page.get_by_role("button", name="创作工作台", exact=True).click()
        page.locator("#draft-title").wait_for()
        expect(page.locator("#draft-title")).to_have_value("沿着江边，慢慢过周末")
        page.get_by_role("textbox", name="搜索创作任务").fill("沿着江边")
        page.locator(".task-card").first.wait_for()
        for width, height in [(1024, 900), (768, 900), (390, 844)]:
            page.set_viewport_size({"width": width, "height": height})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
        page.locator("#toast").evaluate("el=>el.classList.remove('visible')")
        page.screenshot(path=str(output / "mobile.png"), full_page=True)
        assert not errors, errors
        browser.close()
    print(json.dumps({"result": "passed", "javascript_errors": errors,
                      "checks": "真实 API 创建/选择/编辑/上传/审核、历史、未保存提醒、画像、配置持久化、响应式布局",
                      "screenshots": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
