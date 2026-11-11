"""对话工作台的数据恢复、持续讨论和产物访问边界。"""
import uuid

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from xhs_agent.config import AgentConfig
from xhs_agent.conversation import conversation_messages
from xhs_agent.service import TaskService
from xhs_agent.web import create_app

from test_team_web import wait


def test_conversation_persists_chronological_messages_and_decisions_across_restart(tmp_path):
    config = AgentConfig(supervisor_backend="mock", ideation_backend="mock")
    with TestClient(create_app(tmp_path, config=config)) as client:
        job = wait(client, client.post("/api/tasks", json={"request_id": str(uuid.uuid4()),
            "request": {"goal": "记录真实路线", "needs_research": False}}))
        url = f"/api/tasks/{job['task_id']}"
        detail = client.get(url).json()
        pause = detail["workflow"]["interrupts"][0]
        first = detail["collaboration"]["messages"]
        assert [item["kind"] for item in first] == ["delegate", "ask_user"]
        reply = "选择第一个方向，素材是我走过的江边步道"
        wait(client, client.post(url + "/answer", json={"request_id": str(uuid.uuid4()), "expected_version": 0,
                                                       "interrupt_id": pause["id"], "answer": reply}))
        detail = client.get(url).json()
        messages = detail["collaboration"]["messages"]
        assert [item["role"] for item in messages] == ["assistant", "assistant", "user", "assistant", "assistant", "assistant"]
        assert messages[2]["content"] == reply
        assert messages[-1]["kind"] == "finish"
        assert len({item["id"] for item in messages}) == len(messages)
        assert detail["allowed_actions"].count("message") == 1
    with TestClient(create_app(tmp_path, config=config)) as client:
        assert client.get(url).json()["collaboration"]["messages"] == messages


def test_chat_message_revisits_parent_and_revokes_previous_approval(tmp_path):
    config = AgentConfig(supervisor_backend="mock", ideation_backend="mock")
    with TestClient(create_app(tmp_path, config=config)) as client:
        job = wait(client, client.post("/api/tasks", json={"request_id": str(uuid.uuid4()),
            "request": {"goal": "整理散步素材", "topic": "江边步道", "needs_research": False}}))
        url = f"/api/tasks/{job['task_id']}"
        detail = client.get(url).json()
        version = detail["task"]["current_version"]
        wait(client, client.post(url + "/approve", json={"request_id": str(uuid.uuid4()), "expected_version": version,
            "interrupt_id": detail["workflow"]["interrupts"][0]["id"]}))
        detail = client.get(url).json()
        assert detail["task"]["draft"]["approved"]
        body = {"request_id": str(uuid.uuid4()), "expected_version": version,
                "interrupt_id": detail["workflow"]["interrupts"][0]["id"], "text": "请补充适合散步的时间段"}
        assert client.post(url + "/message", json={**body, "interrupt_id": "stale"}).status_code == 409
        assert client.post(url + "/message", json={**body, "text": "   "}).status_code == 422
        job = wait(client, client.post(url + "/message", json=body))
        current = client.get(url).json()
        assert current["task"]["status"] == "pending_review"
        assert current["task"]["current_version"] == version + 1
        assert not any(item["approved"] for item in current["task"]["history"])
        assert any(item["role"] == "user" and item["content"] == body["text"] for item in current["collaboration"]["messages"])
        assert client.post(url + "/message", json=body).json()["job_id"] == job["job_id"]
        assert client.post(url + "/message", json={**body, "request_id": str(uuid.uuid4())}).status_code == 409


def test_artifact_contents_are_versioned_and_bound_to_the_current_task(tmp_path):
    config = AgentConfig(supervisor_backend="mock", ideation_backend="mock")
    with TestClient(create_app(tmp_path, config=config)) as client:
        task_ids = []
        for topic in ("散步", "露营"):
            job = wait(client, client.post("/api/tasks", json={"request_id": str(uuid.uuid4()),
                "request": {"goal": topic, "topic": topic, "needs_research": False}}))
            task_ids.append(job["task_id"])
        url = f"/api/tasks/{task_ids[0]}"
        detail = client.get(url).json()
        ref = next(ref for ref in detail["collaboration"]["artifacts"] if ref["kind"] == "draft")
        params = {"artifact_id": ref["artifact_id"], "version": ref["version"]}
        response = client.get(url + "/artifacts", params=params)
        assert response.status_code == 200 and response.json()["reference"] == ref
        assert response.json()["content"]["title"] == detail["task"]["draft"]["title"]
        assert client.get(f"/api/tasks/{task_ids[1]}/artifacts", params=params).status_code == 404
        assert client.get(url + "/artifacts", params={**params, "artifact_id": "../../examples/llm-config.json"}).status_code == 404
        assert client.get(url + "/artifacts", params={**params, "version": 999}).status_code == 404
        assert client.get(url + "/artifacts", params={**params, "version": 0}).status_code == 422


def test_legacy_checkpoint_question_and_answer_are_read_without_duplicate_question():
    state = {"user_messages": ["问题：走过哪里？\n用户回复：江边步道"], "turn": 1,
             "decision": {"action": "ask_user", "reason": "缺少路线", "question": "走过哪里？"}}
    messages = conversation_messages(state)
    assert [(item["role"], item["content"]) for item in messages] == [("assistant", "走过哪里？"), ("user", "江边步道")]


def test_parent_can_reply_to_discussion_without_rewriting_draft(tmp_path):
    from xhs_agent.agents.supervisor import MockSupervisorAgent
    from xhs_agent.models import FinishDecision

    class ConversationalParent(MockSupervisorAgent):
        def decide(self, context):
            if context.user_messages and context.user_messages[-1].startswith("对话 v"):
                return FinishDecision(action="finish", reason="用户只要求解释", summary="这篇内容采用真实路线分享的角度。",
                                      artifacts=tuple(ref for ref in context.artifacts if ref.kind == "draft"))
            return super().decide(context)

    svc = TaskService(tmp_path, config=AgentConfig(supervisor_backend="mock", ideation_backend="mock"),
                      supervisor_agent=ConversationalParent())
    try:
        task = svc.create_task("路线")
        svc.approve(task.task_id)
        result = svc.message(task.task_id, "解释一下这篇内容的角度")
        assert result.current_version == task.current_version and not result.draft.approved
        assert result.status == "pending_review"
        messages = svc.collaboration(task.task_id)["messages"]
        assert messages[-1]["content"] == "这篇内容采用真实路线分享的角度。"
    finally:
        svc.close()
