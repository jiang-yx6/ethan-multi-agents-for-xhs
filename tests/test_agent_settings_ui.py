"""真实浏览器验证 Agent 设置；所有请求转发到临时 TestClient，不启动用户后端。"""
import mimetypes
import json
import time
import uuid
from threading import Event
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("playwright.sync_api")
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from xhs_agent.config import AgentConfig
from xhs_agent.llm import LLMConfig
from xhs_agent.agents.creation import MockCreationAgent
from xhs_agent.agents.supervisor import MockSupervisorAgent
from xhs_agent.service import TaskService
from xhs_agent.web import create_app


def route_app(route, client, dist):
    parsed = urlsplit(route.request.url)
    if parsed.hostname != "localhost":
        route.abort()
        return
    if parsed.path.startswith("/api/"):
        if parsed.path.endswith("/events"):
            # TestClient 会缓冲整个 SSE 响应；改发一帧实际作业快照，避免阻塞浏览器路由。
            snapshot = client.get(parsed.path.removesuffix("/events")).json()
            route.fulfill(status=200, body="data: " + json.dumps(snapshot) + "\n\n", content_type="text/event-stream")
            return
        response = client.request(route.request.method, parsed.path + ("?" + parsed.query if parsed.query else ""),
                                  content=route.request.post_data_buffer,
                                  headers={"content-type": route.request.headers.get("content-type", "application/json")})
        route.fulfill(status=response.status_code, body=response.content,
                      headers={"content-type": response.headers.get("content-type", "application/json")})
        return
    path = (dist / parsed.path.lstrip("/")).resolve() if parsed.path != "/" else dist / "index.html"
    if not path.is_relative_to(dist.resolve()) or not path.is_file():
        route.fulfill(status=404, body="Not found")
        return
    route.fulfill(path=str(path), content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")


def test_candidates_require_explicit_selection_and_can_regenerate_in_browser(tmp_path):
    root = Path(__file__).resolve().parents[1]
    dist = root / "web" / "dist"
    if not (dist / "index.html").exists():
        pytest.skip("请先构建前端")
    app = create_app(tmp_path, config=AgentConfig(supervisor_backend="mock", ideation_backend="mock"))
    with TestClient(app, base_url="http://localhost:8123") as client, sync_playwright() as playwright:
        from test_team_web import wait
        job = wait(client, client.post("/api/tasks", json={"request_id": str(uuid.uuid4()),
                   "request": {"goal": "上海冬天的日常感受", "needs_research": False}}))
        url = f"/api/tasks/{job['task_id']}"
        first = client.get(url).json()["workflow"]["interrupts"][0]
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100}, reduced_motion="reduce")
            page.set_default_timeout(12000)
            submitted, errors = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def route_request(route):
                if urlsplit(route.request.url).path == url + "/select_idea":
                    payload = json.loads(route.request.post_data)
                    submitted.append(payload)
                    if len(submitted) == 1:
                        response = client.post(url + "/select_idea", json={**payload, "interrupt_id": first["id"]})
                        assert response.status_code == 409
                        route.fulfill(status=409, body=response.content, content_type="application/json")
                        return
                route_app(route, client, dist)

            page.route("**/*", route_request)
            page.goto(f"http://localhost:8123/#/workspace?task={job['task_id']}", wait_until="networkidle")
            panel = page.get_by_role("region", name="人工选择选题")
            expect(panel.get_by_role("heading", name="先选一题，再继续创作")).to_be_in_viewport()
            expect(panel.get_by_role("radio")).to_have_count(3)
            confirm = panel.get_by_role("button", name="确认选题并继续")
            expect(confirm).to_be_disabled()
            expect(page.get_by_label("给父 Agent 发消息", exact=True)).to_be_disabled()
            expect(page.get_by_label("步骤结果与进度", exact=True)).to_contain_text("等待你确认选题")
            panel.get_by_label("想换个方向？", exact=True).fill("更贴近刚到上海的日常体验")
            panel.get_by_role("button", name="按要求重新生成").click()
            expect(panel.get_by_label("想换个方向？", exact=True)).to_have_value("")
            expect(panel.get_by_role("button", name="确认选题并继续")).to_be_disabled()
            # 刷新后仍停在新一批候选上，尚未生成草稿。
            page.reload(wait_until="networkidle")
            current = client.get(url).json()
            assert current["task"]["current_version"] == 0
            second = current["workflow"]["interrupts"][0]
            assert second["id"] != first["id"]
            panel.get_by_role("radio").nth(1).check()
            output = root / "data" / "ui-review"
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / "idea-selection-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(output / "idea-selection-mobile.png"), full_page=True)
            confirm.click()
            expect(panel.get_by_role("radio").nth(1)).to_be_checked()
            expect(confirm).to_be_enabled()
            confirm.click()
            expect(page.locator(".current-delivery")).to_contain_text("草稿 v1")
            assert client.get(url).json()["task"]["topic"] == current["ideas"][1]["title"]
            assert len(submitted) == 2 and submitted[-1]["idea_index"] == 1
            assert submitted[-1]["ideas_artifact_id"] == second["value"]["ideas"]["artifact_id"]
            assert submitted[-1]["interrupt_id"] == second["id"]
            assert errors == []
        finally:
            browser.close()


def test_agent_model_form_copy_and_responsive_layout(tmp_path):
    root = Path(__file__).resolve().parents[1]
    dist = root / "web" / "dist"
    if not (dist / "index.html").exists():
        pytest.skip("请先在 web 目录执行 npm run build")
    startup_path = tmp_path / "startup-models.json"
    startup_path.write_text(json.dumps({"supervisor": {"provider": "deepseek", "model": "startup-model", "max_tokens": 8192}}), encoding="utf-8")
    original_file = startup_path.read_bytes()
    app = create_app(tmp_path, config=AgentConfig(ideation_backend="mock", llm_config_path=str(startup_path),
                     supervisor_llm=LLMConfig(provider="deepseek", model="startup-model", max_tokens=8192,
                                              api_key="startup-ui-secret")))
    with TestClient(app, base_url="http://localhost:8123") as client, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100}, reduced_motion="reduce")
            page.set_default_timeout(12000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def route_request(route):
                route_app(route, client, dist)

            page.route("**/*", route_request)
            page.goto("http://localhost:8123/#/settings", wait_until="networkidle")
            expect(page.get_by_role("tab")).to_have_count(5)
            expect(page.locator("#agent-model")).to_be_visible()
            expect(page.locator("#agent-model")).to_be_disabled()
            expect(page.get_by_label("配置来源与优先级", exact=True)).to_contain_text("当前生效：跟随启动配置")
            expect(page.get_by_label("配置来源与优先级", exact=True)).to_contain_text(str(startup_path))
            expect(page.locator("#agent-max-tokens")).to_have_value("8192")
            page.locator("#agent-config-source").select_option("web")
            page.locator("#agent-backend").select_option("llm")
            page.locator("#agent-provider").fill("custom-vendor")
            page.locator("#agent-protocol").select_option("anthropic")
            page.locator("#agent-base-url").fill("https://gateway.example/anthropic")
            page.locator("#agent-model").fill("one-configured-model")
            page.locator("#agent-max-tokens").fill("16384")
            page.locator("#agent-timeout").fill("240")
            page.locator("#agent-api-key").fill("browser-test-secret")
            page.get_by_role("button", name="保存 Agent 配置", exact=True).click()
            expect(page.locator("#agent-api-key")).to_have_value("")
            expect(page.locator("#agent-tab-supervisor")).to_contain_text("已配置")
            expect(page.get_by_label("配置来源与优先级", exact=True)).to_contain_text("当前生效：网页独立配置")
            for role in ("ideation", "research", "creation", "review"):
                page.locator(f"#agent-tab-{role}").click()
                page.get_by_label("选择已配置模型", exact=True).select_option("supervisor")
                page.get_by_role("button", name="一键应用", exact=True).click()
                expect(page.locator("#agent-model")).to_have_value("one-configured-model")
                expect(page.locator("#agent-max-tokens")).to_have_value("16384")
                expect(page.locator("#agent-timeout")).to_have_value("240")
                expect(page.locator("#agent-config-source")).to_have_value("web")
                expect(page.locator("#agent-tab-" + role)).to_contain_text("已配置")
                expect(page.locator("#agent-api-key")).to_have_value("")
                saved = client.get(f"/api/settings/{role}")
                assert saved.json()["api_key_configured"] and "browser-test-secret" not in saved.text
            page.locator("#agent-tab-creation").click()
            page.locator("#agent-model").fill("creation-specific-model")
            page.get_by_role("button", name="保存 Agent 配置", exact=True).click()
            expect(page.get_by_role("button", name="保存 Agent 配置", exact=True)).to_be_enabled()
            page.locator("#agent-tab-supervisor").click()
            expect(page.locator("#agent-model")).to_have_value("one-configured-model")
            page.locator("#agent-tab-creation").click()
            expect(page.locator("#agent-model")).to_have_value("creation-specific-model")
            assert client.get("/api/settings/supervisor").json()["model"] == "one-configured-model"
            output = root / "data" / "ui-review"
            output.mkdir(parents=True, exist_ok=True)
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(output / "agent-settings-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(output / "agent-settings-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.locator("#agent-config-source").select_option("startup")
            expect(page.locator("#agent-max-tokens")).to_have_value("4096")
            expect(page.locator("#agent-model")).to_be_disabled()
            page.get_by_role("button", name="保存配置来源", exact=True).click()
            expect(page.get_by_label("配置来源与优先级", exact=True)).to_contain_text("当前生效：跟随启动配置")
            assert client.get("/api/settings/creation").json()["config_source"] == "startup"
            assert client.get("/api/settings/supervisor").json()["max_tokens"] == 16384
            assert startup_path.read_bytes() == original_file
            assert errors == []
        finally:
            browser.close()


def test_parent_question_can_be_answered_in_browser_and_rejects_stale_pause(tmp_path):
    root = Path(__file__).resolve().parents[1]
    dist = root / "web" / "dist"
    if not (dist / "index.html").exists():
        pytest.skip("请先在 web 目录执行 npm run build")
    release = Event()

    class PausedCreation(MockCreationAgent):
        def create(self, *args, **kwargs):
            if not release.wait(20):
                raise RuntimeError("测试未释放创作任务")
            return super().create(*args, **kwargs)

    class QuestionSupervisor(MockSupervisorAgent):
        def decide(self, context):
            if not context.user_messages:
                return {"action": "ask_user", "reason": "需要真实素材", "question": "你在上海有哪些实际生活感受？",
                        "missing_information": ["真实生活素材"]}
            return super().decide(context)

    app = create_app(tmp_path, config=AgentConfig(supervisor_backend="mock", ideation_backend="mock"),
                     service_factory=lambda config, event: TaskService(tmp_path, config=config, on_event=event,
                                                                       creation_agent=PausedCreation(), supervisor_agent=QuestionSupervisor()))
    with TestClient(app, base_url="http://localhost:8123") as client, sync_playwright() as playwright:
        response = client.post("/api/tasks", json={"request_id": str(uuid.uuid4()),
                               "request": {"goal": "整理上海冬天的生活体验", "topic": "上海冬天", "needs_research": False}})
        assert response.status_code == 202
        job_id, task_id = response.json()["job_id"], response.json()["task_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()["job"]
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(.02)
        detail = client.get(f"/api/tasks/{task_id}").json()
        assert detail["task"]["status"] == "waiting_input"
        pause = detail["workflow"]["interrupts"][0]
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100}, reduced_motion="reduce")
            page.set_default_timeout(12000)
            submitted, errors = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def route_request(route):
                if urlsplit(route.request.url).path == f"/api/tasks/{task_id}/answer":
                    payload = json.loads(route.request.post_data)
                    submitted.append(payload)
                    if len(submitted) == 1:
                        # 模拟另一页面已推进检查点，后端拒绝过期回复；表单须保留输入。
                        response = client.post(f"/api/tasks/{task_id}/answer", json={**payload, "interrupt_id": "stale"})
                        assert response.status_code == 409
                        route.fulfill(status=response.status_code, body=response.content, content_type="application/json")
                        return
                route_app(route, client, dist)

            page.route("**/*", route_request)
            page.goto(f"http://localhost:8123/#/workspace?task={task_id}", wait_until="networkidle")
            expect(page.get_by_role("heading", name="与父 Agent 对话", exact=True)).to_be_visible()
            expect(page.get_by_role("log", name="对话记录")).to_contain_text(pause["value"]["question"])
            expect(page.get_by_label("步骤结果与进度", exact=True)).to_contain_text("确认内容方向")
            expect(page.get_by_text("检查模型配置后，可继续恢复这次任务。", exact=True)).to_have_count(0)
            page.reload(wait_until="networkidle")
            submit = page.get_by_role("button", name="发送消息", exact=True)
            expect(submit).to_be_disabled()
            page.get_by_label("给父 Agent 发消息", exact=True).fill("   ")
            expect(submit).to_be_disabled()
            reply = "采用第一个方向，写给刚来上海生活的人。我在江边散步时感到风大，想记录实际感受。"
            page.get_by_label("给父 Agent 发消息", exact=True).fill(reply)
            expect(submit).to_be_enabled()
            # 未提交的回复也参与现有离页保护。
            page.once("dialog", lambda dialog: dialog.dismiss())
            page.get_by_role("button", name="技能与模型", exact=True).click()
            expect(page.get_by_label("给父 Agent 发消息", exact=True)).to_have_value(reply)
            output = root / "data" / "ui-review"
            output.mkdir(parents=True, exist_ok=True)
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(output / "conversation-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(output / "conversation-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            submit.click()
            expect(page.get_by_label("给父 Agent 发消息", exact=True)).to_have_value(reply)
            expect(submit).to_be_enabled()
            submit.click()
            expect(page.get_by_label("给父 Agent 发消息", exact=True)).to_have_value("")
            expect(page.locator(".team-state")).to_have_text("执行中")
            running_step = page.locator(".collaboration-step").filter(has=page.get_by_text("创作 Agent", exact=True))
            expect(running_step.locator(".step-status")).to_have_text("执行中")
            page.set_viewport_size({"width": 1440, "height": 1100})
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(output / "conversation-running-desktop.png"), full_page=True)
            release.set()
            page.get_by_role("button", name="打开草稿与审核", exact=True).click()
            expect(page.get_by_label("正文", exact=True)).to_be_visible()
            # 草稿落库后即可查看；须等后台审核和检查点完成后再断言最终状态。
            expect(page.locator(".team-state")).to_have_text("进度已保存")
            current = client.get(f"/api/tasks/{task_id}").json()
            assert current["task"]["status"] == "pending_review"
            assert current["task"]["draft"]["approved"] is False
            assert len(submitted) == 2
            assert submitted[0]["interrupt_id"] == submitted[1]["interrupt_id"] == pause["id"]
            assert submitted[0]["expected_version"] == 0 and submitted[1]["answer"] == reply
            assert submitted[0]["request_id"] != submitted[1]["request_id"]
            collaboration = client.get(f"/api/tasks/{task_id}/collaboration").json()
            assert reply in collaboration["user_messages"][-1]
            page.get_by_role("button", name="关闭弹窗", exact=True).click()
            feedback = "保留真实体验，补充公共交通的建议，不编造具体票价。"
            page.get_by_label("给父 Agent 发消息", exact=True).fill(feedback)
            submit.click()
            expect(page.locator(".current-delivery")).to_contain_text("草稿 v2")
            page.reload(wait_until="networkidle")
            log = page.get_by_role("log", name="对话记录")
            expect(log).to_contain_text(reply)
            expect(log).to_contain_text(feedback)
            expect(page.get_by_label("给父 Agent 发消息", exact=True)).to_have_value("")
            page.locator(".all-artifacts > summary").click()
            artifact = page.locator(".all-artifacts .artifact-card").filter(has_text="内容草稿").first
            artifact.locator("summary").click()
            expect(artifact.locator(".artifact-content")).to_contain_text("阅读此版本")
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(output / "conversation-results-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(output / "conversation-results-mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert errors == []
        finally:
            release.set()
            browser.close()
