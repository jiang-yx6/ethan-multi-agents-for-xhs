"""从 LangGraph 检查点描述人工暂停，并统一恢复操作的校验规则。"""
from __future__ import annotations

from .models import TaskView, WorkflowInterrupt, WorkflowView


class WorkflowConflictError(ValueError):
    """客户端操作与当前检查点不匹配。"""


GATES = {
    "ask_user": ("waiting_input", ["answer", "cancel"]),
    "review": ("pending_review", ["draft", "changes", "message", "approve", "cancel"]),
    "publish_gate": ("approved", ["draft", "changes", "message", "publish", "preflight", "export", "cancel"]),
}
TERMINAL = {"published", "submitted", "unknown", "cancelled", "completed"}
STEP_MESSAGES = {
    "supervisor": "主 Agent 已决定下一步", "execute_agent": "本次委派已执行",
    "ask_user": "用户补充信息已接收", "finish": "协作结果已交付",
    "replace_draft": "新版本已保存",
    "mark_approved": "当前版本已批准", "publish_gate": "已确认本次发布操作",
    "publish": "发布步骤已结束，请查看实际回执", "cancelled": "创作已取消",
}


def describe_workflow(task_id: str, snapshot) -> WorkflowView:
    checkpoint_id = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
    pauses = []
    for task in snapshot.tasks:
        for pause in task.interrupts:
            raw = pause.value if isinstance(pause.value, dict) else {}
            # 草稿及审核正文由业务 API 提供；这里仅暴露人工交互的必要信息。
            value = {key: raw[key] for key in ("kind", "task_id", "version", "hint", "question", "missing_information") if key in raw}
            kind = raw.get("kind", task.name)
            actions = GATES.get(kind, (None, []))[1] if task.name == kind else []
            pauses.append(WorkflowInterrupt(id=pause.id, node=task.name, kind=kind,
                                            value=value, actions=actions))
    errors = [str(task.error) for task in snapshot.tasks if task.error]
    status = ("failed" if errors else "waiting_input" if pauses else
              "not_started" if not checkpoint_id else "ready" if snapshot.next else "completed")
    return WorkflowView(thread_id=task_id, checkpoint_id=checkpoint_id, status=status,
                        next_nodes=list(snapshot.next), interrupts=pauses,
                        error="图节点执行失败，请检查任务错误后重试" if errors else None)


def gate_status(task: TaskView, workflow: WorkflowView) -> str | None:
    """只有单个人工门禁与业务草稿完全对应时，才允许恢复显示状态。"""
    if workflow.status != "waiting_input" or len(workflow.interrupts) != 1:
        return None
    pause = workflow.interrupts[0]
    if pause.kind not in GATES or pause.node != pause.kind:
        return None
    if pause.kind == "ask_user":
        return "waiting_input"
    if not task.draft or pause.value.get("version") != task.current_version:
        return None
    if pause.kind == "publish_gate" and not task.draft.approved:
        return None
    return GATES[pause.kind][0]


def allowed_actions(task: TaskView, workflow: WorkflowView, *, active: bool = False) -> list[str]:
    if active:
        return []
    if task.status in {"submitted", "unknown"}:
        return ["confirm", "export"]
    if task.status == "published":
        return ["export"]
    if task.status == "cancelled":
        return []
    if workflow.status == "unsupported":
        return ["export"] if task.status == "approved" and task.draft and task.draft.approved else []
    if task.status in {"failed", "submitting"}:
        return ["retry"]
    if workflow.status == "not_started" and task.status == "pending":
        return ["retry"]
    target = gate_status(task, workflow)
    if target == task.status:
        return list(workflow.interrupts[0].actions)
    if target is not None:
        return ["reconcile"]
    return []
