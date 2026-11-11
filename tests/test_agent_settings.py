"""按 Agent 保存接口、显式复用模型及运行时生效的验证。"""
import json
import time
import uuid

import pytest
from pydantic import SecretStr

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from xhs_agent.config import AgentConfig, MODEL_ROLES
from xhs_agent.llm import ChatMessage, ChatResponse, LLMConfig
from xhs_agent.service import TaskService
from xhs_agent.web import create_app
from xhs_agent.web.app import credential_scope


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, config=AgentConfig(supervisor_backend="mock", ideation_backend="mock"))
    with TestClient(app) as client:
        yield client, app.state.repository


def model(**changes):
    return {"backend": "llm", "provider": "deepseek", "model": "shared-model", "credential_source": "api_key",
            "api_key": "copy-test-secret", **changes}


def test_all_five_agents_have_separate_settings(client):
    client, repo = client
    response = client.get("/api/settings")
    assert response.headers["cache-control"] == "no-store"
    records = response.json()
    assert [record["role"] for record in records] == list(MODEL_ROLES)
    assert next(item for item in records if item["role"] == "review")["backend"] == "rules"
    assert client.put("/api/settings/publishing", json=model()).status_code == 422
    assert client.put("/api/settings/creation", json={"backend": "rules"}).status_code == 422
    assert repo.setting("creation") is None


@pytest.mark.parametrize("target", ["ideation", "research", "creation", "review"])
def test_copy_reuses_config_and_encrypts_an_independent_key_for_each_target(client, target):
    client, repo = client
    assert client.put("/api/settings/supervisor", json=model()).status_code == 200
    response = client.post(f"/api/settings/{target}/copy", json={"source_role": "supervisor"})
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "shared-model" and response.json()["backend"] == "llm"
    assert response.json()["api_key_configured"]
    assert "copy-test-secret" not in response.text
    source_scope = credential_scope(repo.setting("supervisor"), "supervisor")
    target_scope = credential_scope(repo.setting(target), target)
    assert target_scope != source_scope
    assert repo.api_key(target_scope).get_secret_value() == "copy-test-secret"
    # 修改来源不会联动目标；清除目标也不会清除来源。
    assert client.put("/api/settings/supervisor", json=model(model="new-source", api_key="new-source-secret")).status_code == 200
    assert client.get(f"/api/settings/{target}").json()["model"] == "shared-model"
    assert repo.api_key(target_scope).get_secret_value() == "copy-test-secret"
    body = {key: value for key, value in model().items() if key != "api_key"}
    assert client.put(f"/api/settings/{target}", json={**body, "clear_api_key": True}).status_code == 200
    assert repo.api_key(target_scope) is None
    assert repo.api_key(source_scope).get_secret_value() == "new-source-secret"


def test_copy_requires_an_available_source_and_does_not_overwrite_on_error(client):
    client, repo = client
    assert client.put("/api/settings/creation", json=model()).status_code == 200
    before = repo.setting("creation")
    assert client.post("/api/settings/creation/copy", json={"source_role": "creation"}).status_code == 422
    assert client.post("/api/settings/creation/copy", json={"source_role": "publishing"}).status_code == 422
    assert client.post("/api/settings/creation/copy", json={"source_role": "supervisor"}).status_code == 422
    assert repo.setting("creation") == before
    assert client.put("/api/settings/supervisor", json={**model(), "api_key": ""}).status_code == 200
    assert client.post("/api/settings/creation/copy", json={"source_role": "supervisor"}).status_code == 422
    assert repo.setting("creation") == before


def test_copy_from_startup_config_supports_direct_key_and_preserves_existing_behavior(tmp_path):
    app = create_app(tmp_path, config=AgentConfig(supervisor_llm=LLMConfig(
        provider="deepseek", model="startup-model", api_key=SecretStr("startup-secret"))))
    with TestClient(app) as client:
        response = client.post("/api/settings/creation/copy", json={"source_role": "supervisor"})
        assert response.status_code == 200 and response.json()["api_key_configured"]
        assert "startup-secret" not in client.get("/api/settings").text
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/settings/creation").json()["model"] == "startup-model"
        assert client.get("/api/settings/creation").json()["api_key_configured"]


def test_copy_environment_reference_does_not_return_or_store_plain_key(client, monkeypatch):
    client, repo = client
    monkeypatch.setenv("SHARED_TEST_KEY", "shared-environment-secret")
    payload = {key: value for key, value in model().items() if key != "api_key"}
    payload.update(credential_source="environment", api_key_env="SHARED_TEST_KEY")
    assert client.put("/api/settings/supervisor", json=payload).status_code == 200
    copied = client.post("/api/settings/review/copy", json={"source_role": "supervisor"})
    assert copied.status_code == 200 and copied.json()["credential_configured"]
    assert copied.json()["api_key_env"] == "SHARED_TEST_KEY"
    assert "shared-environment-secret" not in copied.text
    assert not repo.has_api_key(credential_scope(repo.setting("review"), "review"))


def test_copied_creation_and_review_models_are_used_by_real_agent_loop(tmp_path, monkeypatch):
    used_models = []

    class Provider:
        def __init__(self, config):
            used_models.append(config.model)

        def chat(self, request):
            prompt = request.messages[0].content
            payload = json.loads(request.messages[1].content)
            if "小红书创作 Agent" in prompt:
                value = {"title": "真实模型测试稿", "body": "基于用户提供的步道素材整理正文。", "images": [], "tags": []}
            elif "内容审核 Agent" in prompt:
                value = {"draft_version": payload["draft"]["version"], "issues": []}
            else:
                refs = {ref["kind"]: ref for ref in payload["artifacts"]}
                role = "creation" if "draft" not in refs else "review" if "review" not in refs else None
                value = {"action": "finish", "reason": "已交付", "summary": "待人工审核", "artifacts": [refs["draft"]]} if role is None else {
                    "action": "delegate", "reason": "执行当前工作", "task": {"task_id": payload["task_id"],
                        "target_agent": role, "instruction": "基于真实材料写作", "acceptance_criteria": ["保留真实素材"],
                        "input_artifacts": [refs["draft"]] if role == "review" else []}}
            return ChatResponse(provider="offline", model="shared-model", finish_reason="stop",
                                message=ChatMessage(role="assistant", content=json.dumps(value, ensure_ascii=False)))

        def close(self):
            pass

    monkeypatch.setattr("xhs_agent.agents.supervisor.create_llm_provider", Provider)
    monkeypatch.setattr("xhs_agent.agents.structured.create_llm_provider", Provider)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.put("/api/settings/supervisor", json=model()).status_code == 200
        for role in ("creation", "review"):
            assert client.post(f"/api/settings/{role}/copy", json={"source_role": "supervisor"}).status_code == 200
        response = client.post("/api/tasks", json={"request_id": str(uuid.uuid4()), "request": {
            "goal": "整理步道笔记", "topic": "散步", "needs_research": False, "supplied_materials": ["周六去过步道"]}})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()["job"]
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(.02)
        assert job["status"] == "succeeded", job
        task = client.get(f"/api/tasks/{job['task_id']}").json()["task"]
        assert task["status"] == "pending_review" and task["draft"]["title"] == "真实模型测试稿"
        assert not task["draft"]["approved"]
        assert used_models == ["shared-model", "shared-model", "shared-model"]
        assert client.get("/api/health").json()["stages"]["creation"] == "llm"


def test_research_model_is_selected_and_cannot_fabricate_search_sources(tmp_path, monkeypatch):
    from xhs_agent.agents.structured import LLMResearchAgent
    from xhs_agent.models import ResearchRequest
    from xhs_agent.skills import SkillLibrary

    class Provider:
        calls = 0

        def chat(self, request):
            self.calls += 1
            value = {"topic": "散步", "target_audience": "上班族", "pain_points": [], "angles": [], "keywords": [], "tags": [],
                     "sources": [{"title": "编造来源", "url": "https://example.org/invented"}] if self.calls == 1 else []}
            return ChatResponse(provider="offline", model="research", finish_reason="stop",
                                message=ChatMessage(role="assistant", content=json.dumps(value, ensure_ascii=False)))

    provider = Provider()
    agent = LLMResearchAgent(provider=provider)
    agent.bind_skills(SkillLibrary().for_agent("research"))
    brief = agent.research(ResearchRequest(topic="散步", supplied_materials=["去过步道"]))
    assert provider.calls == 2 and brief.sources == []
    assert brief.supplied_materials == ["去过步道"]
    svc = TaskService(tmp_path, config=AgentConfig(research_backend="llm"))
    try:
        assert isinstance(svc.research_agent, LLMResearchAgent)
    finally:
        svc.close()
