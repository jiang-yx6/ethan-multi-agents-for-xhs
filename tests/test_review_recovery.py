"""测试审核的 SQLite 检查点恢复（进程重启后仍可继续审核）。"""


def test_review_survives_restart_and_approve(service_factory):
    svc = service_factory()
    task = svc.create_task("早C晚A护肤入门")
    task_id = task.task_id
    assert task.status == "pending_review"
    assert task.current_version == 1
    svc.close()

    # 模拟进程重启：用同一数据目录新建 service。
    svc2 = service_factory()
    assert svc2.status(task_id).status == "pending_review"
    approved = svc2.approve(task_id)
    assert approved.status == "approved"
    published = svc2.publish(task_id)
    assert published.status == "published"
    assert published.published is True
    assert published.published_version == 1
    svc2.close()
