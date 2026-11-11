"""第一阶段的失败回传、验收边界和可诊断校验错误。全程离线。"""
import json

import pytest

from xhs_agent.agents.runtime import ModelToolRuntime
from xhs_agent.config import AgentConfig
from xhs_agent.llm import (
    ChatMessage, ChatResponse, LLMConfigurationError, LLMConnectionError,
    LLMHTTPError, LLMResponseError,
)
from xhs_agent.models import (
    ArtifactReference, ContentRequest, DelegationResult, DelegationTask, IdeaCandidate,
)
from xhs_agent.service import TaskService
from xhs_agent.skills import SkillLibrary
from xhs_agent.team_store import TeamStore


CONFIG = AgentConfig(ideation_backend="mock", supervisor_backend="mock")


class FailingAgent:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def fail(self, *args, **kwargs):
        self.calls += 1
        raise self.error

    ideate = research = create = review = fail


class SelectRole:
    def __init__(self, role):
        self.role = role

    def decide(self, ctx):
        if ctx.history and ctx.history[-1].result.status == "failed":
            return {"action": "ask_user", "reason": "先处理失败", "question": ctx.history[-1].result.failure_reason,
                    "missing_information": ["修复或提供替代要求"]}
        latest = {ref.kind: ref for ref in ctx.artifacts}
        role = "creation" if self.role == "review" and "draft" not in latest else self.role
        return {"action": "delegate", "reason": "执行明确委派", "task": {
            "task_id": ctx.task_id, "target_agent": role, "instruction": "根据真实素材整理",
            "acceptance_criteria": ["不添加未提供的经历"],
            "input_artifacts": [latest["draft"].model_dump(mode="json")] if role == "review" else [],
        }}


@pytest.mark.parametrize("role", ["ideation", "research", "creation", "review"])
@pytest.mark.parametrize("error", [LLMResponseError("模型输出无效"), LLMConnectionError("连接失败"), LLMHTTPError(503)])
def test_builtin_failure_is_persisted_and_parent_can_ask(tmp_path, role, error):
    worker = FailingAgent(error)
    svc = TaskService(tmp_path, config=CONFIG, supervisor_agent=SelectRole(role), **{role + "_agent": worker})
    try:
        task = svc.create_task("路线")
        assert task.status == "waiting_input" and not task.last_error
        record = svc.collaboration(task.task_id)["history"][-1]
        assert record["result"]["status"] == "failed" and str(error) in record["result"]["failure_reason"]
        delegation = DelegationTask.model_validate(record["task"])
        # 同一次委派重放只返回已保存的失败；父 Agent 再次执行必须使用新 ID。
        assert svc.agent_registry.agent(role).run(delegation).status == "failed"
        assert worker.calls == 1 and svc.store.publish_count(task.task_id) == 0
    finally:
        svc.close()
    svc = TaskService(tmp_path, config=CONFIG)
    try:
        assert svc.workflow(task.task_id).status == "waiting_input"
        assert svc.collaboration(task.task_id)["history"][-1] == record
    finally:
        svc.close()


@pytest.mark.parametrize("error", [LLMConfigurationError("配置未就绪"), RuntimeError("程序错误")])
def test_configuration_and_programming_errors_remain_retryable_nodes(tmp_path, error):
    svc = TaskService(tmp_path, config=CONFIG, creation_agent=FailingAgent(error))
    try:
        task = svc.register_task(ContentRequest(goal="路线", topic="路线", needs_research=False))
        with pytest.raises(type(error), match=str(error)):
            svc.start_task(task.task_id)
        assert svc.status(task.task_id).status == "failed"
        assert svc.collaboration(task.task_id)["history"] == []
        assert svc.workflow(task.task_id).next_nodes == ["execute_agent"]
    finally:
        svc.close()


def test_repeated_failure_waits_for_each_new_reply_instead_of_spinning(tmp_path):
    worker = FailingAgent(LLMResponseError("需要更正输出要求"))
    svc = TaskService(tmp_path, config=CONFIG, creation_agent=worker)
    try:
        task = svc.create_task(ContentRequest(goal="路线", topic="路线", needs_research=False))
        assert task.status == "waiting_input" and worker.calls == 1
        task = svc.answer(task.task_id, "已调整，请再试一次")
        assert task.status == "waiting_input" and worker.calls == 2
        history = svc.collaboration(task.task_id)["history"]
        assert [record["user_message_count"] for record in history] == [0, 1]
        assert len({record["task"]["delegation_id"] for record in history}) == 2
    finally:
        svc.close()


def test_same_kind_inputs_and_changed_delegation_contract_are_rejected(tmp_path):
    svc = TaskService(tmp_path, config=CONFIG)
    try:
        task = svc.register_task("路线")
        reference = ArtifactReference(task_id=task.task_id, artifact_id="material", kind="material", version=1, summary="素材")
        values = dict(task_id=task.task_id, target_agent="research", instruction="整理资料", acceptance_criteria=["不虚构"])
        with pytest.raises(ValueError, match="每类输入产物"):
            DelegationTask(**values, input_artifacts=[reference, reference.model_copy(update={"artifact_id": "other"})])
        delegation = DelegationTask(**values)
        ledger = TeamStore(svc.store)
        ledger.begin(delegation)
        result = DelegationResult(task_id=task.task_id, delegation_id=delegation.delegation_id,
                                  agent_id="research", status="failed", summary="失败", failure_reason="未取得资料")
        with pytest.raises(ValueError, match="其他执行要求"):
            ledger.finish(delegation.model_copy(update={"instruction": "篡改后的要求"}), result)
    finally:
        svc.close()


@pytest.mark.parametrize("role", ["ideation", "research", "creation", "review"])
def test_invalid_builtin_output_returns_failed_instead_of_claiming_completion(tmp_path, role):
    class InvalidAgent:
        def invalid(self, *args, **kwargs):
            return [] if role == "ideation" else None
        ideate = research = create = review = invalid

    svc = TaskService(tmp_path, config=CONFIG, supervisor_agent=SelectRole(role), **{role + "_agent": InvalidAgent()})
    try:
        task = svc.create_task("路线")
        result = svc.collaboration(task.task_id)["history"][-1]["result"]
        assert task.status == "waiting_input" and result["status"] == "failed" and not result["artifacts"]
    finally:
        svc.close()


def test_finish_checks_review_payload_version_as_well_as_reference(tmp_path):
    from xhs_agent.models import ReviewReport

    class Supervisor:
        def decide(self, ctx):
            if len(ctx.history) < 2:
                return SelectRole("review").decide(ctx)
            return {"action": "finish", "reason": "准备交付", "summary": "草稿完成",
                    "artifacts": [ref.model_dump(mode="json") for ref in ctx.artifacts]}

    class InvalidReviewer:
        def run(self, task):
            draft_ref = next(ref for ref in task.input_artifacts if ref.kind == "draft")
            ref = ArtifactReference(task_id=task.task_id, artifact_id="invalid-review", kind="review",
                                    version=draft_ref.version, summary="引用与正文版本不一致")
            TeamStore(svc.store).put_artifact(ref, ReviewReport(draft_version=draft_ref.version + 1).model_dump(mode="json"))
            return DelegationResult(task_id=task.task_id, delegation_id=task.delegation_id,
                                    agent_id="review", status="completed", summary="审核完成", artifacts=[ref])

    svc = TaskService(tmp_path, config=CONFIG, supervisor_agent=Supervisor(), delegated_agents={"review": InvalidReviewer()})
    try:
        task = svc.register_task("路线")
        with pytest.raises(ValueError, match="审核产物正文"):
            svc.start_task(task.task_id)
        assert svc.status(task.task_id).status == "failed"
        assert not svc.status(task.task_id).draft.approved and svc.store.publish_count(task.task_id) == 0
    finally:
        svc.close()


class InvalidProvider:
    def chat(self, request):
        return ChatResponse(provider="offline", model="test", finish_reason="stop",
                            message=ChatMessage(role="assistant", content=json.dumps({
                                "title": "secret-output-value", "sk-never-echo-this-extra-field": "hidden"})))


def test_validation_error_reports_safe_field_types_and_stops_at_budget():
    events = []
    runtime = ModelToolRuntime(InvalidProvider(), max_model_calls=1, max_output_retries=3)
    with pytest.raises(LLMResponseError) as caught:
        runtime.run(skills=SkillLibrary().for_agent("ideation"), system_prompt="返回选题", payload={},
                    validate_output=IdeaCandidate.model_validate_json, on_event=events.append)
    message = str(caught.value)
    assert "ideation Agent" in message and "why: missing" in message
    assert "secret-output-value" not in message and "sk-never-echo" not in json.dumps(events)
    failed = [event for event in events if event["kind"] == "output_validation"]
    assert failed[0]["will_retry"] is False and failed[0]["attempt"] == 1


def test_semantic_validation_failure_keeps_rule_in_final_error_and_event():
    events = []
    def reject(raw):
        raise ValueError("交付草稿前需要委派 review 检查当前版本")
    with pytest.raises(LLMResponseError, match="检查当前版本"):
        ModelToolRuntime(InvalidProvider(), max_output_retries=0).run(
            skills=SkillLibrary().for_agent("supervisor"), system_prompt="检查输出", payload={},
            validate_output=reject, on_event=events.append)
    assert "检查当前版本" in events[-1]["message"] and not events[-1]["will_retry"]
