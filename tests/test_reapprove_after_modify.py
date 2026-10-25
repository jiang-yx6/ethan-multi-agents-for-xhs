"""测试修改后旧批准失效、重新批准后发布新版本。"""


def test_old_approval_invalidated_and_reapprove(service_factory):
    svc = service_factory()
    task = svc.create_task("10 分钟快手早餐")
    task_id = task.task_id
    assert task.current_version == 1
    assert task.status == "pending_review"

    # 批准 v1
    svc.approve(task_id)
    assert svc.status(task_id).status == "approved"

    # 提出修改意见 -> 生成 v2，回到待审核
    svc.request_changes(task_id, "希望增加热量标注")
    after = svc.status(task_id)
    assert after.status == "pending_review"
    assert after.current_version == 2

    # 旧批准已失效：v1 与 v2 当前都未批准
    hist = {h["version"]: h for h in after.history}
    assert hist[1]["approved"] is False
    assert hist[2]["approved"] is False

    # 重新批准 v2 并发布
    svc.approve(task_id)
    pub = svc.publish(task_id)
    assert pub.status == "published"
    assert pub.current_version == 2
    assert pub.published_version == 2

    hist2 = {h["version"]: h for h in pub.history}
    assert hist2[2]["approved"] is True
    assert hist2[1]["approved"] is False
    svc.close()
