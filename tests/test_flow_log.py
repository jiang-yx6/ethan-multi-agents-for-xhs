"""流程文件日志：关联、暂停恢复、真实模型工具、并发与敏感信息过滤。"""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from xhs_agent.agents.structured import LLMCreationAgent
from xhs_agent.config import AgentConfig
from xhs_agent.flow_log import FlowLog
from xhs_agent.llm import ChatMessage, ChatResponse, LLMConfig, ToolCall
from xhs_agent.models import ContentRequest
from xhs_agent.service import TaskService

CONFIG = AgentConfig(supervisor_backend="mock", ideation_backend="mock")


def rows(root):
    return [json.loads(line) for path in sorted((root / "logs").glob("agents-*.jsonl"))
            for line in path.read_text(encoding="utf8").splitlines()]


def test_nodes_delegation_selection_pause_and_restart_append_to_log(tmp_path):
    service = TaskService(tmp_path, config=CONFIG)
    task = service.create_task(ContentRequest(goal="周末路线"))
    first = rows(tmp_path)
    assert task.status == "waiting_input"
    assert any(row["kind"] == "node_paused" and row["node"] == "ask_user" for row in first)
    assert not any(row["kind"] == "node_failed" for row in first)
    assert all(row["task_id"] == task.task_id for row in first)
    service.close()
    service = TaskService(tmp_path, config=CONFIG)
    try:
        task = service.answer(task.task_id, "1")
        assert task.status == "pending_review"
        service.approve(task.task_id)
        service.publish(task.task_id)
        events = rows(tmp_path)
        assert events[:len(first)] == first
        assert any(row["kind"] == "delegation_started" and row["idea_index"] == 1 for row in events)
        assert any(row["kind"] == "node_completed" and row["node"] == "publish"
                   and row["agent_id"] == "publishing" for row in events)
        assert [row["action"] for row in events if row["kind"] == "user_action"] == ["answer", "approve", "publish"]
        assert len({row["run_id"] for row in events if row["kind"] == "execution_started"}) == 4
        assert all(row.get("run_id") for row in events if row["kind"].startswith("node_"))
        assert all("elapsed_ms" in row for row in events if row["kind"] == "node_completed")
    finally:
        service.close()


def test_injected_model_and_tools_inherit_task_and_delegation_context(tmp_path):
    secret = "private-config-test-key"
    class Provider:
        calls = 0
        def chat(self, request):
            self.calls += 1
            message = (ChatMessage(role="assistant", tool_calls=[ToolCall(id="skill-1", name="load_skill",
                            arguments={"skill_id": "social-content"})], reasoning_content="never-log-reasoning")
                       if self.calls == 1 else ChatMessage(role="assistant", content=json.dumps({
                           "title": "真实模型路线", "body": "用户素材 " + secret})))
            return ChatResponse(provider="offline", model="creation-test", message=message,
                                finish_reason="tool_calls" if self.calls == 1 else "stop")

    callback = []
    config = CONFIG.model_copy(update={"creation_llm": LLMConfig(provider="custom", model="test", api_key=secret)})
    service = TaskService(tmp_path, config=config,
                          creation_agent=LLMCreationAgent(provider=Provider(), on_event=callback.append))
    try:
        task = service.create_task("路线")
        events = rows(tmp_path)
        calls = [row for row in events if row["kind"] in {"model_call", "model_result", "tool_started", "skill_tool"}]
        assert len(calls) == 6
        assert all(row["task_id"] == task.task_id and row["run_id"] and row["agent_id"] == "creation"
                   and row["node"] == "execute_agent" and row["delegation_id"] for row in calls)
        assert any(row["model"] == "creation-test" for row in calls if row["kind"] == "model_result")
        text = json.dumps(events)
        assert secret not in text and "never-log-reasoning" not in text and "[REDACTED]" in text
        assert len([event for event in callback if event["kind"] == "model_call"]) == 2
    finally:
        service.close()


def test_failure_records_node_and_safe_trace_then_retry_appends(tmp_path):
    class Broken:
        def create(self, *args, **kwargs):
            raise RuntimeError("接口故障 Bearer never-echo-this-token")
    service = TaskService(tmp_path, config=CONFIG, creation_agent=Broken())
    task = service.register_task(ContentRequest(goal="路线", topic="路线", needs_research=False))
    with pytest.raises(RuntimeError):
        service.start_task(task.task_id)
    failed = rows(tmp_path)
    error = next(row for row in failed if row["kind"] == "node_failed")
    assert error["node"] == "execute_agent" and error["agent_id"] == "creation"
    assert error["delegation_id"] and error["error_type"] == "RuntimeError" and error["frames"]
    assert "never-echo-this-token" not in json.dumps(failed)
    assert failed[-1]["kind"] == "execution_failed"
    service.close()
    service = TaskService(tmp_path, config=CONFIG)
    try:
        assert service.retry(task.task_id).status == "pending_review"
        assert rows(tmp_path)[:len(failed)] == failed
    finally:
        service.close()


def test_parallel_services_do_not_mix_run_or_task_ids(tmp_path):
    services = [TaskService(tmp_path, config=CONFIG) for _ in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks = list(executor.map(lambda pair: pair[1].create_task(f"路线{pair[0]}"), enumerate(services)))
        events = rows(tmp_path)
        mapping = {row["run_id"]: row["task_id"] for row in events if row["kind"] == "execution_started"}
        assert set(mapping.values()) == {task.task_id for task in tasks}
        assert all(mapping[row["run_id"]] == row["task_id"] for row in events if row.get("run_id"))
    finally:
        for service in services:
            service.close()


def test_log_write_error_does_not_fail_business_execution(tmp_path, capsys):
    (tmp_path / "logs").write_text("blocked", encoding="utf8")
    service = TaskService(tmp_path, config=CONFIG)
    try:
        assert service.create_task("路线").status == "pending_review"
        assert "could not be written" in capsys.readouterr().err
    finally:
        service.close()


def test_log_whitelist_drops_prompts_and_redacts_unregistered_credentials(tmp_path):
    log = FlowLog(tmp_path, CONFIG)
    log.write({"kind": "custom", "message": 'api_key="unregistered-secret"',
               "prompt": "hidden-prompt", "reasoning_content": "hidden-reasoning", "api_key": "hidden-key"})
    text = json.dumps(rows(tmp_path))
    assert all(value not in text for value in ("unregistered-secret", "hidden-prompt", "hidden-reasoning", "hidden-key"))
