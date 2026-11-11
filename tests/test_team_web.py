"""团队协作 HTTP API：父 Agent 提问、回复续跑及过期暂停保护。"""
import time
import uuid

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from xhs_agent.cli import main
from xhs_agent.config import AgentConfig
from xhs_agent.service import TaskService
from xhs_agent.web import create_app


def wait(client, response):
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + job_id).json()["job"]
        if job["status"] not in {"queued", "running"}:
            assert job["status"] == "succeeded", job
            return job
        time.sleep(.02)
    pytest.fail("后台作业未完成")


def test_team_api_can_answer_a_question_and_reject_stale_interrupts(tmp_path):
    app = create_app(tmp_path, config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    with TestClient(app) as client:
        assert client.get("/api/health").json()["orchestration"] == "team"
        created = wait(client, client.post("/api/tasks", json={"request_id": str(uuid.uuid4()),
            "request": {"goal": "整理我的旅行素材", "needs_research": False}}))
        task_id = created["task_id"]
        url = f"/api/tasks/{task_id}"
        detail = client.get(url).json()
        assert detail["task"]["status"] == "waiting_input"
        assert detail["allowed_actions"] == ["answer", "cancel"]
        pause = detail["workflow"]["interrupts"][0]
        assert pause["kind"] == "ask_user" and pause["value"]["question"]
        body = {"request_id": str(uuid.uuid4()), "expected_version": 0,
                "interrupt_id": pause["id"], "answer": "我走过江边步道"}
        assert client.post(url + "/answer", json={**body, "interrupt_id": "stale"}).status_code == 409
        assert client.post(url + "/answer", json={**body, "answer": "  "}).status_code == 422
        response = client.post(url + "/answer", json=body)
        answered = wait(client, response)
        assert answered["result"]["status"] == "pending_review"
        assert client.post(url + "/answer", json=body).json()["job_id"] == answered["job_id"]
        assert client.post(url + "/answer", json={**body, "request_id": str(uuid.uuid4())}).status_code == 409
        records = client.get(url + "/collaboration").json()
        assert records["mode"] == "team"
        assert [record["task"]["target_agent"] for record in records["history"]] == ["ideation", "creation", "review"]
        assert "我走过江边步道" in records["user_messages"][0]
        assert len(records["artifacts"]) == 3
        assert client.get(url).json()["task"]["draft"]["approved"] is False


def test_cli_exposes_team_status_and_collaboration(tmp_path, capsys):
    flags = ["--data-dir", str(tmp_path), "--mock-ideation", "--mock-supervisor"]
    assert main([*flags, "create", "路线", "--no-research"]) == 0
    svc = TaskService(tmp_path, config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    try:
        task = svc.store.list_tasks()[0][0]
        assert task.workflow_version == 2 and task.status == "pending_review"
    finally:
        svc.close()
    assert main([*flags, "status", task.task_id]) == 0
    assert main([*flags, "collaboration", task.task_id]) == 0
    assert '"mode": "team"' in capsys.readouterr().out
