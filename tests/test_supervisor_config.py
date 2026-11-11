"""父 Agent 的模型、运行模式、凭据和环境变量独立于选题角色。"""
import json
import os

import pytest
from pydantic import SecretStr

from xhs_agent.agents.ideation import LLMIdeationAgent, MockIdeationAgent
from xhs_agent.agents.supervisor import LLMSupervisorAgent, MockSupervisorAgent
from xhs_agent.cli import main
from xhs_agent.config import AgentConfig
from xhs_agent.llm import ChatMessage, ChatResponse, LLMConfig, LLMConfigurationError
from xhs_agent.service import TaskService


@pytest.fixture
def isolated_parent_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("XHS_SUPERVISOR_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XHS_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("XHS_LLM_MODEL", "child-env-model")
    monkeypatch.setenv("XHS_LLM_API_KEY", "child-generic-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "child-vendor-key")


class FinishProvider:
    def __init__(self):
        self.requests = []
        self.closed = False

    def chat(self, request):
        self.requests.append(request)
        return ChatResponse(provider="offline", model="parent", finish_reason="stop", message=ChatMessage(
            role="assistant", content=json.dumps({"action": "finish", "reason": "测试独立模型配置",
                                                   "summary": "已完成父 Agent 配置验证", "artifacts": []})))

    def close(self):
        self.closed = True


def test_missing_parent_does_not_fall_back_to_child_config_or_environment(tmp_path, monkeypatch, isolated_parent_env):
    calls = []
    monkeypatch.setattr("xhs_agent.agents.supervisor.create_llm_provider", lambda config: calls.append(config))
    svc = TaskService(tmp_path, config=AgentConfig(ideation_llm=LLMConfig(
        provider="deepseek", model="child-config-model", api_key=SecretStr("child-config-key"))))
    try:
        with pytest.raises(LLMConfigurationError, match="独立 supervisor"):
            svc.create_task("验证父 Agent")
        assert calls == []
        assert svc.supervisor_agent._config is None
    finally:
        svc.close()


def test_parent_and_child_backends_are_independent(tmp_path):
    svc = TaskService(tmp_path / "child-mock", config=AgentConfig(ideation_backend="mock"))
    try:
        assert isinstance(svc.ideation_agent, MockIdeationAgent)
        assert isinstance(svc.supervisor_agent, LLMSupervisorAgent)
    finally:
        svc.close()
    svc = TaskService(tmp_path / "parent-mock", config=AgentConfig(supervisor_backend="mock"))
    try:
        assert isinstance(svc.supervisor_agent, MockSupervisorAgent)
        assert isinstance(svc.ideation_agent, LLMIdeationAgent)
    finally:
        svc.close()


def test_parent_uses_only_its_explicit_config_and_owns_its_provider(tmp_path, monkeypatch):
    provider, seen = FinishProvider(), []

    def create(config):
        seen.append(config)
        return provider

    monkeypatch.setattr("xhs_agent.agents.supervisor.create_llm_provider", create)
    svc = TaskService(tmp_path, config=AgentConfig(
        supervisor_llm=LLMConfig(provider="anthropic", model="parent-model", api_key=SecretStr("parent-only-key")),
        ideation_llm=LLMConfig(provider="deepseek", model="child-model", api_key=SecretStr("child-only-key")),
    ))
    try:
        task = svc.create_task("验证父 Agent")
        assert task.status == "completed"
        assert seen[0].provider == "anthropic" and seen[0].model == "parent-model"
        assert seen[0].api_key.get_secret_value() == "parent-only-key"
        assert "parent-only-key" not in json.dumps(svc.collaboration(task.task_id))
    finally:
        svc.close()
    assert provider.closed


def test_parent_reads_its_own_environment_namespace(tmp_path, monkeypatch, isolated_parent_env):
    monkeypatch.setenv("XHS_SUPERVISOR_PROVIDER", "anthropic")
    monkeypatch.setenv("XHS_SUPERVISOR_MODEL", "parent-env-model")
    monkeypatch.setenv("XHS_SUPERVISOR_API_KEY", "parent-env-key")
    provider, seen = FinishProvider(), []
    monkeypatch.setattr("xhs_agent.agents.supervisor.create_llm_provider", lambda config: seen.append(config) or provider)
    svc = TaskService(tmp_path)
    try:
        assert svc.create_task("验证环境配置").status == "completed"
        assert seen[0].model == "parent-env-model"
        assert seen[0].api_key.get_secret_value() == "parent-env-key"
    finally:
        svc.close()


def test_parent_does_not_use_generic_or_vendor_key_implicitly(tmp_path, isolated_parent_env):
    svc = TaskService(tmp_path, config=AgentConfig(supervisor_llm=LLMConfig(provider="deepseek", model="parent-model")))
    try:
        with pytest.raises(LLMConfigurationError, match="supervisor.*API Key"):
            svc.create_task("缺少父 Agent 密钥")
    finally:
        svc.close()


def test_cli_can_mock_child_while_loading_only_parent_config(tmp_path, monkeypatch):
    path = tmp_path / "parent.json"
    path.write_text(json.dumps({"supervisor": {"provider": "anthropic", "model": "parent-model",
                                              "api_key_env": "PARENT_TEST_KEY"}}), encoding="utf-8")
    seen = []
    monkeypatch.setattr("xhs_agent.agents.supervisor.create_llm_provider",
                        lambda config: seen.append(config) or FinishProvider())
    assert main(["--data-dir", str(tmp_path / "data"), "--llm-config", str(path),
                 "--mock-ideation", "create", "仅模拟选题"]) == 0
    assert seen[0].model == "parent-model"


def test_web_parent_settings_and_keys_are_separate_and_survive_restart(tmp_path, isolated_parent_env):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from xhs_agent.web import create_app
    from xhs_agent.web.app import credential_scope

    seen = []

    def factory(config, on_event):
        seen.append(config)
        return TaskService(tmp_path, config=config, on_event=on_event)

    def app():
        return create_app(tmp_path, config=AgentConfig(supervisor_backend="mock", ideation_backend="mock"),
                          service_factory=factory)

    payload = {"backend": "llm", "provider": "deepseek", "model": "same-model", "credential_source": "api_key"}
    with TestClient(app()) as client:
        assert client.put("/api/settings/ideation", json={**payload, "api_key": "child-web-key"}).status_code == 200
        absent = client.get("/api/settings/supervisor").json()
        assert not absent["model_configured"] and not absent["api_key_configured"]
        saved = client.put("/api/settings/supervisor", json={**payload, "api_key": "parent-web-key"})
        assert saved.status_code == 200 and saved.json()["api_key_configured"]
        assert "parent-web-key" not in saved.text
        assert saved.json()["api_key_env"] == "XHS_SUPERVISOR_API_KEY"
        assert credential_scope(payload, "supervisor") != credential_scope(payload, "ideation")
        assert client.get("/api/tasks").status_code == 200
        assert seen[-1].supervisor_llm.api_key.get_secret_value() == "parent-web-key"
        assert seen[-1].ideation_llm.api_key.get_secret_value() == "child-web-key"
    with TestClient(app()) as client:
        assert client.get("/api/settings/supervisor").json()["api_key_configured"]
        assert client.get("/api/tasks").status_code == 200
        assert seen[-1].supervisor_llm.api_key.get_secret_value() == "parent-web-key"
        cleared = client.put("/api/settings/supervisor", json={**payload, "clear_api_key": True})
        assert not cleared.json()["api_key_configured"]
        assert client.get("/api/settings/ideation").json()["api_key_configured"]
        assert client.get("/api/tasks").status_code == 200
        assert seen[-1].supervisor_llm.api_key.get_secret_value() == ""
        assert seen[-1].ideation_llm.api_key.get_secret_value() == "child-web-key"
        assert client.put("/api/settings/ideation", json={"backend": "mock"}).status_code == 200
        health = client.get("/api/health").json()
        assert health["ideation_backend"] == "mock" and health["supervisor_backend"] == "llm"
