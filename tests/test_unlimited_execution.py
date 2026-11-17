"""不限次数时跨过原模型、工具、修正和父图轮次上限；只使用本地假接口。"""
import json

from xhs_agent.agents.images import GenerateArgs, ImageTools
from xhs_agent.agents.runtime import ModelToolRuntime
from xhs_agent.agents.tools import AgentTool, ToolArgs
from xhs_agent.config import AgentConfig
from xhs_agent.llm import ChatMessage, ChatResponse, ToolCall
from xhs_agent.models import DelegateDecision, DelegationResult, DelegationTask, FinishDecision, ImageRef
from xhs_agent.service import TaskService
from xhs_agent.skills import SkillLibrary


def test_default_runtime_exceeds_old_call_tool_retry_and_recursion_limits():
    class Provider:
        calls = 0
        def chat(self, request):
            self.calls += 1
            if self.calls <= 45:
                message = ChatMessage(role="assistant", tool_calls=[ToolCall(id=str(self.calls), name="lookup")])
            else:
                message = ChatMessage(role="assistant", content="invalid" if self.calls <= 51 else '{"done": true}')
            return ChatResponse(provider="offline", model="test", message=message,
                                finish_reason="tool_calls" if message.tool_calls else "stop")
    provider, events = Provider(), []
    result = ModelToolRuntime(provider).run(skills=SkillLibrary().for_agent("research"),
        system_prompt="核实材料后返回 JSON", payload={}, allowed_tools=("lookup",),
        tools=(AgentTool("lookup", "查询材料", ToolArgs, lambda args: "真实工具材料"),),
        validate_output=json.loads, on_event=events.append)
    assert result == {"done": True} and provider.calls == 52
    assert len([event for event in events if event["kind"] == "tool_started"]) == 45
    assert len([event for event in events if event["kind"] == "output_validation"]) == 6


def test_parent_graph_runs_over_twenty_delegations_without_fixed_step_budget(tmp_path):
    class Supervisor:
        def decide(self, context):
            assert context.remaining_rounds is None
            if len(context.history) >= 35:
                return FinishDecision(action="finish", reason="完成", summary="已完成全部委派")
            return DelegateDecision(action="delegate", reason="继续处理", task=DelegationTask(
                task_id=context.task_id, delegation_id=str(len(context.history)), target_agent="research",
                instruction="处理当前材料", acceptance_criteria=["完成当前材料整理"]))
    class Worker:
        def run(self, task):
            return DelegationResult(task_id=task.task_id, delegation_id=task.delegation_id,
                                    agent_id=task.target_agent, status="completed", summary="材料整理完成")
    service = TaskService(tmp_path, config=AgentConfig(ideation_backend="mock"),
                          supervisor_agent=Supervisor(), delegated_agents={"research": Worker()})
    try:
        task = service.create_task("批次处理材料")
        assert task.status == "completed" and len(service.collaboration(task.task_id)["history"]) == 35
    finally:
        service.close()


def test_image_creation_has_no_four_image_limit():
    class Provider:
        def generate(self, prompt, reference):
            return ImageRef(description=prompt, source="generated://image")
    tools = ImageTools([], provider=Provider())
    for index in range(6):
        result = json.loads(tools.generate(GenerateArgs(prompt="图片" + str(index))))
        assert result["image_id"] == "generated-" + str(index)
    assert len(tools.generated) == 6
