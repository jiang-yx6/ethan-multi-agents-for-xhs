"""测试选题（提出想法）环节。"""
import pytest


def test_create_generates_ideas_and_select(service_factory):
    svc = service_factory()
    task = svc.create_task("护肤")
    assert task.status == "ideating"
    assert task.current_version == 0

    ideas = svc.candidate_ideas(task.task_id)
    assert len(ideas) == 3

    chosen = ideas[1]
    after = svc.select_idea(task.task_id, 1)
    assert after.status == "pending_review"
    assert after.topic == chosen.title
    assert after.current_version == 1
    svc.close()


def test_select_out_of_range_keeps_task_waiting(service_factory):
    svc = service_factory()
    task = svc.create_task("理财")
    ideas = svc.candidate_ideas(task.task_id)
    with pytest.raises(ValueError, match="选题序号"):
        svc.select_idea(task.task_id, 99)
    assert svc.status(task.task_id).status == "ideating"
    assert svc.status(task.task_id).current_version == 0
    assert svc.candidate_ideas(task.task_id) == ideas
    svc.close()


def test_cannot_select_twice(service_factory):
    svc = service_factory()
    task = svc.create_task("健身")
    svc.select_idea(task.task_id, 0)
    with pytest.raises(ValueError):
        svc.select_idea(task.task_id, 0)
    svc.close()


def test_brief_carries_style_and_sources(service_factory):
    svc = service_factory()
    task = svc.create_task("基金")
    after = svc.select_idea(task.task_id, 0)
    assert after.draft is not None
    assert after.brief.style == after.profile.style
    assert after.brief.sources
    assert after.brief.evidence
    assert all(e.is_mock for e in after.brief.evidence)
    assert "知识科普" not in after.draft.title
    assert after.review is not None
    svc.close()
