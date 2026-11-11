"""真实模型选题路径的离线测试：脚本化响应及两类实际 HTTP 适配器。"""
import json
import re

import httpx
import pytest

from xhs_agent.agents.ideation import LLMIdeationAgent, MockIdeationAgent
from xhs_agent.agents.supervisor import LLMSupervisorAgent, MockSupervisorAgent
from xhs_agent.cli import main
from xhs_agent.config import AgentConfig, load_role_llm
from xhs_agent.llm import (
    ChatMessage, ChatResponse, LLMConfigurationError, LLMHTTPError, LLMResponseError,
    ToolCall, create_llm_provider,
)
from xhs_agent.models import ContentRequest, TrendItem, UserProfile
from xhs_agent.service import TaskService
from xhs_agent.skills import SkillLibrary


def idea(**changes):
    return {"title": "周末江边散步路线", "why": "服务周末出游需求", "audience": "上班族",
            "angle": "亲测路线分享", "profile_match": "符合城市生活定位", "content_type": "攻略",
            "trend_urls": [], "required_materials": ["实际路线和照片"], **changes}


def answer(**changes):
    return ChatMessage(role="assistant", content=json.dumps({"ideas": [idea(**changes)]}, ensure_ascii=False))


def tool_message(*calls):
    return ChatMessage(role="assistant", tool_calls=[ToolCall(id=identifier, name=name, arguments=args)
                                                    for identifier, name, args in calls])


class ScriptedProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def chat(self, request):
        self.requests.append(request.model_copy(deep=True))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return ChatResponse(provider="offline", model="test", message=result,
                            finish_reason="tool_calls" if result.tool_calls else "stop")

    def close(self):
        self.closed = True


def agent_with(provider, **options):
    agent = LLMIdeationAgent(provider, **options)
    agent.bind_skills(SkillLibrary().for_agent("ideation"))
    return agent


def test_model_uses_full_context_and_scoped_skill_tools():
    provider = ScriptedProvider(tool_message(
        ("load-1", "load_skill", {"skill_id": "skill-topic-evaluator"}),
        ("read-1", "read_skill_resource", {"skill_id": "skill-topic-evaluator", "relative": "../../shared/scoring-dimensions.md"}),
    ), answer(trend_urls=["https://example.org/trend"]))
    agent = agent_with(provider)
    ideas = agent.ideate(ContentRequest(goal="周末路线攻略", constraints=["不写价格"]),
                         UserProfile(name="路线账号", experiences=["走过江边"]),
                         [TrendItem(title="城市散步", url="https://example.org/trend")])
    assert ideas[0].title == "周末江边散步路线"
    payload = json.loads(provider.requests[0].messages[1].content)
    assert payload["profile"]["experiences"] == ["走过江边"]
    assert payload["request"]["constraints"] == ["不写价格"]
    system = provider.requests[0].messages[0].content
    assert "skill-topic-evaluator" in system
    assert "skill-quality-gate" not in system
    assert {tool.name for tool in provider.requests[0].tools} == {"load_skill", "read_skill_resource"}
    returned = provider.requests[1].messages[-2:]
    assert [message.tool_call_id for message in returned] == ["load-1", "read-1"]
    assert all(message.role == "tool" and not message.is_error for message in returned)
    assert "instructions" in returned[0].content
    assert "评分" in returned[1].content
    agent.close()
    assert not provider.closed  # 注入的 Provider 由调用者管理。


def test_model_events_report_skill_ids_without_private_reasoning():
    events = []
    call = tool_message(("read", "load_skill", {"skill_id": "skill-topic-evaluator"}))
    call.reasoning_content = "private model reasoning"
    provider = ScriptedProvider(call, answer())
    agent_with(provider, on_event=events.append).ideate(ContentRequest(goal="路线"), UserProfile(), [])
    assert any(event.get("skill_id") == "skill-topic-evaluator" for event in events)
    assert "private model reasoning" not in json.dumps(events)


@pytest.mark.parametrize("name,args", [
    ("load_skill", {"skill_id": "skill-quality-gate"}),
    ("load_skill", {"skill_id": "skill-topic-evaluator", "role": "review"}),
    ("load_skill", {"skill_id": 123}),
    ("read_skill_resource", {"skill_id": "skill-topic-evaluator", "relative": "../../registry.json"}),
    ("web_fetch", {"url": "https://example.org"}),
])
def test_tool_errors_are_returned_without_granting_access(name, args):
    provider = ScriptedProvider(tool_message(("bad-call", name, args)), answer())
    assert agent_with(provider).ideate(ContentRequest(goal="路线"), UserProfile(), [])
    returned = provider.requests[1].messages[-1]
    assert returned.role == "tool" and returned.is_error
    assert returned.tool_call_id == "bad-call"
    assert "references/general-rules.md" not in returned.content


def test_disabled_trends_are_not_sent_and_model_cannot_invent_sources():
    provider = ScriptedProvider(answer(trend_urls=["https://invented.example"]), answer())
    ideas = agent_with(provider).ideate(ContentRequest(goal="路线", use_trends=False), UserProfile(),
                                       [TrendItem(title="不用的热点", url="https://example.org/trend")])
    assert json.loads(provider.requests[0].messages[1].content)["trends"] == []
    assert ideas[0].trend_urls == []
    assert "trend_urls" in provider.requests[1].messages[-1].content


@pytest.mark.parametrize("invalid", ["not-json", '{"ideas":[]}', '{"ideas":[{"title":"缺字段"}]}',
                                   json.dumps({"ideas": [idea(title="  ")]}),
                                   json.dumps({"ideas": [idea()], "unexpected": True})])
def test_invalid_output_gets_one_repair_then_returns_validated_ideas(invalid):
    provider = ScriptedProvider(ChatMessage(role="assistant", content=invalid), answer())
    assert agent_with(provider).ideate(ContentRequest(goal="路线"), UserProfile(), [])
    assert len(provider.requests) == 2
    assert "上次输出未通过校验" in provider.requests[1].messages[-1].content


def test_invalid_output_never_silently_falls_back_to_mock():
    provider = ScriptedProvider(ChatMessage(role="assistant", content="invalid"),
                                ChatMessage(role="assistant", content="still invalid"))
    with pytest.raises(LLMResponseError, match="修正次数"):
        agent_with(provider).ideate(ContentRequest(goal="路线"), UserProfile(), [])
    assert len(provider.requests) == 2


def test_model_and_tool_call_limits_terminate_loops():
    provider = ScriptedProvider(tool_message(("a", "load_skill", {"skill_id": "skill-topic-evaluator"})),
                                tool_message(("b", "load_skill", {"skill_id": "skill-topic-evaluator"})))
    with pytest.raises(LLMResponseError, match="模型调用已达上限"):
        agent_with(provider, max_model_calls=2).ideate(ContentRequest(goal="路线"), UserProfile(), [])
    assert len(provider.requests) == 2
    provider = ScriptedProvider(tool_message(("a", "load_skill", {"skill_id": "skill-topic-evaluator"})))
    with pytest.raises(LLMResponseError, match="工具调用已达上限"):
        agent_with(provider, max_tool_calls=0).ideate(ContentRequest(goal="路线"), UserProfile(), [])


def test_repeated_tool_ids_are_rejected():
    call = ("same-id", "load_skill", {"skill_id": "skill-topic-evaluator"})
    provider = ScriptedProvider(tool_message(call), tool_message(call))
    with pytest.raises(LLMResponseError, match="重复"):
        agent_with(provider).ideate(ContentRequest(goal="路线"), UserProfile(), [])


def test_context_does_not_leak_between_ideation_runs():
    provider = ScriptedProvider(answer(), answer())
    agent = agent_with(provider)
    agent.ideate(ContentRequest(goal="第一次任务"), UserProfile(name="第一个账号"), [])
    agent.ideate(ContentRequest(goal="第二次任务"), UserProfile(name="第二个账号"), [])
    assert len(provider.requests[1].messages) == 2
    assert "第一个账号" not in provider.requests[1].messages[1].content


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_both_http_protocols_support_real_skill_round_trip(protocol):
    requests = []
    calls = [{"id": "load-a", "type": "function", "function": {
        "name": "load_skill", "arguments": json.dumps({"skill_id": "skill-topic-evaluator"})}}]
    blocks = [{"type": "thinking", "thinking": "internal", "signature": "signed-block"},
              {"type": "tool_use", "id": "load-a", "name": "load_skill", "input": {"skill_id": "skill-topic-evaluator"}}]

    def handle(incoming):
        body = json.loads(incoming.content)
        requests.append(body)
        if protocol == "openai":
            message = ({"role": "assistant", "content": None, "reasoning_content": "opaque-reasoning", "tool_calls": calls}
                       if len(requests) == 1 else {"role": "assistant", "content": answer().content})
            if len(requests) == 2:
                assert body["messages"][-2]["reasoning_content"] == "opaque-reasoning"
                assert body["messages"][-1]["tool_call_id"] == "load-a"
            return httpx.Response(200, json={"choices": [{"message": message,
                "finish_reason": "tool_calls" if len(requests) == 1 else "stop"}]})
        if len(requests) == 2:
            assert body["messages"][-2]["content"] == blocks
            assert body["messages"][-1]["content"][0]["tool_use_id"] == "load-a"
        return httpx.Response(200, json={"role": "assistant", "content": blocks if len(requests) == 1
                                       else [{"type": "text", "text": answer().content}],
                                       "stop_reason": "tool_use" if len(requests) == 1 else "end_turn"})

    with create_llm_provider(protocol, model="test-model", api_key="offline-key",
                             transport=httpx.MockTransport(handle)) as provider:
        assert agent_with(provider).ideate(ContentRequest(goal="路线"), UserProfile(), [])
    assert len(requests) == 2


def test_task_service_defaults_to_model_and_caches_completed_ideas(data_dir):
    provider = ScriptedProvider(answer())
    svc = TaskService(data_dir, ideation_agent=LLMIdeationAgent(provider), config=AgentConfig(supervisor_backend="mock"))
    try:
        task = svc.create_task(ContentRequest(goal="路线"))
        assert task.status == "waiting_input"
        assert svc.candidate_ideas(task.task_id)[0].title == "周末江边散步路线"
        assert svc.answer(task.task_id, "0").status == "pending_review"
        assert len(provider.requests) == 1
    finally:
        svc.close()
    assert not provider.closed
    svc = TaskService(data_dir, config=AgentConfig(supervisor_backend="mock"))
    try:
        assert isinstance(svc.ideation_agent, LLMIdeationAgent)
        assert svc.candidate_ideas(task.task_id)
    finally:
        svc.close()


def test_provider_failure_returns_to_parent_and_new_delegation_after_reply(data_dir):
    provider = ScriptedProvider(LLMHTTPError(503))
    svc = TaskService(data_dir, ideation_agent=LLMIdeationAgent(provider), config=AgentConfig(supervisor_backend="mock"))
    try:
        task = svc.create_task(ContentRequest(goal="路线"))
        task_id = task.task_id
        assert task.status == "waiting_input"
        failed = svc.collaboration(task_id)["history"][0]
        assert failed["result"]["status"] == "failed"
        assert "503" in svc.workflow(task_id).interrupts[0].value["question"]
    finally:
        svc.close()
    provider = ScriptedProvider(answer())
    svc = TaskService(data_dir, ideation_agent=LLMIdeationAgent(provider), config=AgentConfig(supervisor_backend="mock"))
    try:
        assert svc.answer(task_id, "接口已修复，请继续选题").status == "waiting_input"
        history = svc.collaboration(task_id)["history"]
        assert [record["result"]["status"] for record in history] == ["failed", "completed"]
        assert history[0] == failed
        assert history[0]["task"]["delegation_id"] != history[1]["task"]["delegation_id"]
        assert svc.status(task_id).last_error is None
    finally:
        svc.close()


def test_missing_configuration_is_explicit_and_read_only_commands_still_work(tmp_path, monkeypatch):
    def unconfigured(*args, **kwargs):
        raise LLMConfigurationError("缺少配置")

    monkeypatch.setattr("xhs_agent.agents.ideation.create_llm_provider", unconfigured)
    svc = TaskService(tmp_path / "data", config=AgentConfig(supervisor_backend="mock"))
    try:
        assert isinstance(svc.ideation_agent, LLMIdeationAgent)
        with pytest.raises(LLMConfigurationError, match="XHS_LLM_PROVIDER"):
            svc.create_task(ContentRequest(goal="路线"))
        task_id = svc.store._conn.execute("SELECT task_id FROM tasks").fetchone()[0]
        assert svc.status(task_id).status == "failed"
    finally:
        svc.close()
    svc = TaskService(tmp_path / "offline", config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    try:
        assert isinstance(svc.ideation_agent, MockIdeationAgent)
        assert svc.create_task(ContentRequest(goal="路线")).status == "waiting_input"
    finally:
        svc.close()


@pytest.fixture
def offline_parent(monkeypatch):
    def decide(self, context):
        request = context.request.model_copy(update={"topic": ""})
        return MockSupervisorAgent.decide(self, context.model_copy(update={"request": request}))
    monkeypatch.setattr(LLMSupervisorAgent, "decide", decide)


def test_cli_loads_role_config_uses_model_and_closes_owned_provider(tmp_path, monkeypatch, capsys, offline_parent):
    path = tmp_path / "llm.json"
    path.write_text(json.dumps({"ideation": {"provider": "deepseek", "model": "test-model",
                                          "api_key_env": "TEST_MODEL_KEY"},
                                "supervisor": {"provider": "deepseek", "model": "parent-test-model",
                                               "api_key_env": "SUPERVISOR_TEST_KEY"}}), encoding="utf-8")
    provider = ScriptedProvider(answer())
    configs = []

    def create(config):
        configs.append(config)
        return provider

    monkeypatch.setattr("xhs_agent.agents.ideation.create_llm_provider", create)
    base = ["--data-dir", str(tmp_path / "data"), "--llm-config", str(path)]
    assert main([*base, "create", "路线"]) == 0
    assert configs[0].provider == "deepseek" and configs[0].model == "test-model"
    assert provider.closed and len(provider.requests) == 1
    assert "周末江边散步路线" in capsys.readouterr().out
    assert main(["--data-dir", str(tmp_path / "offline"), "--mock-ideation", "--mock-supervisor", "create", "路线"]) == 0


def test_invalid_role_config_does_not_print_raw_secret_values(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"ideation": {"provider": "deepseek", "model": 123,
                                          "api_key": "never-echo-this-value"}}), encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_role_llm(path, "ideation")
    assert "never-echo-this-value" not in str(caught.value)


def test_cli_reports_failed_task_id_and_can_retry_with_model(tmp_path, monkeypatch, capsys, offline_parent):
    def fail(*args, **kwargs):
        raise LLMConfigurationError("缺少配置")

    monkeypatch.setattr("xhs_agent.agents.ideation.create_llm_provider", fail)
    base = ["--data-dir", str(tmp_path / "data")]
    assert main([*base, "create", "路线"]) == 1
    error = capsys.readouterr().err
    task_id = re.search(r"xhs-[0-9a-f]+", error).group()
    assert "retry" in error
    provider = ScriptedProvider(answer())
    monkeypatch.setattr("xhs_agent.agents.ideation.create_llm_provider", lambda config: provider)
    assert main([*base, "retry", task_id]) == 0
    assert provider.closed
