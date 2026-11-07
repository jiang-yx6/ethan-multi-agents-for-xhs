"""每个 Agent 一个子图；父图管理人工决策、有限返工与发布边界。"""
from __future__ import annotations

from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .agents.base import CreationAgent, IdeationAgent, Publisher, ResearchAgent, ReviewAgent
from .agents.review import RuleReviewAgent
from .artifacts import archive_task
from .config import WorkflowConfig
from .models import ContentRequest, Draft, DraftContent, IdeaCandidate, ResearchBrief, ResearchRequest, ReviewReport, TrendItem, UserProfile
from .providers import EmptyTrendProvider, TrendProvider
from .storage import Store


class ContextState(TypedDict, total=False):
    """各 Agent 共用的任务标识与输入快照。"""

    task_id: str
    request: dict
    profile: dict


class IdeationState(ContextState, total=False):
    trends: list[dict]
    ideas: list[dict]


class ResearchState(ContextState, total=False):
    idea: dict
    trends: list[dict]
    feedback: str
    brief: dict


class CreationState(ContextState, total=False):
    brief: dict
    draft: dict
    feedback: str


class QualityState(CreationState, total=False):
    quality_report: dict


class PublishState(TypedDict, total=False):
    """发布只需要任务标识与待发布草稿。"""

    task_id: str
    draft: dict


class XhsState(QualityState, total=False):
    """父图汇总阶段数据；版本由 draft 提供，业务状态由 Store 管理。"""

    ideas: list[dict]
    idea: dict
    trends: list[dict]
    auto_revisions: int
    replacement: dict


def _context(state: ContextState, store: Store) -> tuple[ContentRequest, UserProfile]:
    # 旧检查点没有 request/profile，从业务存储补全。
    task = store.get_task(state["task_id"])
    if task is None:
        raise KeyError(f"任务不存在：{state['task_id']}")
    request = ContentRequest.model_validate(state["request"]) if state.get("request") else task.request
    profile = UserProfile.model_validate(state["profile"]) if state.get("profile") else task.profile
    return request, profile


def _single_graph(schema, name, action):
    builder = StateGraph(schema)
    builder.add_node(name, action)
    builder.add_edge(START, name)
    builder.add_edge(name, END)
    return builder.compile()


def build_ideation_graph(agent: IdeationAgent, store: Store, provider: TrendProvider | None = None):
    def ideate(state: IdeationState) -> dict:
        request, profile = _context(state, store)
        cached_trends = store.get_stage(state["task_id"], "trends")
        if not request.use_trends:
            trends = []
        elif cached_trends is not None:
            trends = [TrendItem.model_validate(t) for t in cached_trends]
        else:
            trends = (provider or EmptyTrendProvider()).collect(request, profile)
        serialized = [t.model_dump(mode="json") for t in trends]
        store.save_stage(state["task_id"], "trends", serialized)
        cached_ideas = store.get_stage(state["task_id"], "ideas")
        ideas = [IdeaCandidate.model_validate(i) for i in cached_ideas] if cached_ideas else agent.ideate(
            request, profile, trends)
        if not ideas:
            raise ValueError("选题 Agent 未返回候选选题")
        output = [i.model_dump(mode="json") for i in ideas]
        store.save_stage(state["task_id"], "ideas", output)
        store.update_status(state["task_id"], "ideating")
        return {"ideas": output, "trends": serialized}
    return _single_graph(IdeationState, "ideate", ideate)


def build_research_graph(agent: ResearchAgent, store: Store):
    def research(state: ResearchState) -> dict:
        request, profile = _context(state, store)
        idea = IdeaCandidate.model_validate(state["idea"])
        research_request = ResearchRequest(
            topic=idea.title, audience=idea.audience, objective=idea.angle,
            style=profile.style, constraints=[*request.constraints, *profile.boundaries],
            preferences=profile.preferences, supplied_materials=request.supplied_materials,
            content_request=request, profile=profile, idea=idea,
            trends=[TrendItem.model_validate(t) for t in state.get("trends", [])
                    if t["url"] in idea.trend_urls],
        )
        # 资料返工也获得审核意见。
        if state.get("feedback"):
            research_request.constraints.append(f"本次修订要求：{state['feedback']}")
        brief = agent.research(research_request)
        store.save_stage(state["task_id"], "research", brief.model_dump(mode="json"))
        store.update_status(state["task_id"], "researched")
        return {"brief": brief.model_dump(mode="json")}
    return _single_graph(ResearchState, "research", research)


def build_creation_graph(agent: CreationAgent, store: Store):
    def create(state: CreationState) -> dict:
        request, profile = _context(state, store)
        # 使用检查点中的上一版草稿推导版本，不能取业务库最新版本：
        # 新草稿可能已经提交，但图检查点尚未落盘。
        version = int((state.get("draft") or {}).get("version", 0)) + 1
        # 节点写入业务库后、检查点落盘前中断：重放时复用同版本。
        draft = store.get_draft(state["task_id"], version)
        if draft is None:
            draft = agent.create(
                ResearchBrief.model_validate(state["brief"]),
                feedback=state.get("feedback") or None, request=request, profile=profile,
                previous_draft=Draft.model_validate(state["draft"]) if state.get("draft") else None,
            )
            draft = draft.model_copy(update={"version": version, "approved": False, "approved_at": None})
            store.save_draft(state["task_id"], draft)
        archive_task(store.get_task(state["task_id"]), store.db_path.parent / "outputs")
        return {"draft": draft.model_dump(mode="json"), "feedback": ""}
    return _single_graph(CreationState, "create", create)


def build_quality_graph(agent: ReviewAgent, store: Store):
    def quality(state: QualityState) -> dict:
        request, profile = _context(state, store)
        draft = Draft.model_validate(state["draft"])
        report = agent.review(request, profile, ResearchBrief.model_validate(state["brief"]), draft)
        if report.draft_version != draft.version:
            raise ValueError("审核结果与当前草稿版本不一致")
        store.save_stage(state["task_id"], "quality_review", report.model_dump(mode="json"), draft.version)
        archive_task(store.get_task(state["task_id"]), store.db_path.parent / "outputs")
        errors = [issue for issue in report.issues if issue.severity == "error"]
        return {"quality_report": report.model_dump(mode="json"),
                "feedback": "\n".join(issue.message for issue in errors)}
    return _single_graph(QualityState, "check", quality)


def _route_quality_node(store: Store, config: WorkflowConfig):
    def node(state: XhsState) -> Command[Literal["research", "create", "review"]]:
        report = ReviewReport.model_validate(state["quality_report"])
        errors = [issue for issue in report.issues if issue.severity == "error"]
        count = state.get("auto_revisions", 0)
        if errors and count < config.max_auto_revisions:
            target = "research" if any(issue.target == "research" for issue in errors) else "create"
            return Command(goto=target, update={"auto_revisions": count + 1})
        store.update_status(state["task_id"], "pending_review")
        return Command(goto="review")
    return node


def build_publish_graph(publisher: Publisher, store: Store):
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
    return _single_graph(PublishState, "publish", publish)


def _select_idea_node(store: Store):
    def node(state: XhsState) -> dict:
        choice = interrupt({"kind": "select_idea", "task_id": state["task_id"],
                            "ideas": state.get("ideas"), "hint": "choose an idea by index (0-based)"})
        ideas = [IdeaCandidate.model_validate(i) for i in state.get("ideas", [])]
        idx = (choice or {}).get("index")
        if type(idx) is not int or not 0 <= idx < len(ideas):
            raise ValueError("选题序号超出范围")
        chosen = ideas[idx]
        store.update_topic(state["task_id"], chosen.title)
        store.update_status(state["task_id"], "researching")
        return {"idea": chosen.model_dump(mode="json")}
    return node


def _review_node(store: Store):
    def node(state: XhsState) -> Command[Literal["mark_approved", "create", "replace_draft", "cancelled"]]:
        decision = interrupt({
            "kind": "review", "task_id": state["task_id"], "version": state["draft"]["version"],
            "draft": state.get("draft"), "quality_report": state.get("quality_report"),
            "hint": "approve / request_changes / cancel",
        })
        if decision.get("action") not in {"approve", "request_changes", "replace_draft", "cancel"}:
            raise ValueError("未知审核操作")
        target = {"approve": "mark_approved", "request_changes": "create", "replace_draft": "replace_draft", "cancel": "cancelled"}[
            decision["action"]]
        return Command(goto=target, update={"feedback": decision.get("comment", ""), "auto_revisions": 0,
                                             "replacement": decision.get("draft", {})})
    return node


def _mark_approved_node(store: Store):
    def node(state: XhsState) -> dict:
        store.approve_draft(state["task_id"], int(state["draft"]["version"]))
        return {}
    return node


def _publish_gate_node(store: Store):
    def node(state: XhsState) -> Command[Literal["publish", "create", "replace_draft", "cancelled"]]:
        decision = interrupt({
            "kind": "publish_gate", "task_id": state["task_id"], "version": state["draft"]["version"],
            "draft": state.get("draft"), "hint": "publish / request_changes / cancel",
        })
        if decision.get("action") not in {"publish", "request_changes", "replace_draft", "cancel"}:
            raise ValueError("未知发布操作")
        target = {"publish": "publish", "request_changes": "create", "replace_draft": "replace_draft", "cancel": "cancelled"}[
            decision["action"]]
        return Command(goto=target, update={"feedback": decision.get("comment", ""), "auto_revisions": 0,
                                             "replacement": decision.get("draft", {})})
    return node


def _cancelled_node(store: Store):
    def node(state: XhsState) -> dict:
        store.update_status(state["task_id"], "cancelled")
        return {}
    return node


def _replace_draft_node(store: Store):
    def node(state: XhsState) -> dict:
        version = int(state["draft"]["version"]) + 1
        draft = store.get_draft(state["task_id"], version)
        if draft is None:
            content = DraftContent.model_validate(state["replacement"])
            draft = Draft(**content.model_dump(), version=version)
            store.save_draft(state["task_id"], draft)
        archive_task(store.get_task(state["task_id"]), store.db_path.parent / "outputs")
        return {"draft": draft.model_dump(mode="json"), "replacement": {}, "feedback": ""}
    return node


def compile_graph(store: Store, ideation_agent: IdeationAgent, research_agent: ResearchAgent,
                  creation_agent: CreationAgent, publisher: Publisher, checkpointer,
                  review_agent: ReviewAgent | None = None, trend_provider: TrendProvider | None = None,
                  config: WorkflowConfig | None = None):
    builder = StateGraph(XhsState)
    builder.add_node("ideation", build_ideation_graph(ideation_agent, store, trend_provider))
    builder.add_node("select_idea", _select_idea_node(store))
    builder.add_node("research", build_research_graph(research_agent, store))
    builder.add_node("create", build_creation_graph(creation_agent, store))
    builder.add_node("replace_draft", _replace_draft_node(store))
    builder.add_node("quality_review", build_quality_graph(review_agent or RuleReviewAgent(), store))
    builder.add_node("route_quality", _route_quality_node(store, config or WorkflowConfig()))
    builder.add_node("review", _review_node(store))
    builder.add_node("mark_approved", _mark_approved_node(store))
    builder.add_node("publish_gate", _publish_gate_node(store))
    builder.add_node("publish", build_publish_graph(publisher, store))
    builder.add_node("cancelled", _cancelled_node(store))
    builder.add_edge(START, "ideation")
    builder.add_edge("ideation", "select_idea")
    builder.add_edge("select_idea", "research")
    builder.add_edge("research", "create")
    builder.add_edge("create", "quality_review")
    builder.add_edge("replace_draft", "quality_review")
    builder.add_edge("quality_review", "route_quality")
    builder.add_edge("mark_approved", "publish_gate")
    builder.add_conditional_edges("publish", lambda state: (
        "retry" if store.get_task(state["task_id"]).status == "approved" else "done"
    ), {"retry": "publish_gate", "done": END})
    builder.add_edge("cancelled", END)
    return builder.compile(checkpointer=checkpointer)
