"""主 Agent 决策节点 + 条件边；专业执行和人工输入后都回到主 Agent。"""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .agents.base import Publisher
from .artifacts import archive_task
from .conversation import conversation_messages, decision_message
from .storage import Store
from .models import (
    ArtifactReference, AskUserDecision, DelegationRecord, DelegationTask, Draft, DraftContent,
    FinishDecision, ReviewReport, SupervisorContext,
)
from .team_store import TeamStore
from .flow_log import trace_node


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
        return SupervisorContext(
            task_id=current.task_id, request=current.request, profile=current.profile,
            available_agents=registry.catalog(), artifacts=tuple(latest.values()),
            history=tuple(DelegationRecord.model_validate(record) for record in state.get("history", [])),
            user_messages=tuple(state.get("user_messages", [])),
            remaining_rounds=max(0, config.team_max_rounds - state.get("rounds", 0)),
        )

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
                changes = [message for message in ctx.user_messages if message.startswith("修改 v")]
                if changes:
                    version = int(changes[-1].split("：", 1)[0].removeprefix("修改 v"))
                    if current.current_version <= version:
                        raise ValueError("用户要求的修改尚未产生新版本")
        return decision

    if hasattr(supervisor, "validate_decision"):
        supervisor.validate_decision = validate_decision

    def coordinate(state):
        if state.get("rounds", 0) >= config.team_max_rounds:
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
        return {"decision": encoded, "turn": turn + 1, "rounds": state.get("rounds", 0) + 1,
                "conversation": [*conversation_messages(state), decision_message(encoded, turn)]}

    def execute(state):
        task = DelegationTask.model_validate(state["decision"]["task"])
        registry.validate_delegation(task)
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
        return {"history": history, "draft": current.draft.model_dump(mode="json") if current.draft else {}}

    def ask(state):
        decision = AskUserDecision.model_validate(state["decision"])
        answer = interrupt({"kind": "ask_user", "task_id": state["task_id"],
                            "question": decision.question, "missing_information": list(decision.missing_information),
                            "hint": "answer / cancel"})
        if isinstance(answer, dict) and answer.get("action") == "cancel":
            return Command(goto="cancelled")
        if not isinstance(answer, dict) or not isinstance(answer.get("answer"), str) or not answer["answer"].strip():
            raise ValueError("用户回复不能为空")
        message = f"问题：{decision.question}\n用户回复：{answer['answer'].strip()}"
        return Command(goto="supervisor", update={
            "user_messages": [*state.get("user_messages", []), message], "rounds": 0,
            "conversation": [*conversation_messages(state), {"id": f"user:{state['turn']}:answer",
                              "role": "user", "kind": "answer", "content": answer["answer"].strip()}],
        })

    def finish(state):
        decision = FinishDecision.model_validate(state["decision"])
        task = store.get_task(state["task_id"])
        store.update_status(task.task_id, "pending_review" if task.draft else "completed")
        return {"summary": decision.summary,
                "draft": task.draft.model_dump(mode="json") if task.draft else {}}

    def human_gate(kind):
        def node(state):
            version = state["draft"]["version"]
            answer = interrupt({"kind": kind, "task_id": state["task_id"], "version": version,
                                "hint": "approve / request_changes / replace_draft / cancel" if kind == "review"
                                else "publish / request_changes / replace_draft / cancel"})
            action = answer.get("action")
            if action == "cancel":
                return Command(goto="cancelled")
            if action == "approve" and kind == "review":
                return Command(goto="mark_approved")
            if action == "publish" and kind == "publish_gate":
                return Command(goto="publish")
            if action == "message":
                text = answer.get("text", "").strip()
                if not text:
                    raise ValueError("消息不能为空")
                store.revoke_approval(state["task_id"])
                draft = store.get_task(state["task_id"]).draft
                return Command(goto="supervisor", update={"rounds": 0,
                    "draft": draft.model_dump(mode="json"),
                    "user_messages": [*state.get("user_messages", []), f"对话 v{version}：{text}"],
                    "conversation": [*conversation_messages(state), {"id": f"user:{state['turn']}:message",
                                      "role": "user", "kind": "message", "content": text}]})
            if action == "request_changes":
                text = answer.get("comment", "").strip()
                if not text:
                    raise ValueError("修改意见不能为空")
                return Command(goto="supervisor", update={"rounds": 0, "user_messages": [
                    *state.get("user_messages", []), f"修改 v{version}：{text}",
                ], "conversation": [*conversation_messages(state), {"id": f"user:{state['turn']}:changes",
                                    "role": "user", "kind": "changes", "content": text}]})
            if action == "replace_draft":
                return Command(goto="replace_draft", update={"replacement": answer["draft"], "rounds": 0})
            raise ValueError("未知人工操作")
        return node

    def replace_draft(state):
        version = int(state["draft"]["version"]) + 1
        draft = store.get_draft(state["task_id"], version)
        if draft is None:
            content = DraftContent.model_validate(state["replacement"])
            draft = Draft(**content.model_dump(), version=version)
            store.save_draft(state["task_id"], draft)
        archive_task(store.get_task(state["task_id"]), store.db_path.parent / "outputs")
        artifacts.put_artifact(ArtifactReference(task_id=state["task_id"], artifact_id=f"manual:{draft.version}",
                                                 kind="draft", version=draft.version,
                                                 summary=f"用户替换的草稿 v{draft.version}：{draft.title}\n{draft.body[:1200]}"),
                               draft.model_dump(mode="json"))
        return {"draft": draft.model_dump(mode="json"), "replacement": {}}

    def mark_approved(state):
        store.approve_draft(state["task_id"], int(state["draft"]["version"]))
        return {}

    def cancelled(state):
        store.update_status(state["task_id"], "cancelled")
        return {}

    builder = StateGraph(TeamState)
    builder.add_node("supervisor", trace_node("supervisor", coordinate, emit, agent_id="supervisor"))
    builder.add_node("execute_agent", trace_node("execute_agent", execute, emit))
    builder.add_node("ask_user", trace_node("ask_user", ask, emit), destinations=("supervisor", "cancelled"))
    builder.add_node("finish", trace_node("finish", finish, emit, agent_id="supervisor"))
    builder.add_node("review", trace_node("review", human_gate("review"), emit), destinations=("supervisor", "replace_draft", "mark_approved", "cancelled"))
    builder.add_node("publish_gate", trace_node("publish_gate", human_gate("publish_gate"), emit), destinations=("supervisor", "replace_draft", "publish", "cancelled"))
    builder.add_node("replace_draft", trace_node("replace_draft", replace_draft, emit))
    builder.add_node("mark_approved", trace_node("mark_approved", mark_approved, emit))
    builder.add_node("publish", build_publish_graph(publisher, store, emit))
    builder.add_node("cancelled", trace_node("cancelled", cancelled, emit))
    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges("supervisor", lambda state: state["decision"]["action"], {
        "delegate": "execute_agent", "ask_user": "ask_user", "finish": "finish",
    })
    builder.add_edge("execute_agent", "supervisor")
    builder.add_edge("replace_draft", "supervisor")
    builder.add_conditional_edges("finish", lambda state: "review" if state.get("draft") else "done",
                                  {"review": "review", "done": END})
    builder.add_edge("mark_approved", "publish_gate")
    builder.add_conditional_edges("publish", lambda state: (
        "retry" if store.get_task(state["task_id"]).status == "approved" else "done"
    ), {"retry": "publish_gate", "done": END})
    builder.add_edge("cancelled", END)
    return builder.compile(checkpointer=checkpointer)
