"""主 Agent 决策节点 + 条件边；专业执行和人工输入后都回到主 Agent。"""
from typing import TypedDict
import re

from langgraph.graph import END, START, StateGraph

from .agents.base import Publisher
from .conversation import conversation_messages, decision_message
from .storage import Store
from .models import (
    DelegateDecision, DelegationRecord, DelegationTask, Draft, IdeaSelection,
    FinishDecision, ReviewReport, SupervisorContext,
)
from .team_store import TeamStore
from .flow_log import trace_node
from .human import HumanNodes
from .memory import current_context


class TeamState(TypedDict, total=False):
    task_id: str
    decision: dict
    history: list[dict]
    user_messages: list[str]
    turn: int
    rounds: int
    draft: dict
    replacement: dict
    summary: str
    conversation: list[dict]
    idea_selection: dict
    regeneration_source: dict
    idea_feedback: str
    selection_required: bool


class PublishState(TypedDict, total=False):
    """发布只需要任务标识与待发布草稿。"""

    task_id: str
    draft: dict


def build_publish_graph(publisher: Publisher, store: Store, on_event=None):
    def publish(state: PublishState) -> dict:
        task = store.get_task(state["task_id"])
        draft = Draft.model_validate(state["draft"])
        if not task or not task.draft or not task.draft.approved or task.current_version != draft.version:
            raise ValueError("发布需要当前版本的人工批准")
        draft = task.draft
        store.update_status(state["task_id"], "submitting")
        result = publisher.publish(state["task_id"], draft)
        store.save_stage(state["task_id"], "publish", result.model_dump(mode="json"), draft.version)
        if result.task_id != state["task_id"] or result.draft_version != draft.version:
            raise ValueError("发布回执与任务或草稿版本不一致")
        if result.status in {"submitted", "unknown", "failed"}:
            if result.published:
                raise ValueError("平台回执状态与 published 标记矛盾")
            # 明确发生在点击前的失败可以回到发布确认；不明结果结束图执行，等待人工核实。
            store.update_status(state["task_id"], "approved" if result.status == "failed" else result.status)
            return {}
        if result.published:
            store.record_publish(state["task_id"], draft)
        elif not (result.reason == "duplicate" and store.get_task(state["task_id"]).published_version == draft.version):
            raise RuntimeError(f"发布失败：{result.reason}")
        store.update_status(state["task_id"], "published")
        return {}
    builder = StateGraph(PublishState)
    builder.add_node("publish", trace_node("publish", publish, on_event or (lambda event: None), agent_id="publishing"))
    builder.add_edge(START, "publish")
    builder.add_edge("publish", END)
    return builder.compile()


def compile_graph(store, registry, supervisor, publisher, checkpointer, config, on_event=None):
    artifacts = TeamStore(store)

    def emit(event):
        if on_event:
            on_event(event)

    def context(state):
        current = store.get_task(state["task_id"])
        # 当前可用产物只保留每类最新版本；历史仍记录之前的委派与结果。
        latest = {ref.kind: ref for ref in artifacts.artifacts(state["task_id"])}
        selection = IdeaSelection.model_validate(state["idea_selection"]) if state.get("idea_selection") else None
        if selection and selection.candidates != latest.get("ideas"):
            selection = None
        if "ideas" in latest and selection is None:
            latest.pop("idea", None)
        return SupervisorContext(
            task_id=current.task_id, request=current.request, profile=current.profile,
            available_agents=registry.catalog(), artifacts=tuple(latest.values()),
            history=tuple(DelegationRecord.model_validate(record) for record in state.get("history", [])),
            user_messages=tuple(state.get("user_messages", [])),
            remaining_rounds=(max(0, config.team_max_rounds - state.get("rounds", 0))
                              if config.team_max_rounds is not None else None),
            idea_selection=selection,
            long_term_preferences=tuple(current_context().preferences if current_context() else
                                        store.preferences(store.memory_scope(current.task_id))["preferences"]),
            control_state={"status": current.status, "current_version": current.current_version,
                           "approved": bool(current.draft and current.draft.approved),
                           "approved_version": current.current_version if current.draft and current.draft.approved else None,
                           "pending_revision": next((text for text in reversed(state.get("user_messages", []))
                                                     if (match := re.match(r"修改 v(\d+)：", text))
                                                     and current.current_version <= int(match.group(1))), None)},
            idea_feedback=state.get("idea_feedback") if state.get("regeneration_source") == (
                latest["ideas"].model_dump(mode="json") if "ideas" in latest else None) else None,
        )

    def needs_selection(state):
        ctx = context(state)
        return any(ref.kind == "ideas" for ref in ctx.artifacts) and ctx.idea_selection is None and not ctx.idea_feedback

    def selection_update(state):
        ref = next(ref for ref in context(state).artifacts if ref.kind == "ideas")
        messages = conversation_messages(state)
        message_id = f"selection-request:{ref.artifact_id}:{ref.version}"
        if not any(item["id"] == message_id for item in messages):
            messages.append({"id": message_id, "role": "assistant", "kind": "select_idea",
                             "content": "候选选题已准备好。请选择一个选题，或说明要求后重新生成。"})
        return {"selection_required": True, "conversation": messages}

    def validate_decision(value, ctx):
        decision = registry.validate_decision(value, ctx)
        if isinstance(decision, FinishDecision):
            current = store.get_task(ctx.task_id)
            if current.draft:
                latest = {ref.kind: ref for ref in ctx.artifacts}
                report = latest.get("review")
                if report is None or report.version != current.current_version:
                    raise ValueError("交付草稿前需要委派 review 检查当前版本")
                if ReviewReport.model_validate(artifacts.resolve(report)).draft_version != current.current_version:
                    raise ValueError("审核产物正文与当前草稿版本不一致")
                if not any(ref.kind == "draft" and ref.version == current.current_version for ref in decision.artifacts):
                    raise ValueError("结束决策必须引用当前版本的 draft")
                if ctx.control_state["pending_revision"]:
                    raise ValueError("用户要求的修改尚未产生新版本")
        return decision

    if hasattr(supervisor, "validate_decision"):
        supervisor.validate_decision = validate_decision

    def coordinate(state):
        # 也保护升级前已经停在 supervisor 的检查点；此处不调用模型。
        if needs_selection(state):
            return selection_update(state)
        if config.team_max_rounds is not None and state.get("rounds", 0) >= config.team_max_rounds:
            raise RuntimeError("主 Agent 协作轮次已达上限；请检查执行记录或提高 team_max_rounds 后重试")
        ctx = context(state)
        turn = state.get("turn", 0)
        cached = store.get_stage(ctx.task_id, "team_decision", turn)
        decision = validate_decision(cached if cached is not None else supervisor.decide(ctx), ctx)
        encoded = decision.model_dump(mode="json")
        store.save_stage(ctx.task_id, "team_decision", encoded, turn)
        store.update_status(ctx.task_id, "coordinating")
        emit({"kind": "supervisor_decision", "message": "主 Agent 已作出协作决策", "action": decision.action,
              "turn": turn, "cached": cached is not None, "agent_id": "supervisor",
              "delegation_id": decision.task.delegation_id if decision.action == "delegate" else None,
              "idea_index": decision.task.idea_index if decision.action == "delegate" else None,
              "question": decision.question if decision.action == "ask_user" else None})
        return {"selection_required": False, "decision": encoded, "turn": turn + 1, "rounds": state.get("rounds", 0) + 1,
                "conversation": [*conversation_messages(state), decision_message(encoded, turn)]}

    def execute(state):
        task = DelegationTask.model_validate(state["decision"]["task"])
        # 若选题结果已经落库但图检查点提交失败，先补齐本次委派历史，再进入人工节点。
        if needs_selection(state) and task.target_agent != "ideation":
            return selection_update(state)
        registry.validate_delegation(task)
        # 重放旧检查点时同样不能绕过人工确认或更换用户选定的序号。
        ctx = context(state)
        if ctx.idea_selection or ctx.idea_feedback:
            # validate_decision 的委派 ID 检查只用于新决策；当前待执行任务尚未记入历史。
            registry.validate_decision(DelegateDecision(action="delegate", reason="执行前校验", task=task), ctx)
        for ref in task.input_artifacts:
            artifacts.resolve(ref)
        _, result = artifacts.begin(task)
        emit({"kind": "delegation_started", "message": "开始执行专业 Agent 委派",
              "agent_id": task.target_agent, "delegation_id": task.delegation_id, "cached": result is not None,
              "instruction": task.instruction, "acceptance_criteria": list(task.acceptance_criteria),
              "idea_index": task.idea_index, "input_artifacts": [ref.model_dump(mode="json") for ref in task.input_artifacts]})
        if result is None:
            worker = registry.agent(task.target_agent)
            result = worker.run(task)
            result = registry.validate_result(task, result)
            artifacts.finish(task, result)
        record = DelegationRecord(task=task, result=result, user_message_count=len(state.get("user_messages", [])))
        history = [*state.get("history", []), record.model_dump(mode="json")]
        current = store.get_task(task.task_id)
        emit({"kind": "delegation_result", "message": "专业 Agent 已返回执行结果",
              "agent_id": task.target_agent, "delegation_id": task.delegation_id, "status": result.status,
              "summary": result.summary, "failure_reason": result.failure_reason,
              "missing_information": list(result.missing_information),
              "artifacts": [ref.model_dump(mode="json") for ref in result.artifacts]})
        return {"history": history, "draft": current.draft.model_dump(mode="json") if current.draft else {},
                **(selection_update(state) if needs_selection(state) else {"selection_required": False})}

    def finish(state):
        decision = FinishDecision.model_validate(state["decision"])
        task = store.get_task(state["task_id"])
        store.update_status(task.task_id, "pending_review" if task.draft else "completed")
        return {"summary": decision.summary,
                "draft": task.draft.model_dump(mode="json") if task.draft else {}}

    human = HumanNodes(store, artifacts, context, emit)
    builder = StateGraph(TeamState)
    builder.add_node("supervisor", trace_node("supervisor", coordinate, emit, agent_id="supervisor"))
    builder.add_node("execute_agent", trace_node("execute_agent", execute, emit))
    builder.add_node("ask_user", trace_node("ask_user", human.ask, emit), destinations=("supervisor", "cancelled"))
    builder.add_node("select_idea", trace_node("select_idea", human.select_idea, emit), destinations=("supervisor", "cancelled"))
    builder.add_node("finish", trace_node("finish", finish, emit, agent_id="supervisor"))
    builder.add_node("review", trace_node("review", human.human_gate("review"), emit), destinations=("supervisor", "replace_draft", "mark_approved", "cancelled"))
    builder.add_node("publish_gate", trace_node("publish_gate", human.human_gate("publish_gate"), emit), destinations=("supervisor", "replace_draft", "publish", "cancelled"))
    builder.add_node("replace_draft", trace_node("replace_draft", human.replace_draft, emit))
    builder.add_node("mark_approved", trace_node("mark_approved", human.mark_approved, emit))
    builder.add_node("publish", build_publish_graph(publisher, store, emit))
    builder.add_node("cancelled", trace_node("cancelled", human.cancelled, emit))
    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges("supervisor", lambda state: "select_idea" if state.get("selection_required") else state["decision"]["action"], {
        "delegate": "execute_agent", "ask_user": "ask_user", "finish": "finish", "select_idea": "select_idea",
    })
    builder.add_conditional_edges("execute_agent", lambda state: "select_idea" if state.get("selection_required") else "supervisor")
    builder.add_edge("replace_draft", "supervisor")
    builder.add_conditional_edges("finish", lambda state: "review" if state.get("draft") else "done",
                                  {"review": "review", "done": END})
    builder.add_edge("mark_approved", "publish_gate")
    builder.add_conditional_edges("publish", lambda state: (
        "retry" if store.get_task(state["task_id"]).status == "approved" else "done"
    ), {"retry": "publish_gate", "done": END})
    builder.add_edge("cancelled", END)
    return builder.compile(checkpointer=checkpointer)
