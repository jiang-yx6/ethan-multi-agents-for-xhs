"""严格类型注册、旧编码读取、父图恢复与模型消息独立存档。"""
import json

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

from xhs_agent.agents.supervisor import LLMSupervisorAgent
from xhs_agent.checkpoints import checkpoint_serializer
from xhs_agent.config import AgentConfig
from xhs_agent.llm import ChatMessage, ChatResponse, ToolCall
from xhs_agent.models import (
    AskUserDecision, DelegateDecision, DelegationTask, Draft, DraftContent,
    FinishDecision, IdeaCandidate, ResearchBrief, ReviewReport,
)
from xhs_agent.service import TaskService


@pytest.mark.parametrize("result", [
    [IdeaCandidate(title="路线", why="周末出游", audience="上班族", angle="步行")],
    ResearchBrief(topic="路线", target_audience="上班族", pain_points=[], angles=[], keywords=[], tags=[]),
    Draft(title="路线", body="实际走过的路段"),
    DraftContent(title="路线", body="实际走过的路段"),
    ReviewReport(draft_version=1, issues=[]),
    DelegateDecision(action="delegate", reason="需要选题", task=DelegationTask(
        task_id="checkpoint-test", target_agent="ideation", instruction="结合需求选题", acceptance_criteria=["有明确角度"])),
    AskUserDecision(action="ask_user", reason="缺少素材", question="走过哪里？", missing_information=("路线",)),
    FinishDecision(action="finish", reason="完成", summary="已交付"),
])
def test_legacy_model_checkpoint_encoding_loads_without_warnings(result, caplog):
    messages = [
        ChatMessage(role="assistant", tool_calls=[ToolCall(id="load-1", name="load_skill", arguments={"skill_id": "topic"})],
                    reasoning_content="opaque-reasoning",
                    anthropic_blocks=[{"type": "thinking", "thinking": "opaque", "signature": "signature"}]),
        ChatMessage(role="tool", tool_call_id="load-1", content="skill text"),
    ]
    payload = {"messages": messages, "result": result}
    # 旧默认序列化器写出的数据无需迁移，也能被新的精确白名单读取。
    encoded = JsonPlusSerializer().dumps_typed(payload)
    restored = checkpoint_serializer().loads_typed(encoded)
    assert restored == payload
    assert type(restored["messages"][0]) is ChatMessage
    assert type(restored["messages"][0].tool_calls[0]) is ToolCall
    restored_result = restored["result"]
    assert type(restored_result[0] if isinstance(restored_result, list) else restored_result) is type(
        result[0] if isinstance(result, list) else result)
    assert not any("unregistered type" in record.message or "Blocked deserialization" in record.message for record in caplog.records)


class UnregisteredModel(BaseModel):
    value: str


def test_checkpoint_serializer_does_not_restore_arbitrary_project_types():
    serializer = checkpoint_serializer()
    restored = serializer.loads_typed(serializer.dumps_typed(UnregisteredModel(value="untrusted")))
    assert not isinstance(restored, UnregisteredModel)


def test_parent_resumes_after_restart_without_duplicating_runtime_messages_in_checkpoints(tmp_path, caplog):
    class Provider:
        def __init__(self, decision):
            self.decision = decision

        def chat(self, request):
            return ChatResponse(provider="offline", model="test", finish_reason="stop", message=ChatMessage(
                role="assistant", content=json.dumps(self.decision), reasoning_content="private-model-reasoning"))

    config = AgentConfig(supervisor_backend="mock", ideation_backend="mock")
    question = {"action": "ask_user", "reason": "缺少路线", "question": "走过哪里？", "missing_information": ["路线"]}
    svc = TaskService(tmp_path, config=config, supervisor_agent=LLMSupervisorAgent(Provider(question)))
    try:
        task = svc.create_task("整理周末散步素材")
        assert task.status == "waiting_input"
        with svc._open_graph(task.task_id) as graph:
            records = list(graph.checkpointer.list(None))
        assert records and all("messages" not in record.checkpoint["channel_values"] for record in records)
        archives = svc.context_archive(task.task_id)["runs"]
        assert len(archives) == 1 and archives[0]["status"] == "completed"
        assert len(archives[0]["messages"]) == 3
        assert "private-model-reasoning" not in json.dumps(archives)
    finally:
        svc.close()

    done = {"action": "finish", "reason": "素材整理完成", "summary": "已记录路线", "artifacts": []}
    svc = TaskService(tmp_path, config=config, supervisor_agent=LLMSupervisorAgent(Provider(done)))
    try:
        assert svc.workflow(task.task_id).status == "waiting_input"
        assert svc.answer(task.task_id, "走过江边步道").status == "completed"
        assert len(svc.context_archive(task.task_id)["runs"]) == 2
    finally:
        svc.close()
    assert not any("unregistered type" in record.message or "Blocked deserialization" in record.message for record in caplog.records)
