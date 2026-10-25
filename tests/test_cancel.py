"""测试取消操作。"""
import pytest


def test_cancel_pending_review(service_factory):
    svc = service_factory()
    task = svc.create_task("新手健身计划")
    assert task.status == "pending_review"
    cancelled = svc.cancel(task.task_id)
    assert cancelled.status == "cancelled"
    assert cancelled.published is False
    svc.close()


def test_cancel_after_approve(service_factory):
    svc = service_factory()
    task = svc.create_task("存钱入门")
    svc.approve(task.task_id)
    assert svc.status(task.task_id).status == "approved"
    cancelled = svc.cancel(task.task_id)
    assert cancelled.status == "cancelled"
    svc.close()


def test_cannot_approve_after_cancel(service_factory):
    svc = service_factory()
    task = svc.create_task("三天两夜旅行攻略")
    svc.cancel(task.task_id)
    with pytest.raises(ValueError):
        svc.approve(task.task_id)
    svc.close()
