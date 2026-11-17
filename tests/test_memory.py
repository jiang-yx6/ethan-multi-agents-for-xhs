"""工作上下文压缩不删除存档、不改变控制状态，也不混淆用户偏好范围。"""
import json

import pytest

from xhs_agent.agents.runtime import ModelToolRuntime
from xhs_agent.agents.tools import AgentTool, ToolArgs
from xhs_agent.config import AgentConfig
from xhs_agent.llm import ChatMessage, ChatRequest, ChatResponse, ToolCall, LLMResponseError
from xhs_agent.memory import ContextPolicy, compact_messages, context_session, message_chars, supervisor_payload
from xhs_agent.models import ContentRequest, DelegationRecord, DelegationTask, DelegationResult, SupervisorContext, UserProfile
from xhs_agent.service import TaskService
from xhs_agent.skills import SkillLibrary
from xhs_agent.storage import Store


def test_supervisor_projection_keeps_full_source_and_structured_controls():
    history = tuple(DelegationRecord(task=DelegationTask(task_id="t", delegation_id=str(i), target_agent="creation",
        instruction="落实具体要求", acceptance_criteria=["交付"]), result=DelegationResult(
            task_id="t", delegation_id=str(i), agent_id="creation", status="completed", summary="结果" + str(i))) for i in range(30))
    context = SupervisorContext(task_id="t", request=ContentRequest(goal="真实需求"), profile=UserProfile(),
        available_agents=(), history=history, user_messages=tuple("用户消息" + str(i) for i in range(30)),
        control_state={"approved": False, "current_version": 12, "pending_revision": "修改 v12：补充真实来源"})
    original = context.model_dump_json()
    payload = supervisor_payload(context, ContextPolicy(recent_delegations=2, recent_user_messages=3))
    assert len(payload["history"]) == 2 and len(payload["user_messages"]) == 3
    assert payload["task_summary"]["covered_delegations"] == 28
    assert payload["task_summary"]["last_earlier_outcomes"][0]["summary"] == "结果27"
    assert payload["context_window"]["total_user_messages"] == 30
    assert payload["control_state"] == context.control_state
    assert context.model_dump_json() == original
    assert len(json.dumps(payload, ensure_ascii=False)) < len(original)


def test_compaction_keeps_tool_batches_and_image_messages_together():
    prefix = [ChatMessage(role="system", content="系统"), ChatMessage(role="user", content="任务")]
    messages = [*prefix, ChatMessage(role="assistant", tool_calls=[ToolCall(id="old", name="lookup", arguments={})]),
                ChatMessage(role="tool", tool_call_id="old", content="早期结果"),
                ChatMessage(role="assistant", tool_calls=[ToolCall(id="a", name="lookup", arguments={}),
                                                         ToolCall(id="b", name="lookup", arguments={})]),
                ChatMessage(role="tool", tool_call_id="a", content="A"),
                ChatMessage(role="tool", tool_call_id="b", content="B"),
                ChatMessage(role="user", images=["data:image/png;base64,aGVsbG8="])]
    projected = compact_messages(messages, ContextPolicy(recent_tool_rounds=1))
    ChatRequest(messages=projected)
    assert len(messages) == 8 and projected[:2] == prefix
    assert [item.tool_call_id for item in projected if item.role == "tool"] == ["a", "b"]
    assert projected[-1].images == messages[-1].images
    assert "早期结果" in projected[2].content


def test_essential_input_budget_fails_explicitly_without_truncation():
    messages = [ChatMessage(role="system", content="系统"), ChatMessage(role="user", content="需求" * 3000)]
    with pytest.raises(LLMResponseError, match="必要任务输入"):
        compact_messages(messages, ContextPolicy(max_input_chars=4000))
    assert len(messages[1].content) == 6000


def test_oversized_latest_batch_keeps_all_calls_metadata_and_continuation_offsets():
    calls = [ToolCall(id=str(i), name="web_fetch", arguments={"url": f"https://example.org/{i}"}) for i in range(5)]
    messages = [ChatMessage(role="system", content="系统"), ChatMessage(role="user", content="完整需求"),
                ChatMessage(role="assistant", tool_calls=calls)]
    for i in range(5):
        messages.append(ChatMessage(role="tool", tool_call_id=str(i), content=json.dumps({
            "url": f"https://example.org/{i}", "title": "原始来源", "text": "引文材料" * 10000,
            "offset": 100, "total_chars": 100000, "next_offset": 40100, "truncated": True}, ensure_ascii=False)))
    original = [message.model_dump() for message in messages]
    projected = compact_messages(messages, ContextPolicy(max_input_chars=20000))
    ChatRequest(messages=projected)
    assert message_chars(projected) <= 20000 and projected[:3] == messages[:3]
    assert [message.tool_call_id for message in projected[3:]] == [str(i) for i in range(5)]
    for i, message in enumerate(projected[3:]):
        value = json.loads(message.content)
        assert value["url"] == f"https://example.org/{i}" and value["context_excerpt"] is True
        assert value["next_offset"] == 100 + len(value["text"]) < 40100
        assert value["total_chars"] == 100000 and value["truncated"] is True
    assert [message.model_dump() for message in messages] == original


def test_large_tool_batch_is_excerpted_for_model_and_fully_archived(tmp_path):
    class Provider:
        requests = []
        def chat(self, request):
            self.requests.append(request)
            message = (ChatMessage(role="assistant", tool_calls=[ToolCall(id=str(i), name="lookup", arguments={})
                       for i in range(5)]) if len(self.requests) == 1 else ChatMessage(role="assistant", content='{"done":true}'))
            return ChatResponse(provider="offline", model="test", message=message,
                                finish_reason="tool_calls" if message.tool_calls else "stop")
    store = Store(tmp_path / "store.db")
    store.create_task("t", "任务")
    provider, events = Provider(), []
    try:
        with context_session(store, "t", ContextPolicy(max_input_chars=20000), lambda value: value):
            result = ModelToolRuntime(provider).run(skills=SkillLibrary().for_agent("research"),
                system_prompt="核实资料", payload={"goal": "保持完整需求"}, allowed_tools=("lookup",),
                tools=(AgentTool("lookup", "查询", ToolArgs, lambda args: "原始结果" * 8000),),
                validate_output=json.loads, on_event=events.append)
        assert result == {"done": True} and message_chars(provider.requests[-1].messages) <= 20000
        replies = [message for message in provider.requests[-1].messages if message.role == "tool"]
        assert len(replies) == 5 and all(json.loads(message.content)["context_excerpt"] for message in replies)
        archived = [message for message in store.context_archives("t")[0]["messages"] if message["role"] == "tool"]
        assert len(archived) == 5 and all(message["content"] == "原始结果" * 8000 for message in archived)
        event = next(event for event in events if event["kind"] == "context_compacted")
        assert event["original_messages"] == event["sent_messages"]
        assert event["original_chars"] > 160000 and event["sent_chars"] <= 20000
    finally:
        store.close()


def test_runtime_compacts_requests_but_archives_all_tool_rounds_and_preferences(tmp_path):
    class Provider:
        requests = []
        def chat(self, request):
            self.requests.append(request)
            index = len(self.requests)
            message = (ChatMessage(role="assistant", tool_calls=[ToolCall(id=str(index), name="lookup", arguments={})],
                                   reasoning_content="private-hidden-reasoning") if index < 6 else
                       ChatMessage(role="assistant", content='{"done": true}'))
            return ChatResponse(provider="offline", model="test", message=message,
                                finish_reason="tool_calls" if index < 6 else "stop")
    store = Store(tmp_path / "store.db")
    store.create_task("t", "主题", memory_scope="alice")
    store.save_preferences("alice", ["简洁自然"])
    provider = Provider()
    try:
        with context_session(store, "t", ContextPolicy(recent_tool_rounds=1), lambda value: value):
            result = ModelToolRuntime(provider, max_model_calls=8).run(skills=SkillLibrary().for_agent("creation"),
                system_prompt="处理任务", payload={"goal": "内容"}, allowed_tools=("lookup",),
                tools=(AgentTool("lookup", "查询", ToolArgs, lambda args: "实际工具结果"),), validate_output=json.loads)
        assert result == {"done": True}
        assert all(json.loads(request.messages[1].content)["long_term_preferences"] == ["简洁自然"] for request in provider.requests)
        assert all(len(request.messages) <= 5 for request in provider.requests)
        assert len(provider.requests[-1].messages) == 5
        archive = store.context_archives("t")[0]
        assert archive["status"] == "completed" and len(archive["messages"]) == 13
        assert [m["tool_call_id"] for m in archive["messages"] if m["role"] == "tool"] == ["1", "2", "3", "4", "5"]
        assert "private-hidden-reasoning" not in json.dumps(archive)
    finally:
        store.close()
    store = Store(tmp_path / "store.db")
    try:
        assert len(store.context_archives("t")[0]["messages"]) == 13
        assert store.preferences("alice")["preferences"] == ["简洁自然"]
        assert store.preferences("bob")["preferences"] == []
    finally:
        store.close()


def test_failed_output_is_archived_with_redaction(tmp_path):
    class Provider:
        def chat(self, request):
            return ChatResponse(provider="offline", model="test", message=ChatMessage(role="assistant", content="bad-secret"), finish_reason="stop")
    store = Store(tmp_path / "store.db")
    store.create_task("t", "主题")
    try:
        with context_session(store, "t", ContextPolicy(), lambda value: value.replace("secret", "[REDACTED]")):
            with pytest.raises(LLMResponseError):
                ModelToolRuntime(Provider(), max_output_retries=0).run(skills=SkillLibrary().for_agent("creation"),
                    system_prompt="处理", payload={}, validate_output=json.loads)
        archive = store.context_archives("t")[0]
        assert archive["status"] == "failed" and archive["messages"][-1]["content"] == "bad-[REDACTED]"
    finally:
        store.close()


def test_full_context_remains_authoritative_after_model_projection(tmp_path):
    from xhs_agent.agents.supervisor import LLMSupervisorAgent
    from test_team_graph import ModelProvider
    provider = ModelProvider()
    config = AgentConfig(supervisor_backend="mock", ideation_backend="mock")
    svc = TaskService(tmp_path, config=config, supervisor_agent=LLMSupervisorAgent(provider, max_output_retries=2))
    try:
        task = svc.create_task("真实路线")
        assert task.status == "pending_review"
        archives = svc.context_archive(task.task_id)
        assert len(archives["runs"]) == 3 and archives["task_summary"] is not None
        assert all(run["status"] == "completed" for run in archives["runs"])
        with pytest.raises(LLMResponseError, match="新版本"):
            svc.request_changes(task.task_id, "需要落实新的内容")
        assert not svc.status(task.task_id).draft.approved
        payload = json.loads(provider.requests[-1].messages[1].content)
        assert payload["control_state"]["approved"] is False
        assert "需要落实新的内容" in payload["control_state"]["pending_revision"]
    finally:
        svc.close()


def test_parent_window_is_bounded_but_all_user_replies_survive_restart(tmp_path):
    from xhs_agent.agents.supervisor import LLMSupervisorAgent
    from xhs_agent.execution import ExecutionConfig
    from test_team_graph import ModelProvider
    class Provider(ModelProvider):
        def chat(self, request):
            response = super().chat(request)
            payload = json.loads(request.messages[1].content)
            if not payload["artifacts"] and not any("结束提问" in text for text in payload["user_messages"]):
                response.message.content = json.dumps({"action": "ask_user", "reason": "补充真实信息", "question": "还有哪些素材？",
                                                       "missing_information": ["用户补充"]})
            return response
    provider = Provider()
    config = AgentConfig(ideation_backend="mock", execution=ExecutionConfig(context=ContextPolicy(recent_user_messages=2)))
    svc = TaskService(tmp_path, config=config, supervisor_agent=LLMSupervisorAgent(provider))
    try:
        task = svc.create_task("记录生活")
        for index in range(8):
            svc.answer(task.task_id, "素材" + str(index))
        assert svc.answer(task.task_id, "结束提问").status == "pending_review"
        assert all(len(json.loads(request.messages[1].content)["user_messages"]) <= 2 for request in provider.requests)
        assert len(svc.collaboration(task.task_id)["user_messages"]) == 9
        assert svc.store.get_stage(task.task_id, "context_summary")["covered_user_messages"] == 7
    finally:
        svc.close()
    svc = TaskService(tmp_path, config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    try:
        assert len(svc.collaboration(task.task_id)["user_messages"]) == 9
        assert len(svc.context_archive(task.task_id)["runs"]) == len(provider.requests)
    finally:
        svc.close()


def test_profile_preferences_api_scope_and_explicit_clear(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from xhs_agent.web import create_app
    from test_team_web import wait
    from uuid import uuid4
    with TestClient(create_app(tmp_path, config=AgentConfig(supervisor_backend="mock", ideation_backend="mock"))) as client:
        assert client.put("/api/profiles/default/preferences", json={"preferences": ["喜欢短句", "喜欢短句"]}).json()["preferences"] == ["喜欢短句"]
        assert client.get("/api/profiles/unknown/preferences").status_code == 404
        job = wait(client, client.post("/api/tasks", json={"request_id": str(uuid4()), "request": {"goal": "主题", "topic": "主题"}}))
        store = Store(tmp_path / "store.db")
        try:
            assert store.memory_scope(job["task_id"]) == "profile:default"
            assert store.preferences("default")["preferences"] == []
        finally:
            store.close()
        assert client.get(f"/api/tasks/{job['task_id']}/context-archive").status_code == 200
        assert client.delete("/api/profiles/default/preferences").json()["preferences"] == []


def test_specialist_receives_only_its_scope_preferences_across_tasks(tmp_path):
    from xhs_agent.agents.structured import LLMCreationAgent
    class Provider:
        def __init__(self):
            self.payloads = []
        def chat(self, request):
            self.payloads.append(json.loads(request.messages[1].content))
            return ChatResponse(provider="offline", model="test", message=ChatMessage(role="assistant", content=json.dumps(
                {"title": "生活记录", "body": "基于用户材料写作"})), finish_reason="stop")
    provider = Provider()
    svc = TaskService(tmp_path, config=AgentConfig(supervisor_backend="mock", ideation_backend="mock"),
                      creation_agent=LLMCreationAgent(provider=provider))
    try:
        svc.store.save_preferences("alice", ["喜欢短句"])
        svc.store.save_preferences("bob", ["喜欢详细步骤"])
        for scope in ("alice", "bob"):
            svc.create_task(ContentRequest(goal="生活记录", topic="生活记录", needs_research=False), memory_scope=scope)
        svc.store.save_preferences("alice", [])
        svc.create_task(ContentRequest(goal="生活记录", topic="生活记录", needs_research=False), memory_scope="alice")
        assert [payload["long_term_preferences"] for payload in provider.payloads] == [["喜欢短句"], ["喜欢详细步骤"], []]
        task = svc.register_task("主题", task_id="bound", memory_scope="alice")
        with pytest.raises(ValueError, match="其他输入"):
            svc.register_task("主题", task_id=task.task_id, memory_scope="bob")
    finally:
        svc.close()


def test_preferences_cli_requires_no_model_and_can_clear(tmp_path, capsys):
    from xhs_agent.cli import main
    flags = ["--data-dir", str(tmp_path), "--memory-scope", "profile:default"]
    assert main([*flags, "preferences", "--set", "不夸张", "--set", "短句"]) == 0
    assert main([*flags, "preferences"]) == 0
    assert "不夸张" in capsys.readouterr().out
    assert main([*flags, "preferences", "--clear"]) == 0
    store = Store(tmp_path / "store.db")
    try:
        assert store.preferences("profile:default")["preferences"] == []
        assert store._conn.execute("SELECT count(*) FROM preference_history").fetchone()[0] == 2
    finally:
        store.close()
