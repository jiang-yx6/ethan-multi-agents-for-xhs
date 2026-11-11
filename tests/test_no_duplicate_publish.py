"""测试防止重复模拟发布。"""
import pytest


def test_duplicate_publish_rejected(service_factory):
    svc = service_factory()
    task = svc.create_task("咖啡探店")
    task_id = task.task_id
    svc.approve(task_id)
    first = svc.publish(task_id)
    assert first.status == "published"
    assert svc.store.publish_count(task_id) == 1

    # 再次通过服务发布应被拒绝（状态已非 approved）
    with pytest.raises(ValueError):
        svc.publish(task_id)

    # 直接调用 Mock 发布器也应幂等，不产生第二条记录
    result = svc.publisher.publish(task_id, first.draft)
    assert result.published is False
    assert result.reason == "duplicate"

    assert svc.store.publish_count(task_id) == 1
    svc.close()
