"""人工选题门禁：候选版本、重启、重新生成和防止模型改选。"""
import pytest
from xhs_agent.models import ContentRequest
from xhs_agent.workflow import WorkflowConflictError


def source(pause):
    return {"interrupt_id": pause.id, "ideas_artifact_id": pause.value["ideas"]["artifact_id"],
            "ideas_version": pause.value["ideas"]["version"]}


def test_parent_delegates_ideation_and_uses_user_choice(service_factory):
    svc = service_factory()
    task = svc.create_task(ContentRequest(goal="护肤"))
    assert task.status == "waiting_input"
    assert task.current_version == 0

    ideas = svc.candidate_ideas(task.task_id)
    assert len(ideas) == 3

    chosen = ideas[1]
    pause = svc.workflow(task.task_id).interrupts[0]
    assert pause.kind == "select_idea"
    assert len(svc.collaboration(task.task_id)["history"]) == 1
    svc.close()
    svc = service_factory()
    after = svc.select_idea(task.task_id, 1, **source(pause))
    assert after.status == "pending_review"
    assert after.topic == chosen.title
    assert after.current_version == 1
    selection = svc.collaboration(task.task_id)["idea_selection"]
    assert selection["candidates"] == pause.value["ideas"] and selection["idea_index"] == 1
    selected = svc.artifact(task.task_id, selection["idea"]["artifact_id"], selection["idea"]["version"])
    assert selected["content"]["title"] == chosen.title
    svc.close()


@pytest.mark.parametrize("index", [-1, 99, "1", True, None])
def test_invalid_choice_does_not_resume_or_default_to_zero(service_factory, index):
    svc = service_factory()
    task = svc.create_task(ContentRequest(goal="生活分享"))
    ideas = svc.candidate_ideas(task.task_id)
    first = svc.workflow(task.task_id).interrupts[0]
    with pytest.raises(ValueError, match="有效"):
        svc.select_idea(task.task_id, index, **source(first))
    assert svc.status(task.task_id).status == "waiting_input"
    assert svc.status(task.task_id).current_version == 0
    assert svc.candidate_ideas(task.task_id) == ideas
    second = svc.workflow(task.task_id).interrupts[0]
    assert second.id == first.id
    with pytest.raises(WorkflowConflictError):
        svc.answer(task.task_id, "采用第一个方向")
    assert svc.select_idea(task.task_id, 0, **source(second)).status == "pending_review"
    svc.close()


def test_regeneration_requires_fresh_confirmation_and_rejects_old_source(service_factory):
    svc = service_factory()
    try:
        task = svc.create_task(ContentRequest(goal="生活分享"))
        first = svc.workflow(task.task_id).interrupts[0]
        with pytest.raises(ValueError):
            svc.regenerate_ideas(task.task_id, "  ", **source(first))
        task = svc.regenerate_ideas(task.task_id, "希望更多日常体验", **source(first))
        second = svc.workflow(task.task_id).interrupts[0]
        assert task.current_version == 0 and task.status == "waiting_input"
        assert second.kind == "select_idea" and second.id != first.id
        assert second.value["ideas"] != first.value["ideas"]
        history = svc.collaboration(task.task_id)["history"]
        assert [record["task"]["target_agent"] for record in history] == ["ideation", "ideation"]
        assert "希望更多日常体验" in history[-1]["task"]["instruction"]
        with pytest.raises(WorkflowConflictError):
            svc.select_idea(task.task_id, 0, **source(first))
        with pytest.raises(WorkflowConflictError):
            svc.select_idea(task.task_id, 0, **{**source(first), "interrupt_id": second.id})
        assert svc.select_idea(task.task_id, 2, **source(second)).status == "pending_review"
    finally:
        svc.close()


@pytest.mark.parametrize("bypass", ["change_index", "omit_selection"])
def test_supervisor_cannot_change_or_omit_confirmed_selection(tmp_path, bypass):
    from xhs_agent.agents.supervisor import MockSupervisorAgent
    from xhs_agent.config import AgentConfig
    from xhs_agent.service import TaskService

    class Supervisor(MockSupervisorAgent):
        calls = 0

        def decide(self, context):
            self.calls += 1
            if context.idea_selection:
                return {"action": "delegate", "reason": "模拟绕过选择", "task": {
                    "task_id": context.task_id, "target_agent": "creation", "instruction": "创作",
                    "acceptance_criteria": ["完成草稿"],
                    "input_artifacts": [context.idea_selection.candidates.model_dump(mode="json")]
                        if bypass == "change_index" else [],
                    "idea_index": 0 if bypass == "change_index" else None}}
            return super().decide(context)

    supervisor = Supervisor()
    config = AgentConfig(ideation_backend="mock", supervisor_backend="mock")
    svc = TaskService(tmp_path, config=config, supervisor_agent=supervisor)
    try:
        task = svc.create_task(ContentRequest(goal="生活分享"))
        assert supervisor.calls == 1
        pause = svc.workflow(task.task_id).interrupts[0]
        with pytest.raises(ValueError, match="用户确认"):
            svc.select_idea(task.task_id, 1, **source(pause))
        assert svc.status(task.task_id).current_version == 0
        assert len(svc.collaboration(task.task_id)["history"]) == 1
    finally:
        svc.close()
    svc = TaskService(tmp_path, config=config)
    try:
        assert svc.retry(task.task_id).topic == svc.candidate_ideas(task.task_id)[1].title
    finally:
        svc.close()


def test_selection_can_be_cancelled_without_creating_draft(service_factory):
    svc = service_factory()
    try:
        task = svc.create_task(ContentRequest(goal="健身"))
        assert svc.cancel(task.task_id).status == "cancelled"
        assert svc.status(task.task_id).draft is None
    finally:
        svc.close()


def test_committed_candidates_recover_history_before_selection(service_factory, monkeypatch):
    from xhs_agent.team_store import TeamStore
    svc = service_factory()
    original = TeamStore.finish
    interrupted = False

    def crash_after_commit(ledger, task, result):
        nonlocal interrupted
        original(ledger, task, result)
        if task.target_agent == "ideation" and not interrupted:
            interrupted = True
            raise RuntimeError("模拟候选落库后检查点中断")

    monkeypatch.setattr(TeamStore, "finish", crash_after_commit)
    try:
        task = svc.register_task(ContentRequest(goal="生活分享"))
        with pytest.raises(RuntimeError, match="检查点中断"):
            svc.start_task(task.task_id)
        assert svc.retry(task.task_id).status == "waiting_input"
        assert svc.workflow(task.task_id).interrupts[0].kind == "select_idea"
        assert len(svc.collaboration(task.task_id)["history"]) == 1
        assert len([ref for ref in TeamStore(svc.store).artifacts(task.task_id) if ref.kind == "ideas"]) == 1
    finally:
        svc.close()


def test_selection_after_an_existing_draft_requires_new_delivery(tmp_path):
    from xhs_agent.agents.supervisor import MockSupervisorAgent
    from xhs_agent.config import AgentConfig
    from xhs_agent.service import TaskService

    class Supervisor(MockSupervisorAgent):
        def decide(self, context):
            if any(ref.kind == "draft" for ref in context.artifacts) and not any(ref.kind == "ideas" for ref in context.artifacts):
                return {"action": "delegate", "reason": "先整理素材后再提供方向", "task": {
                    "task_id": context.task_id, "target_agent": "ideation", "instruction": "提供其他角度",
                    "acceptance_criteria": ["提供候选"]}}
            return super().decide(context)

    svc = TaskService(tmp_path, config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"),
                      supervisor_agent=Supervisor())
    try:
        task = svc.create_task("生活分享")
        assert task.current_version == 1 and task.status == "waiting_input"
        pause = svc.workflow(task.task_id).interrupts[0]
        selected = svc.candidate_ideas(task.task_id)[1]
        task = svc.select_idea(task.task_id, 1, **source(pause))
        assert task.current_version == 2 and task.status == "pending_review"
        assert task.topic == selected.title and task.review.draft_version == 2
    finally:
        svc.close()


def test_cannot_answer_after_draft_delivery(service_factory):
    svc = service_factory()
    task = svc.create_task("健身")
    svc.status(task.task_id)
    with pytest.raises(ValueError):
        svc.answer(task.task_id, "0")
    svc.close()


def test_brief_carries_style_and_sources(service_factory):
    svc = service_factory()
    task = svc.create_task("基金")
    after = svc.status(task.task_id)
    assert after.draft is not None
    assert after.brief.style == after.profile.style
    assert after.brief.sources
    assert after.brief.evidence
    assert all(e.is_mock for e in after.brief.evidence)
    assert "知识科普" not in after.draft.title
    assert after.review is not None
    svc.close()
