"""启动时读取 .env；配置参数、进程环境和本地密钥的优先级验证。"""
import json
import os

import pytest
from dotenv import load_dotenv

from xhs_agent.cli import main
from xhs_agent.config import AgentConfig


@pytest.mark.parametrize("explicit_config", [False, True])
def test_cli_loads_dotenv_before_parsing_config_paths(tmp_path, monkeypatch, explicit_config):
    pytest.importorskip("uvicorn")
    models = tmp_path / "env models.json"
    explicit = tmp_path / "explicit models.json"
    for path, name in ((models, "from-env-file"), (explicit, "from-command-line")):
        path.write_text(json.dumps({role: {"provider": "deepseek", "model": name}
                                    for role in ("supervisor", "ideation")}), encoding="utf8")
    execution = tmp_path / "execution.json"
    execution.write_text(json.dumps({"execution": {"roles": {"creation": {"prompt": "环境文件配置"}}}}), encoding="utf8")
    environment = tmp_path / ".env"
    environment.write_text(f'XHS_LLM_CONFIG="{models.as_posix()}"\n'
                           f'XHS_EXECUTION_CONFIG="{execution.as_posix()}"\n'
                           'TAVILY_API_KEY="file-key#with=characters"\n', encoding="utf-8-sig")
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED")
    for name in ("XHS_LLM_CONFIG", "XHS_EXECUTION_CONFIG", "TAVILY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "terminal-key")
    monkeypatch.setattr("xhs_agent.cli.load_dotenv", lambda **kwargs: load_dotenv(environment, **kwargs))
    monkeypatch.setattr("xhs_agent.web.create_app", lambda root, config, **kwargs: config)
    configs = []
    monkeypatch.setattr("uvicorn.run", lambda config, **kwargs: configs.append(config))
    args = ["--data-dir", str(tmp_path / "data")]
    if explicit_config:
        args += ["--llm-config", str(explicit)]
    assert main([*args, "web"]) == 0
    assert configs[0].supervisor_llm.model == ("from-command-line" if explicit_config else "from-env-file")
    assert configs[0].execution.for_role("creation").prompt == "环境文件配置"
    assert os.environ["TAVILY_API_KEY"] == "terminal-key"


def test_web_factory_reads_dotenv_credentials_without_exposing_them(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from xhs_agent.web import create_app
    values = {"XHS_REVIEW_PROVIDER": "deepseek", "XHS_REVIEW_MODEL": "env-review",
              "XHS_REVIEW_API_KEY": "private-env-test-key", "XHS_REVIEW_MAX_TOKENS": "10000"}
    environment = tmp_path / ".env"
    environment.write_text("\n".join(f"{name}={value}" for name, value in values.items()), encoding="utf8")
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED")
    for name in values:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("xhs_agent.web.app.load_dotenv", lambda **kwargs: load_dotenv(environment, **kwargs))
    with TestClient(create_app(tmp_path / "data", config=AgentConfig(review_backend="llm"))) as client:
        response = client.get("/api/settings/review")
        settings = response.json()
        assert settings["model"] == "env-review" and settings["max_tokens"] == 10000
        assert settings["credential_configured"] and "private-env-test-key" not in response.text
