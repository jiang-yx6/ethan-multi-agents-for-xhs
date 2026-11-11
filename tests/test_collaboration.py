"""协作契约、目录权限与现有模型工具边界的离线验证。"""
import json

import pytest
from pydantic import ValidationError

from xhs_agent.agents import AgentRegistry, DelegatedAgent, SupervisorAgent, build_agent_registry
from xhs_agent.agents.base import SkillEnabledAgent
from xhs_agent.agents.ideation import LLMIdeationAgent
from xhs_agent.agents.supervisor import SUPERVISOR_DECISION_ADAPTER, supervisor_system_prompt
from xhs_agent.llm import ChatMessage, ChatResponse, ToolCall
from xhs_agent.models import (
    AgentDefinition, ArtifactReference, AskUserDecision, ContentRequest, DelegateDecision,
    DelegationRecord, DelegationResult, DelegationTask, FinishDecision, SupervisorContext, UserProfile,
)
from xhs_agent.skills import SkillLibrary


def artifact(**changes):
    return ArtifactReference(**{
        "task_id": "task-1", "artifact_id": "draft-1", "kind": "draft", "version": 1,
        "summary": "基于用户素材生成的第一版草稿", **changes,
    })


def task(**changes):
    return DelegationTask(**{
        "task_id": "task-1", "delegation_id": "delegation-1", "target_agent": "review",
        "instruction": "核对草稿与用户要求，指出证据不足的部分", "input_artifacts": (artifact(),),
        "acceptance_criteria": ("列出具体问题与建议",), **changes,
    })


def result(**changes):
    return DelegationResult(**{
        "task_id": "task-1", "delegation_id": "delegation-1", "agent_id": "review",
        "status": "completed", "summary": "完成检查，建议补充素材来源", **changes,
    })


def context(registry, **changes):
    return SupervisorContext(**{
        "task_id": "task-1", "request": ContentRequest(goal="整理我的周末出游笔记"),
        "profile": UserProfile(), "available_agents": registry.catalog(), "artifacts": (artifact(),), **changes,
    })


@pytest.fixture
def registry():
    return build_agent_registry(SkillLibrary())


def test_contracts_and_all_decisions_round_trip_json(registry):
    delegation = task(materials=("周六走过江边步道",))
    completed = result(artifacts=(artifact(kind="review", artifact_id="review-1"),))
    record = DelegationRecord(task=delegation, result=completed)
    ctx = context(registry, history=(record,))
    assert SupervisorContext.model_validate_json(ctx.model_dump_json()) == ctx
    for decision in (
        DelegateDecision(action="delegate", reason="先检查现有草稿", task=delegation),
        AskUserDecision(action="ask_user", reason="缺少真实素材", question="实际走过哪些路段？",
                        missing_information=("实际路线",)),
        FinishDecision(action="finish", reason="已交付所需草稿", summary="草稿待用户审核", artifacts=(artifact(),)),
    ):
        parsed = registry.validate_decision(decision.model_dump_json(), context(registry))
        assert parsed == decision
    schema = SUPERVISOR_DECISION_ADAPTER.json_schema()
    assert set(schema["discriminator"]["mapping"]) == {"delegate", "ask_user", "finish"}
    assert "完成条件" in supervisor_system_prompt()


@pytest.mark.parametrize("changes", [
    {"instruction": "  "}, {"acceptance_criteria": ()}, {"acceptance_criteria": ("  ",)},
    {"target_agent": "publishing"}, {"target_agent": "supervisor"},
    {"target_agent": "arbitrary.module.Agent"}, {"tool_names": ["publish"]},
    {"input_artifacts": (artifact(task_id="other"),)}, {"input_artifacts": (artifact(), artifact())},
])
def test_invalid_delegations_are_rejected(changes):
    with pytest.raises(ValidationError):
        task(**changes)


@pytest.mark.parametrize("version", [0, -1, "1", True])
def test_artifact_version_is_a_positive_integer(version):
    with pytest.raises(ValidationError):
        artifact(version=version)


@pytest.mark.parametrize("changes", [
    {"status": "needs_input"}, {"status": "failed"}, {"status": "unknown"},
    {"missing_information": ("缺少照片",)}, {"failure_reason": "连接失败"},
    {"status": "failed", "failure_reason": " "},
    {"artifacts": (artifact(task_id="other"),)},
])
def test_result_status_requires_consistent_details(changes):
    with pytest.raises(ValidationError):
        result(**changes)
    assert result(status="needs_input", missing_information=("缺少照片",)).status == "needs_input"
    assert result(status="failed", failure_reason="检索接口不可用").status == "failed"


@pytest.mark.parametrize("changes", [
    {"task_id": "other"}, {"delegation_id": "other"}, {"agent_id": "creation"},
])
def test_results_cannot_be_attached_to_another_delegation(registry, changes):
    with pytest.raises(ValueError, match="不匹配"):
        registry.validate_result(task(), result(**changes))
    with pytest.raises(ValidationError, match="不匹配"):
        DelegationRecord(task=task(), result=result(**changes))


def test_context_rejects_mixed_tasks_and_duplicate_delegations(registry):
    record = DelegationRecord(task=task(), result=result())
    for changes in ({"artifacts": (artifact(task_id="other"),)}, {"history": (record, record)},
                    {"task_id": "other", "artifacts": (), "history": (record,)},
                    {"available_agents": registry.catalog() * 2}):
        with pytest.raises(ValidationError):
            context(registry, **changes)


def test_failed_delegation_cannot_repeat_without_changed_inputs_or_user_reply(registry):
    previous = task()
    record = DelegationRecord(task=previous, result=result(status="failed", failure_reason="输出不满足要求"))
    repeated = DelegateDecision(action="delegate", reason="重试", task=previous.model_copy(update={"delegation_id": "new"}))
    with pytest.raises(ValueError, match="不能原样重复"):
        registry.validate_decision(repeated, context(registry, history=(record,)))
    assert registry.validate_decision(repeated, context(registry, history=(record,), user_messages=("已修复配置",))) == repeated
    changed = repeated.model_copy(update={"task": repeated.task.model_copy(update={"instruction": "缩短结果并保留来源"})})
    assert registry.validate_decision(changed, context(registry, history=(record,))) == changed


def test_decisions_cannot_reuse_history_or_invent_artifacts(registry):
    record = DelegationRecord(task=task(), result=result())
    with pytest.raises(ValueError, match="委派 ID"):
        registry.validate_decision(DelegateDecision(action="delegate", reason="重做", task=task()),
                                   context(registry, history=(record,)))
    for ref in (artifact(version=2), artifact(artifact_id="invented"), artifact(summary="篡改的摘要")):
        with pytest.raises(ValueError, match="已有的产物"):
            registry.validate_decision(DelegateDecision(action="delegate", reason="审核",
                                                       task=task(input_artifacts=(ref,))), context(registry))
    with pytest.raises(ValueError, match="当前任务"):
        registry.validate_decision(DelegateDecision(action="delegate", reason="审核",
                                                   task=task(task_id="other", input_artifacts=())), context(registry))
    with pytest.raises(ValueError, match="已有的产物"):
        registry.validate_decision(FinishDecision(action="finish", reason="完成", summary="已交付",
                                                 artifacts=(artifact(task_id="other"),)), context(registry))
    with pytest.raises(ValueError, match="不能重复"):
        registry.validate_decision(FinishDecision(action="finish", reason="完成", summary="已交付",
                                                 artifacts=(artifact(), artifact())), context(registry))
    with pytest.raises(PermissionError, match="可用目录"):
        registry.validate_decision(DelegateDecision(action="delegate", reason="审核", task=task()),
                                   context(registry, available_agents=()))
    with pytest.raises(ValidationError):
        registry.validate_decision({"action": "publish", "account": "main"}, context(registry))


def test_registry_exposes_only_enabled_delegatable_agents(registry):
    assert {item.agent_id for item in registry.catalog()} == {"ideation", "research", "creation", "review"}
    assert len(registry.catalog(delegatable_only=False)) == 6
    assert not registry.definition("publishing").delegatable
    assert not registry.definition("supervisor").delegatable
    assert registry.tools_for("supervisor") == ()
    assert registry.skills_for("supervisor").catalog() == []
    with pytest.raises(PermissionError):
        registry.definition("arbitrary.module.Agent")
    with pytest.raises(RuntimeError, match="尚未绑定"):
        registry.agent("review")
    for role in ("supervisor", "publishing"):
        with pytest.raises(ValidationError):
            AgentDefinition(agent_id=role, description="角色", responsibilities=("职责",), delegatable=True)


def test_disabled_or_unregistered_agent_cannot_be_delegated():
    registry = AgentRegistry(SkillLibrary())
    with pytest.raises(PermissionError):
        registry.validate_delegation(task())
    registry.register(AgentDefinition(agent_id="review", description="审核", responsibilities=("审核",),
                                      delegatable=True, enabled=False))
    assert registry.catalog(delegatable_only=False) == ()
    with pytest.raises(PermissionError):
        registry.validate_delegation(task())


def test_registry_limits_skills_and_rechecks_revocation(tmp_path):
    library = SkillLibrary.initialize(tmp_path / "skills")
    source = tmp_path / "demo"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\nInstructions", encoding="utf-8")
    library.import_skill(source, agents=["ideation"])
    registry = AgentRegistry(library)
    agent = SkillEnabledAgent()
    definition = AgentDefinition(agent_id="ideation", description="选题", responsibilities=("选题",),
                                 skill_ids=("demo",), tool_names=("load_skill",), delegatable=True)
    registry.register(definition, agent)
    assert registry.agent("ideation") is agent
    assert agent.allowed_tools == ("load_skill",)
    assert agent.skills.load("demo").instructions.endswith("Instructions")
    library.revoke("ideation", "demo")
    assert agent.skills.catalog() == []
    with pytest.raises(PermissionError):
        agent.skills.load("demo")
    # 新的角色授权不能自动扩大已经绑定的注册表范围。
    library.grant("review", "demo")
    review = SkillEnabledAgent()
    registry.register(AgentDefinition(agent_id="review", description="审核", responsibilities=("审核",)), review)
    with pytest.raises(PermissionError):
        review.skills.load("demo")
    with pytest.raises(ValueError, match="已注册"):
        registry.register(definition)
    with pytest.raises(ValueError, match="独立 Agent"):
        registry.register(AgentDefinition(agent_id="creation", description="创作", responsibilities=("创作",)), agent)
    with pytest.raises(PermissionError, match="扩大"):
        registry.register(AgentDefinition(agent_id="creation", description="创作", responsibilities=("创作",),
                                          skill_ids=("demo",)))
    with pytest.raises(ValueError, match="已实现"):
        registry.register(AgentDefinition(agent_id="creation", description="创作", responsibilities=("创作",),
                                          tool_names=("publish",)))


def test_uniform_protocols_can_exchange_a_delegation(registry):
    class Coordinator:
        def decide(self, context):
            if not context.history:
                return DelegateDecision(action="delegate", reason="检查已有草稿", task=task())
            return FinishDecision(action="finish", reason="已检查", summary=context.history[-1].result.summary)

    class Worker:
        def run(self, task):
            return DelegationResult(task_id=task.task_id, delegation_id=task.delegation_id,
                                    agent_id=task.target_agent, status="completed", summary="已完成审核")

    coordinator, worker = Coordinator(), Worker()
    assert isinstance(coordinator, SupervisorAgent)
    assert isinstance(worker, DelegatedAgent)
    decision = registry.validate_decision(coordinator.decide(context(registry)), context(registry))
    outcome = worker.run(decision.task)
    registry.validate_result(decision.task, outcome)
    final_context = context(registry, history=(DelegationRecord(task=decision.task, result=outcome),))
    assert registry.validate_decision(coordinator.decide(final_context), final_context).action == "finish"


@pytest.mark.parametrize("tools", [(), ("load_skill",)])
def test_model_cannot_execute_an_unadvertised_skill_tool(tools):
    class Provider:
        def __init__(self):
            self.requests = []

        def chat(self, request):
            self.requests.append(request)
            message = (ChatMessage(role="assistant", tool_calls=[ToolCall(
                id="forbidden", name="read_skill_resource",
                arguments={"skill_id": "skill-topic-evaluator", "relative": "../../shared/scoring-dimensions.md"},
            )]) if len(self.requests) == 1 else ChatMessage(role="assistant", content=json.dumps({"ideas": [{
                "title": "周末路线", "why": "整理出游素材", "audience": "上班族", "angle": "路线分享",
            }]})))
            return ChatResponse(provider="offline", model="test", message=message,
                                finish_reason="tool_calls" if message.tool_calls else "stop")

    provider = Provider()
    agent = LLMIdeationAgent(provider)
    registry = AgentRegistry(SkillLibrary())
    registry.register(AgentDefinition(agent_id="ideation", description="选题", responsibilities=("选题",),
                                      skill_ids=("skill-topic-evaluator",), tool_names=tools, delegatable=True), agent)
    assert agent.ideate(ContentRequest(goal="出游"), UserProfile(), [])
    assert tuple(tool.name for tool in provider.requests[0].tools) == tools
    assert provider.requests[0].tool_choice == ("auto" if tools else "none")
    reply = provider.requests[1].messages[-1]
    assert reply.is_error and reply.tool_call_id == "forbidden"
    assert "instructions" not in reply.content


def test_task_service_assembles_children_and_runs_the_parent(service_factory):
    service = service_factory()
    try:
        assert service.agent_registry.agent("ideation").agent is service.ideation_agent
        assert service.ideation_agent.allowed_tools == ("load_skill", "read_skill_resource")
        assert service.publisher.allowed_tools == ()
        task_view = service.create_task("周末出游")
        assert task_view.status == "pending_review"
        assert service.collaboration(task_view.task_id)["history"]
    finally:
        service.close()
