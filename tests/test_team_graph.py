"""主 Agent 的动态路由、真实模型协议、委派重放与人工门禁。"""
import json

import pytest

from xhs_agent.agents.creation import MockCreationAgent
from xhs_agent.agents.supervisor import LLMSupervisorAgent
from xhs_agent.config import AgentConfig, load_role_llm
from xhs_agent.llm import ChatMessage, ChatResponse
from xhs_agent.models import ArtifactReference, ContentRequest, DelegationResult, DraftContent, ReviewIssue, ReviewReport
from xhs_agent.service import TaskService
from xhs_agent.skills import SkillLibrary
from xhs_agent.team_store import TeamStore
from xhs_agent.workflow import WorkflowConflictError, allowed_actions


class CallbackSupervisor:
    def __init__(self, choose):
        self.choose = choose
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        return self.choose(context)


def delegate(ctx, role, refs=(), **extra):
    return {"action": "delegate", "reason": "根据当前上下文委派", "task": {
        "task_id": ctx.task_id, "target_agent": role, "input_artifacts": [r.model_dump(mode="json") for r in refs],
        "instruction": "满足本次需求，保留真实材料", "acceptance_criteria": ["明确指出仍需核实的部分"], **extra,
    }}


def ask():
    return {"action": "ask_user", "reason": "缺少实际路线", "question": "实际走过哪些路段？",
            "missing_information": ["实际路线"]}


def finish(ctx):
    return {"action": "finish", "reason": "当前任务已交付", "summary": "结果待人工审核",
            "artifacts": [ref.model_dump(mode="json") for ref in ctx.artifacts if ref.kind in {"draft", "review"}]}


def direct(ctx):
    latest = {ref.kind: ref for ref in ctx.artifacts}
    draft = latest.get("draft")
    if draft is None:
        return delegate(ctx, "creation")
    report = latest.get("review")
    if report is None or report.version != draft.version:
        return delegate(ctx, "review", [draft])
    return finish(ctx)


class CountingCreation(MockCreationAgent):
    def __init__(self):
        self.calls = []

    def create(self, brief, feedback=None, **kwargs):
        self.calls.append((brief, feedback, kwargs))
        return super().create(brief, feedback, **kwargs)


def make_service(root, **kwargs):
    return TaskService(root, config=kwargs.pop("config", AgentConfig(ideation_backend="mock", supervisor_backend="mock")),
                       supervisor_agent=kwargs.pop("supervisor_agent", CallbackSupervisor(direct)), **kwargs)


def test_default_is_team_and_can_skip_ideation_and_research(tmp_path):
    class Forbidden:
        def ideate(self, *args):
            raise AssertionError("不需要选题")

        def research(self, *args):
            raise AssertionError("不需要调研")

    events = []
    worker = CountingCreation()
    svc = make_service(tmp_path, ideation_agent=Forbidden(), research_agent=Forbidden(), creation_agent=worker,
                       on_event=events.append)
    try:
        task = svc.create_task(ContentRequest(goal="整理一篇路线笔记", topic="周末路线", needs_research=False,
                                              supplied_materials=["周六走过江边步道"]))
        assert task.workflow_version == 2 and task.status == "pending_review"
        assert "周六走过江边步道" in task.draft.body
        assert [r["task"]["target_agent"] for r in svc.collaboration(task.task_id)["history"]] == ["creation", "review"]
        assert len(worker.calls) == 1
        assert svc.agent_registry.agent("creation").run
        assert svc.workflow(task.task_id).next_nodes == ["review"]
        assert any(e.get("kind") == "supervisor_decision" for e in events)
        assert any(e.get("kind") == "delegation_result" for e in events)
        assert svc.approve(task.task_id).status == "approved"
        assert svc.publish(task.task_id).published
        assert svc.store.publish_count(task.task_id) == 1
    finally:
        svc.close()


def test_adapter_permissions_also_limit_the_underlying_agent(tmp_path):
    svc = make_service(tmp_path)
    try:
        adapter = svc.agent_registry.agent("ideation")
        adapter.bind_skills(SkillLibrary().for_agent("ideation", skill_ids=()))
        adapter.bind_tools(())
        with pytest.raises(PermissionError):
            svc.ideation_agent.skills.load("skill-topic-evaluator")
        assert svc.ideation_agent.allowed_tools == ()
    finally:
        svc.close()


def test_ask_user_pauses_and_resumes_after_restart_without_reasking(tmp_path):
    first = CallbackSupervisor(lambda ctx: ask())
    svc = make_service(tmp_path, supervisor_agent=first)
    task = svc.create_task("路线")
    pause = svc.workflow(task.task_id).interrupts[0]
    assert task.status == "waiting_input" and pause.kind == "ask_user"
    assert pause.value["question"] == "实际走过哪些路段？"
    assert allowed_actions(task, svc.workflow(task.task_id)) == ["answer", "cancel"]
    assert len(first.contexts) == 1
    svc.close()
    resumed = CallbackSupervisor(direct)
    svc = make_service(tmp_path, supervisor_agent=resumed)
    try:
        with pytest.raises(WorkflowConflictError):
            svc.answer(task.task_id, "江边", interrupt_id="old-interrupt")
        with pytest.raises(ValueError):
            svc.answer(task.task_id, "  ", interrupt_id=pause.id)
        result = svc.answer(task.task_id, "江边步道与公园", interrupt_id=pause.id)
        assert result.status == "pending_review"
        assert len(first.contexts) == 1
        assert "江边步道与公园" in resumed.contexts[0].user_messages[0]
    finally:
        svc.close()


def test_ask_user_can_be_cancelled(tmp_path):
    svc = make_service(tmp_path, supervisor_agent=CallbackSupervisor(lambda ctx: ask()))
    try:
        task = svc.create_task("路线")
        assert svc.cancel(task.task_id).status == "cancelled"
        assert svc.workflow(task.task_id).status == "completed"
    finally:
        svc.close()


def test_supervisor_can_delegate_research_after_review_and_then_revise(tmp_path):
    class Reviewer:
        def review(self, request, profile, brief, draft):
            return ReviewReport(draft_version=draft.version, issues=[ReviewIssue(
                message="需要补充资料", target="research")] if draft.version == 1 else [])

    def choose(ctx):
        latest = {ref.kind: ref for ref in ctx.artifacts}
        draft = latest.get("draft")
        if draft is None:
            return delegate(ctx, "creation")
        report = latest.get("review")
        if report is None or report.version != draft.version:
            return delegate(ctx, "review", [draft])
        if draft.version == 1:
            if "brief" not in latest:
                return delegate(ctx, "research", [draft])
            return delegate(ctx, "creation", [latest["brief"], draft], instruction="补充资料后修改")
        return finish(ctx)

    svc = make_service(tmp_path, supervisor_agent=CallbackSupervisor(choose), review_agent=Reviewer())
    try:
        task = svc.create_task("路线")
        roles = [r["task"]["target_agent"] for r in svc.collaboration(task.task_id)["history"]]
        assert roles == ["creation", "review", "research", "creation", "review"]
        assert task.current_version == 2 and task.status == "pending_review"
        assert task.review.passed and not task.draft.approved
    finally:
        svc.close()


def test_missing_worker_inputs_return_to_supervisor(tmp_path):
    def choose(ctx):
        if not ctx.history:
            return delegate(ctx, "review")
        assert ctx.history[-1].result.status == "needs_input"
        return ask()

    svc = make_service(tmp_path, supervisor_agent=CallbackSupervisor(choose))
    try:
        task = svc.create_task("路线")
        assert task.status == "waiting_input"
        record = svc.collaboration(task.task_id)["history"][0]
        assert record["result"]["missing_information"]
    finally:
        svc.close()


@pytest.mark.parametrize("failure_point", ["draft", "result", "decision"])
def test_committed_work_is_not_repeated_after_checkpoint_failure(tmp_path, monkeypatch, failure_point):
    worker = CountingCreation()
    supervisor = CallbackSupervisor(direct)
    svc = make_service(tmp_path, creation_agent=worker, supervisor_agent=supervisor)
    task = svc.register_task("路线")
    failed = False

    if failure_point == "draft":
        target, method = svc.store, "save_draft"
    elif failure_point == "result":
        target, method = TeamStore, "finish"
    else:
        target, method = svc.store, "save_stage"
    original = getattr(target, method)

    def crash(*args, **kwargs):
        nonlocal failed
        value = original(*args, **kwargs)
        should_fail = failure_point != "decision" or args[1] == "team_decision"
        if not failed and should_fail:
            failed = True
            raise RuntimeError("模拟提交成功后中断")
        return value

    monkeypatch.setattr(target, method, crash)
    try:
        with pytest.raises(RuntimeError, match="模拟提交成功"):
            svc.start_task(task.task_id)
        result = svc.retry(task.task_id)
        assert result.status == "pending_review" and result.current_version == 1
        assert len(worker.calls) == 1
        assert len(supervisor.contexts) == 3
        assert len(svc.collaboration(task.task_id)["history"]) == 2
    finally:
        svc.close()


def test_round_budget_stops_the_team_and_can_resume_with_larger_budget(tmp_path):
    def choose(ctx):
        return delegate(ctx, "ideation") if len(ctx.history) < 2 else finish(ctx)

    supervisor = CallbackSupervisor(choose)
    svc = make_service(tmp_path, supervisor_agent=supervisor,
                       config=AgentConfig(ideation_backend="mock", team_max_rounds=2, supervisor_backend="mock"))
    task = svc.register_task("路线")
    with pytest.raises(RuntimeError, match="轮次已达上限"):
        svc.start_task(task.task_id)
    assert len(supervisor.contexts) == 2
    svc.close()
    svc = make_service(tmp_path, supervisor_agent=supervisor,
                       config=AgentConfig(ideation_backend="mock", team_max_rounds=3, supervisor_backend="mock"))
    try:
        assert svc.retry(task.task_id).status == "completed"
        assert svc.collaboration(task.task_id)["summary"]
    finally:
        svc.close()


def test_changes_and_manual_replacement_return_to_supervisor(tmp_path):
    svc = TaskService(tmp_path, config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    try:
        task = svc.create_task("路线")
        assert svc.approve(task.task_id).draft.approved
        revised = svc.request_changes(task.task_id, "补充交通方式")
        assert revised.current_version == 2 and not revised.draft.approved
        assert "补充交通方式" in revised.draft.body
        replacement = svc.replace_draft(task.task_id, DraftContent(title="我的路线", body="真实路线记录"))
        assert replacement.current_version == 3 and replacement.status == "pending_review"
        assert replacement.review.draft_version == 3
        assert replacement.draft.body == "真实路线记录"
        assert not any(row["approved"] for row in replacement.history)
    finally:
        svc.close()






class ModelProvider:
    def __init__(self):
        self.requests = []
        self.closed = False

    def chat(self, request):
        self.requests.append(request)
        payload = json.loads(request.messages[1].content)
        from xhs_agent.models import SupervisorContext
        ctx = SupervisorContext.model_validate(payload)
        decision = direct(ctx)
        return ChatResponse(provider="offline", model="supervisor", finish_reason="stop",
                            message=ChatMessage(role="assistant", content=json.dumps(decision, ensure_ascii=False)))

    def close(self):
        self.closed = True


def test_real_supervisor_model_loop_uses_context_and_no_tools(tmp_path):
    provider = ModelProvider()
    svc = make_service(tmp_path, supervisor_agent=LLMSupervisorAgent(provider))
    try:
        task = svc.create_task("路线")
        assert task.status == "pending_review"
        assert len(provider.requests) == 3
        assert all(not req.tools and req.tool_choice == "none" for req in provider.requests)
        first, last = [json.loads(req.messages[1].content) for req in (provider.requests[0], provider.requests[-1])]
        assert first["history"] == [] and len(last["history"]) == 2
        assert first["remaining_rounds"] == 20
        assert last["remaining_rounds"] == 18
    finally:
        svc.close()
    assert not provider.closed


def test_model_can_repair_a_semantically_invalid_finish_decision(tmp_path):
    class Provider(ModelProvider):
        def chat(self, request):
            response = super().chat(request)
            if len(self.requests) == 2:
                response.message.content = json.dumps({
                    "action": "finish", "reason": "过早结束", "summary": "还没有审核", "artifacts": [],
                })
            return response

    provider = Provider()
    svc = make_service(tmp_path, supervisor_agent=LLMSupervisorAgent(provider))
    try:
        assert svc.create_task("路线").status == "pending_review"
        assert len(provider.requests) == 4
        assert "检查当前版本" in provider.requests[2].messages[-1].content
    finally:
        svc.close()


def test_supervisor_cannot_finish_with_an_unreviewed_draft(tmp_path):
    def choose(ctx):
        return delegate(ctx, "creation") if not ctx.artifacts else finish(ctx)

    svc = make_service(tmp_path, supervisor_agent=CallbackSupervisor(choose))
    task = svc.register_task("路线")
    try:
        with pytest.raises(ValueError, match="检查当前版本"):
            svc.start_task(task.task_id)
        assert not svc.status(task.task_id).draft.approved
        assert svc.store.publish_count(task.task_id) == 0
    finally:
        svc.close()


def test_artifact_references_and_delegation_ids_are_immutable(tmp_path):
    svc = make_service(tmp_path)
    try:
        task = svc.create_task("路线")
        artifacts = TeamStore(svc.store)
        ref = artifacts.artifacts(task.task_id)[0]
        payload = artifacts.resolve(ref)
        with pytest.raises(ValueError, match="不能覆盖"):
            artifacts.put_artifact(ref, {"changed": True})
        with pytest.raises(ValueError, match="引用被修改"):
            artifacts.resolve(ref.model_copy(update={"version": 99}))
        assert artifacts.resolve(ref) == payload
    finally:
        svc.close()


def test_material_references_and_new_materials_reach_creation_without_changing_brief(tmp_path):
    def choose(ctx):
        latest = {ref.kind: ref for ref in ctx.artifacts}
        if "brief" not in latest:
            return delegate(ctx, "research", [latest["material"]])
        if "draft" not in latest:
            return delegate(ctx, "creation", [latest["brief"], latest["material"]], materials=["新增素材：乘坐地铁到达"])
        return direct(ctx)

    svc = make_service(tmp_path, supervisor_agent=CallbackSupervisor(choose))
    try:
        task = svc.register_task(ContentRequest(goal="路线分享", topic="路线", needs_research=False))
        ledger = TeamStore(svc.store)
        ledger.put_artifact(ArtifactReference(task_id=task.task_id, artifact_id="user-material", kind="material",
                                              version=1, summary="实际走过的路线"), "周六走过江边步道")
        task = svc.start_task(task.task_id)
        assert "周六走过江边步道" in task.draft.body
        assert "乘坐地铁到达" in task.draft.body
        assert task.request.supplied_materials == []
        ref = next(ref for ref in ledger.artifacts(task.task_id) if ref.kind == "brief")
        assert ledger.resolve(ref)["supplied_materials"] == ["周六走过江边步道"]
    finally:
        svc.close()


def test_custom_worker_failure_is_a_result_the_supervisor_can_handle(tmp_path):
    class Worker:
        def run(self, task):
            return DelegationResult(task_id=task.task_id, delegation_id=task.delegation_id,
                                    agent_id=task.target_agent, status="failed", summary="未取得可用来源",
                                    failure_reason="指定来源没有相关资料")

    def choose(ctx):
        if not ctx.history:
            return delegate(ctx, "research")
        assert ctx.history[-1].result.status == "failed"
        return ask()

    svc = make_service(tmp_path, supervisor_agent=CallbackSupervisor(choose), delegated_agents={"research": Worker()})
    try:
        task = svc.create_task("路线")
        assert task.status == "waiting_input"
        assert svc.collaboration(task.task_id)["history"][0]["result"]["failure_reason"]
    finally:
        svc.close()


def test_supervisor_has_independent_config_and_requires_its_own_section(tmp_path):
    path = tmp_path / "llm.json"
    path.write_text(json.dumps({"ideation": {"provider": "openai", "model": "ideas"},
                                "supervisor": {"provider": "anthropic", "model": "coordinator"}}), encoding="utf-8")
    assert load_role_llm(path, "supervisor").model == "coordinator"
    path.write_text(json.dumps({"ideation": {"provider": "openai", "model": "ideas"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="supervisor"):
        load_role_llm(path, "supervisor")
    assert AgentConfig().team_max_rounds == 20
    svc = TaskService(tmp_path / "data")
    try:
        assert isinstance(svc.supervisor_agent, LLMSupervisorAgent)
    finally:
        svc.close()


def test_only_parent_child_graph_is_available(tmp_path):
    from pydantic import ValidationError
    from xhs_agent.cli import build_parser

    with pytest.raises(ValidationError):
        AgentConfig(orchestration="workflow")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["select", "task", "0"])
    svc = make_service(tmp_path)
    try:
        task = svc.register_task("新任务")
        with svc._open_graph(task.task_id) as graph:
            nodes = set(graph.get_graph().nodes)
        assert {"supervisor", "execute_agent", "ask_user", "finish"}.issubset(nodes)
        assert not {"ideation", "select_idea", "research", "create", "quality_review", "route_quality"} & nodes
    finally:
        svc.close()


def test_retired_checkpoint_data_can_be_read_but_not_resumed(tmp_path):
    svc = make_service(tmp_path)
    try:
        svc.store.create_task("retired-task", "历史主题", workflow_version=1)
        assert svc.status("retired-task").topic == "历史主题"
        assert svc.workflow("retired-task").status == "unsupported"
        assert svc.collaboration("retired-task")["mode"] == "unsupported"
        assert allowed_actions(svc.status("retired-task"), svc.workflow("retired-task")) == []
        with pytest.raises(WorkflowConflictError, match="旧图"):
            svc.start_task("retired-task")
        assert svc.status("retired-task").status == "pending"
        assert svc.store.publish_count("retired-task") == 0
    finally:
        svc.close()
