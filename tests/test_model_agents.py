"""共享模型基础设施保持角色隔离、资源所有权和统一执行配置。"""
import pytest

from xhs_agent.agents.ideation import LLMIdeationAgent
from xhs_agent.agents.structured import LLMCreationAgent, LLMResearchAgent, LLMReviewAgent
from xhs_agent.agents.supervisor import LLMSupervisorAgent
from xhs_agent.config import AgentConfig
from xhs_agent.execution import ExecutionConfig, RoleExecution
from xhs_agent.llm import LLMConfig
from xhs_agent.service import TaskService


MODEL_AGENTS = (LLMSupervisorAgent, LLMIdeationAgent, LLMResearchAgent, LLMCreationAgent, LLMReviewAgent)


@pytest.mark.parametrize("agent_class", MODEL_AGENTS)
def test_all_model_agents_close_only_their_own_provider_once(agent_class, monkeypatch):
    class Provider:
        closed = 0
        def close(self):
            self.closed += 1
    owned = Provider()
    calls = []
    monkeypatch.setattr("xhs_agent.agents.model.create_llm_provider", lambda config: calls.append(config) or owned)
    agent = agent_class(config=LLMConfig(provider="deepseek", model="configured-model"))
    assert calls == []  # 只读服务组装不会创建连接。
    assert agent._get_provider() is owned and agent._get_provider() is owned
    assert len(calls) == 1
    agent.close()
    agent.close()
    assert owned.closed == 1
    external = Provider()
    agent_class(provider=external).close()
    assert external.closed == 0


@pytest.mark.parametrize("agent_class", (LLMResearchAgent, LLMCreationAgent, LLMReviewAgent))
def test_specialists_do_not_fall_back_to_generic_vendor_credentials(agent_class, monkeypatch):
    seen = []
    monkeypatch.setattr("xhs_agent.agents.model.create_llm_provider", lambda config: seen.append(config) or object())
    agent = agent_class(config=LLMConfig(provider="deepseek", model="independent-model"))
    agent._get_provider()
    assert seen[0].api_key_env == f"XHS_{agent.role.upper()}_API_KEY"
    assert seen[0].api_key is None


def test_factory_uses_one_execution_budget_source_and_defaults_to_unlimited(tmp_path):
    execution = ExecutionConfig(roles={"ideation": RoleExecution(
        prompt="用户需求优先", max_model_calls=2, max_tool_calls=1, max_output_retries=0)})
    service = TaskService(tmp_path, config=AgentConfig(supervisor_backend="mock", execution=execution))
    try:
        assert service.ideation_agent.execution == execution.for_role("ideation")
        assert service.ideation_agent.execution.runtime_options() == {
            "max_model_calls": 2, "max_tool_calls": 1, "max_output_retries": 0}
        assert "ideation_max_model_calls" not in AgentConfig.model_fields
        assert ExecutionConfig().for_role("ideation").runtime_options() == {
            "max_model_calls": None, "max_tool_calls": None, "max_output_retries": None}
        assert ExecutionConfig().for_role("creation").max_model_calls is None
    finally:
        service.close()
