"""启动配置与网页覆盖、输出预算传递及恢复来源的端到端验证。"""
import json
import time
import uuid

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from pydantic import SecretStr

from xhs_agent.cli import main
from xhs_agent.config import AgentConfig, MODEL_ROLES
from xhs_agent.llm import ChatMessage, ChatResponse, LLMConfig, create_llm_provider
from xhs_agent.service import TaskService
from xhs_agent.web import create_app


def wait_job(client, response, status="succeeded"):
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + response.json()["job_id"]).json()["job"]
        if job["status"] not in {"queued", "running"}:
            assert job["status"] == status, job
            return job
        time.sleep(.02)
    pytest.fail("测试作业未完成")


@pytest.mark.parametrize("role", MODEL_ROLES)
def test_web_override_and_startup_are_explicit_and_isolated_by_role(tmp_path, role):
    startup_path = tmp_path / "models.json"
    startup_path.write_text(json.dumps({role: {"provider": "deepseek", "model": "startup-model", "max_tokens": 8192}}), encoding="utf-8")
    original = startup_path.read_bytes()
    initial = LLMConfig(provider="deepseek", model="startup-model", max_tokens=8192, timeout=180,
                        api_key=SecretStr("startup-test-key"), extra_body={"startup_only": True})
    base = AgentConfig(llm_config_path=str(startup_path), **{f"{role}_backend": "llm", f"{role}_llm": initial})
    seen = []

    def factory(config, on_event):
        seen.append(config)
        return TaskService(tmp_path, config=config, on_event=on_event)

    with TestClient(create_app(tmp_path, config=base, service_factory=factory)) as client:
        url = f"/api/settings/{role}"
        response = client.get(url)
        value = response.json()
        assert value["config_source"] == "startup" and value["max_tokens"] == 8192 and value["timeout"] == 180
        assert value["startup"]["origin"] == "file" and value["startup"]["path"] == str(startup_path)
        assert "startup-test-key" not in response.text and "startup_only" not in response.text
        payload = {"backend": "llm", "provider": "deepseek", "model": "web-model", "credential_source": "api_key",
                   "api_key": "web-test-key", "max_tokens": 16384, "timeout": 240}
        response = client.put(url, json=payload)
        assert response.status_code == 200 and response.json()["config_source"] == "web"
        assert response.json()["startup"]["values"]["max_tokens"] == 8192
        assert client.get("/api/tasks").status_code == 200
        active = getattr(seen[-1], f"{role}_llm")
        assert active.model == "web-model" and active.max_tokens == 16384 and active.timeout == 240
        assert active.extra_body == {} and active.api_key.get_secret_value() == "web-test-key"
        # 网页覆盖不会写回启动文件，也不会改变其他角色。
        assert startup_path.read_bytes() == original
        untouched = next(other for other in MODEL_ROLES if other != role)
        assert client.get(f"/api/settings/{untouched}").json()["config_source"] == "startup"
        response = client.delete(url)
        assert response.status_code == 200 and response.json()["config_source"] == "startup"
        assert response.json()["max_tokens"] == 8192
        assert client.get("/api/tasks").status_code == 200
        assert getattr(seen[-1], f"{role}_llm") == initial
        assert startup_path.read_bytes() == original


def test_old_web_settings_keep_defaults_and_copy_includes_limits(tmp_path):
    app = create_app(tmp_path, config=AgentConfig(supervisor_backend="mock", ideation_backend="mock"))
    with TestClient(app) as client:
        legacy = {"backend": "llm", "provider": "deepseek", "model": "legacy-model", "credential_source": "api_key"}
        app.state.repository.save_setting(legacy, role="supervisor")
        value = client.get("/api/settings/supervisor").json()
        assert value["max_tokens"] == 4096 and value["timeout"] == 120 and value["config_source"] == "web"
        payload = {**legacy, "api_key": "copy-limit-key", "max_tokens": 16384, "timeout": 300}
        assert client.put("/api/settings/supervisor", json=payload).status_code == 200
        copied = client.post("/api/settings/review/copy", json={"source_role": "supervisor"})
        assert copied.status_code == 200
        assert copied.json()["max_tokens"] == 16384 and copied.json()["timeout"] == 300
        assert copied.json()["config_source"] == "web" and copied.json()["api_key_configured"]
        reset = client.delete("/api/settings/supervisor").json()
        assert reset["config_source"] == "startup" and not reset["credential_configured"]
        # 保留的网页密钥不会在“跟随启动配置”时被隐式使用。
        assert not reset["api_key_configured"]
        assert client.get("/api/settings/review").json()["api_key_configured"]
    with TestClient(create_app(tmp_path)) as client:
        value = client.get("/api/settings/review").json()
        assert value["max_tokens"] == 16384 and value["timeout"] == 300
        for field in ("max_tokens", "timeout"):
            assert client.put("/api/settings/review", json={**payload, field: 0}).status_code == 422


def test_startup_environment_limits_have_their_own_source(tmp_path, monkeypatch):
    for name, value in {"PROVIDER": "deepseek", "MODEL": "env-model", "API_KEY": "env-test-key",
                        "MAX_TOKENS": "12000", "TIMEOUT": "210", "TRUST_ENV": "true"}.items():
        monkeypatch.setenv("XHS_REVIEW_" + name, value)
    with TestClient(create_app(tmp_path, config=AgentConfig(review_backend="llm"))) as client:
        response = client.get("/api/settings/review")
        value = response.json()
        assert value["config_source"] == "startup" and value["startup"]["origin"] == "environment"
        assert value["max_tokens"] == 12000 and value["timeout"] == 210 and value["trust_env"]
        assert value["credential_configured"] and "env-test-key" not in response.text


def test_cli_passes_the_explicit_config_file_path_to_web(tmp_path, monkeypatch):
    pytest.importorskip("uvicorn")
    path = tmp_path / "models.json"
    path.write_text(json.dumps({role: {"provider": "deepseek", "model": role + "-test", "max_tokens": 10000}
                                for role in ("supervisor", "ideation")}), encoding="utf-8")
    apps = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: apps.append(app))
    assert main(["--data-dir", str(tmp_path / "workspace"), "--llm-config", str(path), "web"]) == 0
    with TestClient(apps[0]) as client:
        value = client.get("/api/settings/supervisor").json()
        assert value["startup"]["origin"] == "file" and value["startup"]["path"] == str(path.resolve())
        assert value["max_tokens"] == 10000


def test_web_limit_change_reaches_anthropic_request_and_recovers_truncated_review(tmp_path, monkeypatch):
    budgets = []

    def handle(request):
        body = json.loads(request.content)
        budgets.append(body["max_tokens"])
        short = body["max_tokens"] == 4096
        version = json.loads(body["messages"][0]["content"][0]["text"])["draft"]["version"]
        return httpx.Response(200, json={"id": "offline-review", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": '{"issues":[' if short else json.dumps({"draft_version": version, "issues": []})}],
            "stop_reason": "max_tokens" if short else "end_turn", "usage": {"input_tokens": 100, "output_tokens": 20}})

    monkeypatch.setattr("xhs_agent.agents.structured.create_llm_provider",
                        lambda config: create_llm_provider(config, transport=httpx.MockTransport(handle)))
    config = AgentConfig(supervisor_backend="mock", ideation_backend="mock")
    with TestClient(create_app(tmp_path, config=config)) as client:
        payload = {"backend": "llm", "provider": "anthropic", "model": "offline-model", "api_key": "review-test-key",
                   "credential_source": "api_key", "max_tokens": 4096}
        assert client.put("/api/settings/review", json=payload).status_code == 200
        job = wait_job(client, client.post("/api/tasks", json={"request_id": str(uuid.uuid4()),
            "request": {"goal": "整理步道素材", "topic": "步道", "needs_research": False}}))
        url = f"/api/tasks/{job['task_id']}"
        detail = client.get(url).json()
        assert detail["task"]["status"] == "waiting_input"
        failure = detail["collaboration"]["history"][-1]["result"]
        assert failure["status"] == "failed"
        assert "review Agent" in failure["failure_reason"] and "4096" in failure["failure_reason"]
        assert "review-test-key" not in json.dumps(detail) and '{"issues":[' not in failure["failure_reason"]
        assert client.put("/api/settings/review", json={**payload, "max_tokens": 8192}).status_code == 200
        wait_job(client, client.post(url + "/answer", json={"request_id": str(uuid.uuid4()), "expected_version": 1,
            "interrupt_id": detail["workflow"]["interrupts"][0]["id"], "answer": "已提高审核输出预算，请重新审核"}))
        task = client.get(url).json()["task"]
        assert task["status"] == "pending_review" and task["current_version"] == 1 and not task["draft"]["approved"]
        assert budgets == [4096, 8192]


def test_supervisor_validation_diagnostics_are_persisted_without_raw_output(tmp_path, monkeypatch):
    class Provider:
        def chat(self, request):
            return ChatResponse(provider="offline", model="test", finish_reason="stop", message=ChatMessage(
                role="assistant", content='{"action":"invalid-secret-tag","raw":"never-echo-model-output"}'))

        def close(self):
            pass

    monkeypatch.setattr("xhs_agent.agents.supervisor.create_llm_provider", lambda config: Provider())
    config = AgentConfig(ideation_backend="mock", supervisor_llm=LLMConfig(provider="openai", model="test"))
    with TestClient(create_app(tmp_path, config=config)) as client:
        job = wait_job(client, client.post("/api/tasks", json={"request_id": str(uuid.uuid4()),
            "request": {"goal": "整理散步素材", "topic": "散步"}}), "failed")
        snapshot = client.get("/api/jobs/" + job["job_id"]).json()
        validations = [event for event in snapshot["events"] if event["kind"] == "output_validation"]
        assert len(validations) == 3
        assert [event["will_retry"] for event in validations] == [True, True, False]
        assert all("supervisor Agent" in event["message"] and "union_tag_invalid" in event["message"] for event in validations)
        assert "union_tag_invalid" in job["error"]
        assert "invalid-secret-tag" not in json.dumps(snapshot) and "never-echo-model-output" not in json.dumps(snapshot)
        task_id = job["task_id"]
    with TestClient(create_app(tmp_path, config=config)) as client:
        assert "union_tag_invalid" in client.get(f"/api/tasks/{task_id}").json()["task"]["last_error"]
        assert len([event for event in client.get("/api/jobs/" + job["job_id"]).json()["events"]
                    if event["kind"] == "output_validation"]) == 3
