"""真实 HTTP 工作流的本地测试；平台提交和登录使用 FakeConnector。"""
import base64
import threading
import time
import uuid

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from xhs_agent.agents.ideation import MockIdeationAgent
from xhs_agent.config import WorkflowConfig
from xhs_agent.models import ContentRequest
from xhs_agent.platforms import AccountIdentity, PublishReceipt
from xhs_agent.platforms.xiaohongshu import XiaohongshuConnector
from xhs_agent.service import TaskService
from xhs_agent.web import create_app
from xhs_agent.web.app import credential_scope, fingerprint
from xhs_agent.web.repository import WebRepository


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRncAAAAASUVORK5CYII=")
NOTE_URL = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"


class FakeConnector(XiaohongshuConnector):
    def __init__(self):
        self.calls = 0
        self.logins = 0
        self.result_state = "unknown"

    def login(self, profile_dir, *, timeout=180):
        self.logins += 1
        return AccountIdentity(state="authenticated", user_id="web-user", nickname="测试账号")

    def whoami(self, profile_dir):
        return AccountIdentity(state="authenticated", user_id="web-user", nickname="测试账号")

    def submit(self, request, profile_dir):
        self.calls += 1
        return PublishReceipt(state=self.result_state, message="测试平台回执")


@pytest.fixture
def web(tmp_path):
    root = tmp_path / "data"
    connector = FakeConnector()

    def factory(config, on_event):
        return TaskService(root, config=config, on_event=on_event, connector_factory=lambda name: connector)

    app = create_app(root, config=WorkflowConfig(ideation_backend="mock"), service_factory=factory)
    with TestClient(app) as client:
        yield client, app, root, connector


def wait(client, response):
    assert response.status_code == 202, response.text
    identifier = response.json()["job_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + identifier).json()["job"]
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(.02)
    pytest.fail("作业未在测试期限内完成")


def new_task(client):
    payload = {"request_id": str(uuid.uuid4()), "request": {"goal": "周末路线", "use_trends": False, "needs_research": False}}
    response = client.post("/api/tasks", json=payload)
    job = wait(client, response)
    assert job["status"] == "succeeded", job
    return job["task_id"], payload


def action(client, task_id, kind, **extra):
    detail = client.get(f"/api/tasks/{task_id}").json()
    task = detail["task"]
    pause = ({"interrupt_id": detail["workflow"]["interrupts"][0]["id"] if detail["workflow"]["interrupts"] else "unavailable"}
             if kind in {"select", "drafts", "changes", "approve", "cancel", "publish"} else {})
    return client.post(f"/api/tasks/{task_id}/{kind}", json={"request_id": str(uuid.uuid4()),
                        "expected_version": task["current_version"], **pause, **extra})


def selected_task(client):
    task_id, _ = new_task(client)
    assert wait(client, action(client, task_id, "select", index=0))["status"] == "succeeded"
    return task_id


def test_full_workflow_versions_prevent_stale_approval_and_export(web):
    client, _, _, _ = web
    task_id = selected_task(client)
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert "approve" in detail["allowed_actions"]
    task = detail["task"]
    saved = wait(client, action(client, task_id, "drafts", title="周末散步", body="真实素材整理成的正文。",
                               images=[], tags=["周末"], content_type="分享"))
    assert saved["status"] == "succeeded"
    conflict = client.post(f"/api/tasks/{task_id}/approve", json={"request_id": str(uuid.uuid4()),
                           "expected_version": task["current_version"],
                           "interrupt_id": detail["workflow"]["interrupts"][0]["id"]})
    assert conflict.status_code == 409
    assert wait(client, action(client, task_id, "approve"))["status"] == "succeeded"
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["task"]["status"] == "approved" and detail["task"]["current_version"] == 2
    assert client.get(f"/api/tasks/{task_id}/drafts/1").json()["title"] != "周末散步"
    assert "真实素材整理成的正文。" in client.get(f"/api/tasks/{task_id}/export").text
    assert wait(client, action(client, task_id, "drafts", title="新的标题", body="新版本", images=[]))["status"] == "succeeded"
    assert not client.get(f"/api/tasks/{task_id}").json()["task"]["draft"]["approved"]
    assert client.get("/api/tasks", params={"query": "新的标题"}).json()["total"] == 1


def test_create_idempotency_search_and_profile_snapshots(web):
    client, _, _, _ = web
    task_id, payload = new_task(client)
    same = client.post("/api/tasks", json=payload)
    assert same.json()["task_id"] == task_id
    assert client.get("/api/tasks").json()["total"] == 1
    payload["request"]["goal"] = "不同的请求"
    assert client.post("/api/tasks", json=payload).status_code == 409
    assert client.get("/api/tasks?query=路线").json()["total"] == 1
    assert client.get("/api/tasks?query=%25").json()["total"] == 0
    profile = client.get("/api/profiles").json()[0]["profile"]
    old = profile["name"]
    profile["name"] = "修改后的画像"
    assert client.put("/api/profiles/default", json={"profile": profile}).status_code == 200
    assert client.get(f"/api/tasks/{task_id}").json()["task"]["profile"]["name"] == old


def test_settings_are_persistent_without_returning_secrets(web, monkeypatch):
    client, app, _, _ = web
    monkeypatch.setenv("WEB_TEST_MODEL_KEY", "private-test-value")
    response = client.put("/api/settings/ideation", json={"backend": "llm", "provider": "deepseek",
                           "model": "test-model", "api_key_env": "WEB_TEST_MODEL_KEY"})
    assert response.status_code == 200
    assert response.json()["credential_configured"]
    assert "private-test-value" not in response.text
    assert app.state.repository.setting()["model"] == "test-model"
    bad = client.put("/api/settings/ideation", json={"provider": "deepseek", "model": "test-model",
                     "api_key": "private-test-value\ninvalid"})
    assert bad.status_code == 422 and "private-test-value" not in bad.text
    assert client.get("/api/skills?role=ideation").json()
    assert client.get("/api/skills?role=publishing").json() == []


def test_direct_key_is_encrypted_kept_replaced_and_cleared(web, monkeypatch):
    client, app, root, _ = web
    secret = "web-direct-test-secret"
    payload = {"backend": "llm", "provider": "deepseek", "model": "test-model",
               "credential_source": "api_key", "api_key": secret}
    response = client.put("/api/settings/ideation", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["api_key_configured"] and response.json()["credential_configured"]
    assert response.headers["cache-control"] == "no-store"
    assert "api_key" not in response.json() and secret not in response.text
    metadata = app.state.repository.setting()
    assert "api_key" not in metadata and "clear_api_key" not in metadata
    scope = credential_scope(metadata)
    assert app.state.repository.api_key(scope).get_secret_value() == secret
    with app.state.repository.connection() as conn:
        encrypted = bytes(conn.execute("SELECT encrypted FROM credentials WHERE scope=?", (scope,)).fetchone()[0])
    assert secret.encode() not in encrypted
    assert secret.encode() not in (root / "web.db").read_bytes()
    assert not (root / "web.db-wal").exists() or secret.encode() not in (root / "web.db-wal").read_bytes()
    assert secret not in client.get("/api/settings/ideation").text
    # 回传公开配置不包含密钥，应保留；显式空输入也保留。
    keep = {key: value for key, value in metadata.items()}
    assert client.put("/api/settings/ideation", json={**keep, "api_key": ""}).status_code == 200
    assert WebRepository(root).api_key(scope).get_secret_value() == secret
    replacement = "web-replacement-test-secret"
    assert client.put("/api/settings/ideation", json={**keep, "api_key": replacement}).status_code == 200
    assert app.state.repository.api_key(scope).get_secret_value() == replacement
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-fallback-to-environment")
    cleared = client.put("/api/settings/ideation", json={**keep, "clear_api_key": True})
    assert cleared.status_code == 200 and not cleared.json()["api_key_configured"]
    assert not cleared.json()["credential_configured"] and app.state.repository.api_key(scope) is None


@pytest.mark.parametrize("changed", [{"provider": "anthropic"}, {"base_url": "https://different-gateway.example/v1"}])
def test_direct_key_is_bound_to_provider_and_endpoint(web, changed):
    client, _, _, _ = web
    payload = {"backend": "llm", "provider": "deepseek", "model": "test-model", "credential_source": "api_key"}
    assert client.put("/api/settings/ideation", json={**payload, "api_key": "endpoint-specific-test-key"}).status_code == 200
    switched = client.put("/api/settings/ideation", json={**payload, **changed})
    assert switched.status_code == 200 and not switched.json()["api_key_configured"]
    # 同一接口改模型可以继续使用原凭据，其他接口的凭据不会被删除。
    restored = client.put("/api/settings/ideation", json={**payload, "model": "another-model"})
    assert restored.json()["api_key_configured"]


def test_direct_key_reaches_model_config_and_survives_server_restart(tmp_path, monkeypatch):
    from xhs_agent.agents.ideation import MockIdeationAgent

    root, seen = tmp_path / "credential-restart", []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-key-must-not-override")

    def factory(config, on_event):
        if config.ideation_llm:
            seen.append(config.ideation_llm.api_key.get_secret_value())
        return TaskService(root, config=config, ideation_agent=MockIdeationAgent(), on_event=on_event)

    def app():
        return create_app(root, config=WorkflowConfig(ideation_backend="mock"), service_factory=factory)

    payload = {"backend": "llm", "provider": "deepseek", "model": "test-model", "credential_source": "api_key"}
    with TestClient(app()) as client:
        assert client.put("/api/settings/ideation", json={**payload, "api_key": "restart-test-key"}).status_code == 200
        new_task(client)
    with TestClient(app()) as client:
        assert client.get("/api/settings/ideation").json()["api_key_configured"]
        new_task(client)
        assert set(seen) == {"restart-test-key"}
        assert client.put("/api/settings/ideation", json={**payload, "clear_api_key": True}).status_code == 200
        new_task(client)
        assert seen[-1] == ""  # 直接配置模式清除后，不会改用环境中的另一个密钥。


def test_invalid_key_update_and_corrupted_ciphertext_do_not_expose_credentials(web):
    client, app, _, _ = web
    payload = {"backend": "llm", "provider": "deepseek", "model": "test-model", "credential_source": "api_key"}
    secret = "ciphertext-test-secret"
    assert client.put("/api/settings/ideation", json={**payload, "api_key": secret}).status_code == 200
    invalid = client.put("/api/settings/ideation", json={**payload, "api_key": secret, "clear_api_key": True})
    assert invalid.status_code == 422 and secret not in invalid.text
    scope = credential_scope(app.state.repository.setting())
    with app.state.repository.connection() as conn:
        conn.execute("UPDATE credentials SET encrypted=? WHERE scope=?", (b"corrupt-token", scope))
    broken = client.get("/api/health")
    assert broken.status_code == 422 and secret not in broken.text
    # 损坏凭据不阻止打开设置或直接替换。
    assert client.get("/api/settings/ideation").status_code == 200
    assert client.put("/api/settings/ideation", json={**payload, "api_key": "fixed-test-key"}).status_code == 200
    assert client.get("/api/health").status_code == 200


def test_media_and_uncertain_publish_do_not_repeat_submission(web):
    client, _, _, connector = web
    task_id = selected_task(client)
    assert client.post("/api/media", files={"file": ("wrong.png", b"not an image", "image/png")}).status_code == 422
    uploaded = client.post("/api/media", files={"file": ("../../photo.png", PNG, "image/png")}).json()
    assert client.get(uploaded["url"]).content == PNG
    assert wait(client, action(client, task_id, "drafts", title="真实路线", body="整理自己的散步记录。",
                images=[{"description": "实拍照片", "media_id": uploaded["media_id"]}]))["status"] == "succeeded"
    assert wait(client, action(client, task_id, "approve"))["status"] == "succeeded"
    assert client.post("/api/accounts", json={"account": {"platform": "xiaohongshu", "account_id": "main"}}).status_code == 200
    login = client.post("/api/accounts/xiaohongshu/main/login", json={"request_id": str(uuid.uuid4())})
    assert wait(client, login)["status"] == "succeeded"
    detail = client.get(f"/api/tasks/{task_id}").json()
    ref = {"platform": "xiaohongshu", "account_id": "main"}
    assert client.post(f"/api/tasks/{task_id}/preflight", json={"expected_version": detail["task"]["current_version"], "account": ref}).json()["valid"]
    connector.result_state = "failed"
    assert wait(client, action(client, task_id, "publish", account=ref, confirmed=True))["status"] == "succeeded"
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["task"]["status"] == "approved"
    assert detail["task"]["publish_result"]["status"] == "failed"
    connector.result_state = "unknown"
    response = action(client, task_id, "publish", account=ref, confirmed=True)
    assert wait(client, response)["status"] == "succeeded"
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["task"]["status"] == "unknown" and not detail["task"]["published"]
    assert action(client, task_id, "publish", account=ref, confirmed=True).status_code == 409
    assert connector.calls == 2
    assert wait(client, action(client, task_id, "confirm-published", note_url=NOTE_URL, confirmed=True))["status"] == "succeeded"
    assert client.get(f"/api/tasks/{task_id}").json()["task"]["published"]
    assert connector.calls == 2


def test_untrusted_origin_and_path_inputs_are_rejected(web):
    client, _, _, _ = web
    assert client.post("/api/accounts", json={"account": {"platform": "xiaohongshu", "account_id": "a"}},
                       headers={"Origin": "https://untrusted.example"}).status_code == 403
    task_id = selected_task(client)
    response = action(client, task_id, "drafts", title="标题", body="正文", images=[{"description": "非法", "source": "C:/secret.txt"}])
    assert response.status_code == 422
    assert client.get("/api/media/not-existing").status_code == 404


def test_sse_contains_durable_events(web):
    client, _, _, _ = web
    task_id, _ = new_task(client)
    job = client.get(f"/api/tasks/{task_id}").json()["job"]
    response = client.get(f"/api/jobs/{job['job_id']}/events")
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"succeeded"' in response.text and "data: " in response.text
    assert len(client.get(f"/api/jobs/{job['job_id']}").json()["events"]) >= 2


def test_duplicate_write_is_blocked_while_model_is_running(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class SlowAgent(MockIdeationAgent):
        def ideate(self, *args):
            entered.set()
            assert release.wait(5)
            return super().ideate(*args)

    root = tmp_path / "slow"
    def factory(config, on_event): return TaskService(root, ideation_agent=SlowAgent(), config=config)
    with TestClient(create_app(root, config=WorkflowConfig(ideation_backend="mock"), service_factory=factory)) as client:
        payload = {"request_id": str(uuid.uuid4()), "request": {"goal": "测试长任务"}}
        response = client.post("/api/tasks", json=payload)
        assert entered.wait(3)
        task_id = response.json()["task_id"]
        assert client.get(f"/api/tasks/{task_id}").json()["allowed_actions"] == []
        assert action(client, task_id, "cancel").status_code == 409
        assert client.post("/api/tasks", json=payload).json()["job_id"] == response.json()["job_id"]
        release.set()
        assert wait(client, response)["status"] == "succeeded"


def test_restart_restores_safe_approval_gate_without_replaying_job(tmp_path):
    root = tmp_path / "restart"
    service = TaskService(root, config=WorkflowConfig(ideation_backend="mock"))
    task = service.create_task(ContentRequest(goal="重启恢复"))
    service.select_idea(task.task_id, 0)
    service.approve(task.task_id)
    service.close()
    repo = WebRepository(root)
    job, _ = repo.enqueue(request_id=str(uuid.uuid4()), fingerprint="test", resource="task:"+task.task_id,
                          task_id=task.task_id, kind="approve", params={})
    repo.start(job["job_id"])
    with TestClient(create_app(root, config=WorkflowConfig(ideation_backend="mock"))) as client:
        detail = client.get(f"/api/tasks/{task.task_id}").json()
        assert detail["task"]["status"] == "approved"
        assert detail["workflow"]["status"] == "waiting_input"
        assert detail["workflow"]["interrupts"][0]["kind"] == "publish_gate"
        assert detail["job"]["status"] == "interrupted"
        assert "publish" in detail["allowed_actions"] and "retry" not in detail["allowed_actions"]
        assert any(event["kind"] == "reconciled" for event in detail["events"])
        assert client.get(f"/api/tasks/{task.task_id}").json()["workflow"]["checkpoint_id"] == detail["workflow"]["checkpoint_id"]


def test_workflow_exposes_real_gates_and_releases_job_between_pauses(web):
    client, _, _, _ = web
    task_id, _ = new_task(client)
    detail = client.get(f"/api/tasks/{task_id}").json()
    first = detail["workflow"]
    assert first["thread_id"] == task_id and first["checkpoint_id"]
    assert first["status"] == "waiting_input" and first["next_nodes"] == ["select_idea"]
    assert detail["job"]["status"] == "succeeded" and detail["allowed_actions"] == ["select"]
    assert first["interrupts"][0]["actions"] == ["select"]
    assert wait(client, action(client, task_id, "select", index=0))["status"] == "succeeded"
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["workflow"]["next_nodes"] == ["review"]
    assert detail["workflow"]["interrupts"][0]["id"] != first["interrupts"][0]["id"]
    assert detail["workflow"]["interrupts"][0]["value"]["version"] == 1
    assert "draft" not in detail["workflow"]["interrupts"][0]["value"]
    assert {event["node"] for event in detail["events"] if event["kind"] == "graph_step"} >= {"research", "create", "quality_review"}
    assert any(event["kind"] == "graph_paused" for event in detail["events"])
    assert client.get(f"/api/tasks/{task_id}/workflow").json()["workflow"] == detail["workflow"]
    stream = client.get(f"/api/jobs/{detail['job']['job_id']}/events").text
    assert '"workflow"' in stream and '"waiting_input"' in stream
    assert wait(client, action(client, task_id, "cancel"))["result"]["workflow"]["status"] == "completed"


def test_stale_interrupt_is_rejected_at_http_and_worker_with_same_version(web):
    client, app, root, _ = web
    task_id = selected_task(client)
    before = client.get(f"/api/tasks/{task_id}").json()
    old_id = before["workflow"]["interrupts"][0]["id"]
    assert wait(client, action(client, task_id, "approve"))["status"] == "succeeded"
    after = client.get(f"/api/tasks/{task_id}").json()
    assert before["task"]["current_version"] == after["task"]["current_version"]
    assert action(client, task_id, "cancel", interrupt_id=old_id).status_code == 409
    assert action(client, task_id, "approve").status_code == 409
    missing = client.post(f"/api/tasks/{task_id}/cancel", json={"request_id": str(uuid.uuid4()), "expected_version": 1})
    assert missing.status_code == 422
    params = {"request_id": str(uuid.uuid4()), "expected_version": 1, "interrupt_id": old_id}
    job = app.state.runner.submit(request_id=params["request_id"], fingerprint="stale-worker", resource="task:"+task_id,
                                  task_id=task_id, kind="cancel", params=params)
    deadline = time.monotonic() + 5
    while app.state.repository.job(job["job_id"])["status"] in {"queued", "running"}:
        assert time.monotonic() < deadline
        time.sleep(.02)
    assert app.state.repository.job(job["job_id"])["status"] == "failed"
    service = TaskService(root, config=WorkflowConfig(ideation_backend="mock"))
    try:
        assert service.status(task_id).status == "approved"
        assert service.workflow(task_id).interrupts[0].id == after["workflow"]["interrupts"][0]["id"]
    finally:
        service.close()


def test_business_status_cannot_override_actual_graph_gate(web):
    client, _, root, _ = web
    task_id = selected_task(client)
    service = TaskService(root, config=WorkflowConfig(ideation_backend="mock"))
    service.store.update_status(task_id, "approved")
    service.close()
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["workflow"]["status"] == "inconsistent"
    assert detail["allowed_actions"] == ["reconcile"]
    assert action(client, task_id, "cancel").status_code == 409
    assert wait(client, action(client, task_id, "reconcile"))["status"] == "succeeded"
    restored = client.get(f"/api/tasks/{task_id}").json()
    assert restored["task"]["status"] == "pending_review"
    assert restored["workflow"]["status"] == "waiting_input" and "approve" in restored["allowed_actions"]
    assert restored["workflow"]["checkpoint_id"] == detail["workflow"]["checkpoint_id"]


def test_restart_of_unexecuted_job_needs_manual_retry(tmp_path):
    root = tmp_path / "unstarted"
    service = TaskService(root, config=WorkflowConfig(ideation_backend="mock"))
    task = service.register_task(ContentRequest(goal="执行前中断"))
    service.close()
    repo = WebRepository(root)
    repo.enqueue(request_id=str(uuid.uuid4()), fingerprint="queued-create", resource="task:"+task.task_id,
                 task_id=task.task_id, kind="create", params={})
    with TestClient(create_app(root, config=WorkflowConfig(ideation_backend="mock"))) as client:
        detail = client.get(f"/api/tasks/{task.task_id}").json()
        assert detail["task"]["status"] == "failed" and detail["ideas"] == []
        assert detail["workflow"]["status"] == "not_started" and detail["allowed_actions"] == ["retry"]
        assert wait(client, action(client, task.task_id, "retry"))["status"] == "succeeded"
        assert client.get(f"/api/tasks/{task.task_id}").json()["workflow"]["status"] == "waiting_input"


def test_graph_node_failure_is_retryable_and_returns_to_human_review(tmp_path):
    from xhs_agent.agents.research import MockResearchAgent

    class FailOnce(MockResearchAgent):
        failed = False

        def research(self, request):
            if not self.failed:
                self.failed = True
                raise RuntimeError("测试资料节点临时失败")
            return super().research(request)

    root, agent = tmp_path / "failed-node", FailOnce()

    def factory(config, on_event):
        return TaskService(root, config=config, on_event=on_event, research_agent=agent)

    with TestClient(create_app(root, config=WorkflowConfig(ideation_backend="mock"), service_factory=factory)) as client:
        task_id, _ = new_task(client)
        assert wait(client, action(client, task_id, "select", index=0))["status"] == "failed"
        failed = client.get(f"/api/tasks/{task_id}").json()
        assert failed["workflow"]["status"] == "failed" and failed["workflow"]["next_nodes"] == ["research"]
        assert failed["allowed_actions"] == ["retry"] and failed["workflow"]["interrupts"] == []
        assert wait(client, action(client, task_id, "retry"))["status"] == "succeeded"
        resumed = client.get(f"/api/tasks/{task_id}").json()
        assert resumed["task"]["status"] == "pending_review" and resumed["task"]["last_error"] is None
        assert resumed["workflow"]["status"] == "waiting_input"


def test_only_one_web_server_can_own_a_data_directory(tmp_path):
    root = tmp_path / "locked"
    with TestClient(create_app(root, config=WorkflowConfig(ideation_backend="mock"))):
        with pytest.raises(RuntimeError, match="已有 Web 服务"):
            with TestClient(create_app(root, config=WorkflowConfig(ideation_backend="mock"))):
                pass
