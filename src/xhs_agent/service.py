"""任务服务：组装可注入能力，保存输入快照，恢复人工中断和失败节点。"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from collections.abc import Callable
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .agents.base import CreationAgent, IdeationAgent, Publisher, ResearchAgent, ReviewAgent
from .agents.creation import MockCreationAgent
from .agents.ideation import LLMIdeationAgent, MockIdeationAgent
from .agents.publishing import MockPublisher
from .agents.research import MockResearchAgent
from .agents.review import RuleReviewAgent
from .agents.registry import build_agent_registry
from .artifacts import archive_task
from .config import WorkflowConfig
from .graph import compile_graph
from .models import ContentRequest, DraftContent, IdeaCandidate, TaskView, TrendItem, UserProfile, WorkflowView
from .platforms import AccountRef, AccountService, PlatformPublisher, PlatformPublishingService, PlatformStore, create_platform_connector
from .platforms.content import draft_fingerprint, local_image_path
from .providers import EmptyTrendProvider, TrendProvider
from .storage import Store
from .skills import SkillLibrary
from .workflow import STEP_MESSAGES, TERMINAL, WorkflowConflictError, describe_workflow, gate_status


class TaskService:
    def __init__(self, data_dir: str | Path = "data",
                 ideation_agent: IdeationAgent | None = None,
                 research_agent: ResearchAgent | None = None,
                 creation_agent: CreationAgent | None = None,
                 publisher: Publisher | None = None,
                 review_agent: ReviewAgent | None = None,
                 trend_provider: TrendProvider | None = None,
                 config: WorkflowConfig | None = None,
                 skill_library: SkillLibrary | None = None,
                 connector_factory=create_platform_connector,
                 on_event: Callable[[dict], None] | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.skill_library = skill_library or SkillLibrary()
        self.checkpoint_path = self.data_dir / "checkpoints.db"
        self.store = Store(self.data_dir / "store.db")
        self.config = config or WorkflowConfig()
        self.on_event = on_event
        self.ideation_agent = ideation_agent if ideation_agent is not None else (
            MockIdeationAgent() if self.config.ideation_backend == "mock" else LLMIdeationAgent(
                config=self.config.ideation_llm, max_model_calls=self.config.ideation_max_model_calls,
                max_tool_calls=self.config.ideation_max_tool_calls,
                max_output_retries=self.config.ideation_max_output_retries,
                on_event=on_event,
            ))
        self.research_agent = research_agent or MockResearchAgent()
        self.creation_agent = creation_agent or MockCreationAgent()
        self.publisher = publisher or MockPublisher(self.store)
        self.review_agent = review_agent or RuleReviewAgent()
        self.trend_provider = trend_provider or EmptyTrendProvider()
        self._connector_factory = connector_factory
        self._platform_store: PlatformStore | None = None
        try:
            self.agent_registry = build_agent_registry(self.skill_library, {
                "ideation": self.ideation_agent, "research": self.research_agent,
                "creation": self.creation_agent, "review": self.review_agent, "publishing": self.publisher,
            })
        except Exception:
            self.store.close()
            raise

    @contextmanager
    def _open_graph(self, task_id: str):
        conn = sqlite3.connect(str(self.checkpoint_path), check_same_thread=False)
        try:
            saver = SqliteSaver(conn)
            saver.setup()
            yield compile_graph(
                self.store, self.ideation_agent, self.research_agent, self.creation_agent,
                self._task_publisher(task_id), saver, self.review_agent, self.trend_provider, self.config,
            )
        finally:
            conn.close()

    def _config(self, task_id: str) -> dict:
        return {"configurable": {"thread_id": task_id}, "recursion_limit": 60}

    def _execute(self, task_id: str, value) -> TaskView:
        try:
            with self._open_graph(task_id) as graph:
                # 只发送节点完成摘要，不把节点输出、模型消息或推理内容写入 Web 事件。
                for update in graph.stream(value, self._config(task_id), stream_mode="updates"):
                    for node in update:
                        if node in STEP_MESSAGES:
                            self._emit({"kind": "graph_step", "node": node, "message": STEP_MESSAGES[node]})
        except Exception as exc:
            self.store.mark_failed(task_id, f"{type(exc).__name__}: {exc}")
            exc.add_note(f"任务 ID：{task_id}；修复配置或接口问题后，可执行 xhs retry {task_id}")
            raise
        self.store.clear_error(task_id)
        self.reconcile_task(task_id)
        workflow = self.workflow(task_id)
        if workflow.status == "waiting_input":
            self._emit({"kind": "graph_paused", "message": "流程已暂停，等待你的操作",
                        "nodes": workflow.next_nodes, "interrupt_ids": [i.id for i in workflow.interrupts]})
        elif workflow.status == "completed":
            self._emit({"kind": "graph_completed", "message": "图执行已结束，请查看任务结果"})
        return self.status(task_id)

    def _emit(self, event: dict) -> None:
        if self.on_event:
            self.on_event(event)

    def workflow(self, task_id: str) -> WorkflowView:
        self.status(task_id)
        with self._open_graph(task_id) as graph:
            return describe_workflow(task_id, graph.get_state(self._config(task_id)))

    def validate_resume(self, task_id: str, action: str, interrupt_id: str | None = None) -> str:
        task, workflow = self.status(task_id), self.workflow(task_id)
        if len(workflow.interrupts) != 1 or gate_status(task, workflow) != task.status:
            raise WorkflowConflictError("图当前未停在可操作的人工节点，请刷新任务状态")
        pause = workflow.interrupts[0]
        if interrupt_id is not None and interrupt_id != pause.id:
            raise WorkflowConflictError("人工暂停位置已经变化，请刷新后重新操作")
        if action not in pause.actions:
            raise WorkflowConflictError("当前人工暂停节点不支持该操作")
        return pause.id

    def reconcile_task(self, task_id: str) -> bool:
        """只对齐安全的人工门禁；此方法不会运行图或重放业务操作。"""
        task = self.status(task_id)
        if task.status in TERMINAL:
            return False
        target = gate_status(task, self.workflow(task_id))
        if target is None:
            return False
        self.store.update_status(task_id, target)
        self.store.clear_error(task_id)
        return True

    def _resume(self, task_id: str, payload: dict, interrupt_id: str) -> TaskView:
        return self._execute(task_id, Command(resume={interrupt_id: payload}))

    def create_task(self, request: ContentRequest | str,
                    profile: UserProfile | None = None,
                    trends: list[TrendItem] | None = None) -> TaskView:
        task = self.register_task(request, profile, trends)
        return self.start_task(task.task_id)

    def register_task(self, request: ContentRequest | str, profile: UserProfile | None = None,
                      trends: list[TrendItem] | None = None, *, task_id: str | None = None) -> TaskView:
        """只登记输入快照，供 Web 在模型执行前返回任务标识。"""
        # 保留 create_task("主题") 的 Python 调用方式。
        request = ContentRequest(goal=request, topic=request) if isinstance(request, str) else request
        request = ContentRequest.model_validate(request).model_copy(deep=True)
        profile = UserProfile.model_validate(profile or UserProfile()).model_copy(deep=True)
        validated_trends = [TrendItem.model_validate(t).model_dump(mode="json") for t in trends] if trends is not None else None
        task_id = task_id or "xhs-" + uuid.uuid4().hex
        try:
            self.store.create_task(task_id, request.topic or request.goal, request, profile)
        except sqlite3.IntegrityError:
            existing = self.status(task_id)
            if existing.request != request or existing.profile != profile:
                raise ValueError("任务标识已绑定其他输入") from None
            return existing
        if validated_trends is not None:
            self.store.save_stage(task_id, "trends", validated_trends if request.use_trends else [])
        return self.status(task_id)

    def start_task(self, task_id: str) -> TaskView:
        task = self._require(task_id, "pending", "只有尚未执行的任务才能开始")
        return self._execute(task_id, {
            "task_id": task_id,
            "request": task.request.model_dump(mode="json"), "profile": task.profile.model_dump(mode="json"),
            "auto_revisions": 0,
        })

    def candidate_ideas(self, task_id: str) -> list[IdeaCandidate]:
        self.status(task_id)
        cached = self.store.get_stage(task_id, "ideas")
        if cached is None:
            with self._open_graph(task_id) as graph:
                cached = (graph.get_state(self._config(task_id)).values or {}).get("ideas", [])
        return [IdeaCandidate.model_validate(i) for i in cached]

    def select_idea(self, task_id: str, index: int, *, interrupt_id: str | None = None) -> TaskView:
        self._require(task_id, "ideating", "只有选题中的任务才能选择想法")
        pause = self.validate_resume(task_id, "select", interrupt_id)
        if type(index) is not int or not 0 <= index < len(self.candidate_ideas(task_id)):
            raise ValueError("选题序号超出范围")
        return self._resume(task_id, {"index": index}, pause)

    def status(self, task_id: str) -> TaskView:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"任务不存在：{task_id}")
        return task

    def approve(self, task_id: str, *, interrupt_id: str | None = None) -> TaskView:
        task = self._require(task_id, "pending_review", "只有待审核的任务才能批准")
        pause = self.validate_resume(task_id, "approve", interrupt_id)
        self.store.save_stage(task_id, "approval_fingerprint", draft_fingerprint(task.draft), task.current_version)
        return self._resume(task_id, {"action": "approve"}, pause)

    def replace_draft(self, task_id: str, content: DraftContent, *, interrupt_id: str | None = None) -> TaskView:
        self._require(task_id, ("pending_review", "approved"), "当前状态不支持替换草稿")
        pause = self.validate_resume(task_id, "draft", interrupt_id)
        content = DraftContent.model_validate(content).model_copy(deep=True)
        # 固化本地绝对路径，跨进程工作目录变化不会指向另一张图片。
        for image in content.images:
            path = local_image_path(image.source)
            if path is not None:
                if not path.is_file():
                    raise ValueError(f"图片不存在：{path}")
                image.source = str(path)
        return self._resume(task_id, {"action": "replace_draft", "draft": content.model_dump(mode="json")}, pause)

    def request_changes(self, task_id: str, comment: str, *, interrupt_id: str | None = None) -> TaskView:
        self._require(task_id, ("pending_review", "approved"), "当前状态不支持提出修改意见")
        pause = self.validate_resume(task_id, "changes", interrupt_id)
        if not comment.strip():
            raise ValueError("修改意见不能为空")
        return self._resume(task_id, {"action": "request_changes", "comment": comment}, pause)

    def publish(self, task_id: str, *, platform: str | None = None, account_id: str | None = None,
                interrupt_id: str | None = None) -> TaskView:
        self._require(task_id, "approved", "只有已批准的任务才能发布")
        pause = self.validate_resume(task_id, "publish", interrupt_id)
        if bool(platform) != bool(account_id):
            raise ValueError("真实发布必须同时指定 platform 与 account_id")
        target = self.store.get_stage(task_id, "publish_target")
        if platform:
            ref = AccountRef(platform=platform, account_id=account_id)
            if target and target != ref.model_dump():
                raise ValueError("任务已绑定其他发布账号，首版工作流每个任务使用一个目标账号")
            self.platform_publishing.prepare(task_id, ref)
            self.store.bind_publish_target(task_id, ref.model_dump())
        elif target:
            self.platform_publishing.prepare(task_id, AccountRef.model_validate(target))
        return self._resume(task_id, {"action": "publish"}, pause)

    @property
    def accounts(self) -> AccountService:
        if self._platform_store is None:
            self._platform_store = PlatformStore(self.data_dir)
        return AccountService(self._platform_store, self._connector_factory)

    @property
    def platform_publishing(self) -> PlatformPublishingService:
        return PlatformPublishingService(self.store, self.accounts)

    def _task_publisher(self, task_id: str) -> Publisher:
        target = self.store.get_stage(task_id, "publish_target")
        if target is None:
            return self.publisher
        publisher = PlatformPublisher(self.platform_publishing, AccountRef.model_validate(target))
        publisher.bind_skills(self.agent_registry.skills_for("publishing"))
        publisher.bind_tools(self.agent_registry.tools_for("publishing"))
        return publisher

    def confirm_published(self, task_id: str, note_url: str) -> TaskView:
        self.platform_publishing.confirm_published(task_id, note_url)
        return self.status(task_id)

    def retry(self, task_id: str) -> TaskView:
        """继续失败节点，已完成的图节点由检查点复用；发布回执不明时先核对平台。"""
        task = self._require(task_id, ("failed", "submitting", "pending"), "只有未执行或执行中断的任务才能重试")
        if self.reconcile_task(task_id):
            return self.status(task_id)
        with self._open_graph(task_id) as graph:
            has_checkpoint = bool(graph.get_state(self._config(task_id)).values)
        value = None if has_checkpoint else {
            "task_id": task_id, "request": task.request.model_dump(mode="json"),
            "profile": task.profile.model_dump(mode="json"), "auto_revisions": 0,
        }
        return self._execute(task_id, value)

    def export(self, task_id: str) -> Path:
        task = self._require(task_id, ("approved", "submitted", "unknown", "published"), "导出前请人工审核当前版本")
        return archive_task(task, self.data_dir / "outputs")

    def cancel(self, task_id: str, *, interrupt_id: str | None = None) -> TaskView:
        self._require(task_id, ("pending_review", "approved"), "当前状态不支持取消")
        pause = self.validate_resume(task_id, "cancel", interrupt_id)
        return self._resume(task_id, {"action": "cancel"}, pause)

    def _require(self, task_id: str, statuses, message: str) -> TaskView:
        task = self.status(task_id)
        allowed = statuses if isinstance(statuses, tuple) else (statuses,)
        if task.status not in allowed:
            raise ValueError(f"{message}（当前状态：{task.status}）")
        return task

    def close(self) -> None:
        if isinstance(self.ideation_agent, LLMIdeationAgent):
            self.ideation_agent.close()
        if self._platform_store is not None:
            self._platform_store.close()
        self.store.close()
