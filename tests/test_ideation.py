"""由父 Agent 发起选题委派，并通过 ask_user 接收用户选择。"""
import pytest
from xhs_agent.models import ContentRequest


def test_parent_delegates_ideation_and_uses_user_choice(service_factory):
    svc = service_factory()
    task = svc.create_task(ContentRequest(goal="护肤"))
    assert task.status == "waiting_input"
    assert task.current_version == 0

    ideas = svc.candidate_ideas(task.task_id)
    assert len(ideas) == 3

    chosen = ideas[1]
    assert svc.workflow(task.task_id).interrupts[0].kind == "ask_user"
    after = svc.answer(task.task_id, "1")
    assert after.status == "pending_review"
    assert after.topic == chosen.title
    assert after.current_version == 1
    svc.close()


def test_invalid_choice_prompts_again(service_factory):
    svc = service_factory()
    task = svc.create_task(ContentRequest(goal="生活分享"))
    ideas = svc.candidate_ideas(task.task_id)
    first = svc.workflow(task.task_id).interrupts[0]
    svc.answer(task.task_id, "99", interrupt_id=first.id)
    assert svc.status(task.task_id).status == "waiting_input"
    assert svc.status(task.task_id).current_version == 0
    assert svc.candidate_ideas(task.task_id) == ideas
    second = svc.workflow(task.task_id).interrupts[0]
    assert second.id != first.id
    with pytest.raises(ValueError):
        svc.answer(task.task_id, "0", interrupt_id=first.id)
    assert svc.answer(task.task_id, "0", interrupt_id=second.id).status == "pending_review"
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
