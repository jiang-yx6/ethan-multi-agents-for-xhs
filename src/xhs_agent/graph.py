"""LangGraph 编排：研究 → 创作 → 人工审核（含发布/取消）。"""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .agents.base import CreationAgent, Publisher, ResearchAgent
from .models import Draft, ResearchBrief
from .storage import Store


class XhsState(TypedDict, total=False):
    """图状态。全部字段为 JSON 可序列化类型，保证检查点可持久化。"""

    task_id: str
    topic: str
    brief: dict
    draft: dict
    version: int
    feedback: str
    decision: dict
    phase: str


def _research_node(agent: ResearchAgent, store: Store):
    def node(state: XhsState) -> dict:
        brief = agent.research(state["topic"])
        store.update_status(state["task_id"], "researched")
        return {"brief": brief.model_dump(mode="json"), "phase": "researched"}

    return node


def _create_node(agent: CreationAgent, store: Store):
    def node(state: XhsState) -> dict:
        brief = ResearchBrief.model_validate(state["brief"])
        feedback = state.get("feedback") or None
        new_version = int(state.get("version", 0)) + 1
        draft = agent.create(brief, feedback=feedback)
        draft.version = new_version
        store.save_draft(state["task_id"], draft)  # 使旧批准失效并进入待审核
        return {
            "draft": draft.model_dump(mode="json"),
            "version": new_version,
            "phase": "pending_review",
            "feedback": "",
        }

    return node


def _review_node(store: Store):
    def node(state: XhsState) -> dict:
        decision = interrupt(
            {
                "kind": "review",
                "task_id": state["task_id"],
                "version": state.get("version"),
                "draft": state.get("draft"),
                "hint": "approve / request_changes / cancel",
            }
        )
        return {"decision": decision, "feedback": decision.get("comment", "")}

    return node


def _route_review(state: XhsState) -> str:
    action = (state.get("decision") or {}).get("action")
    return {
        "approve": "mark_approved",
        "request_changes": "create",
        "cancel": "cancelled",
    }.get(action, "cancelled")


def _mark_approved_node(store: Store):
    def node(state: XhsState) -> dict:
        store.approve_draft(state["task_id"], int(state.get("version", 0)))
        return {"phase": "approved"}

    return node


def _publish_gate_node(store: Store):
    def node(state: XhsState) -> dict:
        decision = interrupt(
            {
                "kind": "publish_gate",
                "task_id": state["task_id"],
                "version": state.get("version"),
                "draft": state.get("draft"),
                "hint": "publish / request_changes / cancel",
            }
        )
        return {"decision": decision, "feedback": decision.get("comment", "")}

    return node


def _route_publish(state: XhsState) -> str:
    action = (state.get("decision") or {}).get("action")
    return {
        "publish": "publish",
        "request_changes": "create",
        "cancel": "cancelled",
    }.get(action, "cancelled")


def _publish_node(publisher: Publisher, store: Store):
    def node(state: XhsState) -> dict:
        draft = Draft.model_validate(state["draft"])
        result = publisher.publish(state["task_id"], draft)
        # 无论是否为重复发布，任务终态都记为 published（幂等）。
        store.update_status(state["task_id"], "published")
        return {"phase": "published"}

    return node


def _cancelled_node(store: Store):
    def node(state: XhsState) -> dict:
        store.update_status(state["task_id"], "cancelled")
        return {"phase": "cancelled"}

    return node


def compile_graph(
    store: Store,
    research_agent: ResearchAgent,
    creation_agent: CreationAgent,
    publisher: Publisher,
    checkpointer,
):
    """构建并编译状态图。``checkpointer`` 由调用方注入（SQLite 检查点）。"""
    builder = StateGraph(XhsState)

    builder.add_node("research", _research_node(research_agent, store))
    builder.add_node("create", _create_node(creation_agent, store))
    builder.add_node("review", _review_node(store))
    builder.add_node("mark_approved", _mark_approved_node(store))
    builder.add_node("publish_gate", _publish_gate_node(store))
    builder.add_node("publish", _publish_node(publisher, store))
    builder.add_node("cancelled", _cancelled_node(store))

    builder.add_edge(START, "research")
    builder.add_edge("research", "create")
    builder.add_edge("create", "review")
    builder.add_conditional_edges(
        "review",
        _route_review,
        {"mark_approved": "mark_approved", "create": "create", "cancelled": "cancelled"},
    )
    builder.add_edge("mark_approved", "publish_gate")
    builder.add_conditional_edges(
        "publish_gate",
        _route_publish,
        {"publish": "publish", "create": "create", "cancelled": "cancelled"},
    )
    builder.add_edge("publish", END)
    builder.add_edge("cancelled", END)

    return builder.compile(checkpointer=checkpointer)
