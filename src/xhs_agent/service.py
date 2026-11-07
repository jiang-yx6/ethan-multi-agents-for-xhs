"""任务服务：组装可注入能力，保存输入快照，恢复人工中断和失败节点。"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .agents.base import CreationAgent, IdeationAgent, Publisher, ResearchAgent, ReviewAgent, SkillEnabledAgent
from .agents.creation import MockCreationAgent
from .agents.ideation import MockIdeationAgent
from .agents.publishing import MockPublisher
from .agents.research import MockResearchAgent
from .agents.review import RuleReviewAgent
from .artifacts import archive_task
from .config import WorkflowConfig
from .graph import compile_graph
from .models import ContentRequest, DraftContent, IdeaCandidate, TaskView, TrendItem, UserProfile
from .platforms import AccountRef, AccountService, PlatformPublisher, PlatformPublishingService, PlatformStore, create_platform_connector
from .platforms.content import draft_fingerprint, local_image_path
from .providers import EmptyTrendProvider, TrendProvider
from .storage import Store
from .skills import SkillLibrary


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
                 connector_factory=create_platform_connector):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.skill_library = skill_library or SkillLibrary()
        self.checkpoint_path = self.data_dir / "checkpoints.db"
        self.store = Store(self.data_dir / "store.db")
        self.ideation_agent = ideation_agent or MockIdeationAgent()
        self.research_agent = research_agent or MockResearchAgent()
        self.creation_agent = creation_agent or MockCreationAgent()
        self.publisher = publisher or MockPublisher(self.store)
        self.review_agent = review_agent or RuleReviewAgent()
        self.trend_provider = trend_provider or EmptyTrendProvider()
        self.config = config or WorkflowConfig()
        self._connector_factory = connector_factory
        self._platform_store: PlatformStore | None = None
        assigned = set()
        for role, agent in (("ideation", self.ideation_agent), ("research", self.research_agent),
                            ("creation", self.creation_agent), ("review", self.review_agent),
                            ("publishing", self.publisher)):
            if isinstance(agent, SkillEnabledAgent):
                if id(agent) in assigned:
                    self.store.close()
                    raise ValueError("不同角色必须使用独立 Agent 实例，避免技能访问范围互相覆盖")
                assigned.add(id(agent))
                agent.bind_skills(self.skill_library.for_agent(role))

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
                graph.invoke(value, self._config(task_id))
        except Exception as exc:
            self.store.mark_failed(task_id, f"{type(exc).__name__}: {exc}")
            raise
        self.store.clear_error(task_id)
        return self.status(task_id)

    def _resume(self, task_id: str, payload: dict) -> TaskView:
        return self._execute(task_id, Command(resume=payload))

    def create_task(self, request: ContentRequest | str,
                    profile: UserProfile | None = None,
                    trends: list[TrendItem] | None = None) -> TaskView:
        # 保留 create_task("主题") 的 Python 调用方式。
        request = ContentRequest(goal=request, topic=request) if isinstance(request, str) else request
        request = ContentRequest.model_validate(request).model_copy(deep=True)
        profile = UserProfile.model_validate(profile or UserProfile()).model_copy(deep=True)
        validated_trends = [TrendItem.model_validate(t).model_dump(mode="json") for t in trends] if trends is not None else None
        task_id = "xhs-" + uuid.uuid4().hex
        self.store.create_task(task_id, request.topic or request.goal, request, profile)
        if validated_trends is not None:
            self.store.save_stage(task_id, "trends", validated_trends if request.use_trends else [])
        return self._execute(task_id, {
            "task_id": task_id,
            "request": request.model_dump(mode="json"), "profile": profile.model_dump(mode="json"),
            "auto_revisions": 0,
        })

    def candidate_ideas(self, task_id: str) -> list[IdeaCandidate]:
        self.status(task_id)
        cached = self.store.get_stage(task_id, "ideas")
        if cached is None:
            with self._open_graph(task_id) as graph:
                cached = (graph.get_state(self._config(task_id)).values or {}).get("ideas", [])
        return [IdeaCandidate.model_validate(i) for i in cached]

    def select_idea(self, task_id: str, index: int) -> TaskView:
        self._require(task_id, "ideating", "只有选题中的任务才能选择想法")
        if type(index) is not int or not 0 <= index < len(self.candidate_ideas(task_id)):
            raise ValueError("选题序号超出范围")
        return self._resume(task_id, {"index": index})

    def status(self, task_id: str) -> TaskView:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"任务不存在：{task_id}")
        return task

    def approve(self, task_id: str) -> TaskView:
        task = self._require(task_id, "pending_review", "只有待审核的任务才能批准")
        self.store.save_stage(task_id, "approval_fingerprint", draft_fingerprint(task.draft), task.current_version)
        return self._resume(task_id, {"action": "approve"})

    def replace_draft(self, task_id: str, content: DraftContent) -> TaskView:
        self._require(task_id, ("pending_review", "approved"), "当前状态不支持替换草稿")
        content = DraftContent.model_validate(content).model_copy(deep=True)
        # 固化本地绝对路径，跨进程工作目录变化不会指向另一张图片。
        for image in content.images:
            path = local_image_path(image.source)
            if path is not None:
                if not path.is_file():
                    raise ValueError(f"图片不存在：{path}")
                image.source = str(path)
        return self._resume(task_id, {"action": "replace_draft", "draft": content.model_dump(mode="json")})

    def request_changes(self, task_id: str, comment: str) -> TaskView:
        self._require(task_id, ("pending_review", "approved"), "当前状态不支持提出修改意见")
        if not comment.strip():
            raise ValueError("修改意见不能为空")
        return self._resume(task_id, {"action": "request_changes", "comment": comment})

    def publish(self, task_id: str, *, platform: str | None = None, account_id: str | None = None) -> TaskView:
        self._require(task_id, "approved", "只有已批准的任务才能发布")
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
        return self._resume(task_id, {"action": "publish"})

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
        publisher.bind_skills(self.skill_library.for_agent("publishing"))
        return publisher

    def confirm_published(self, task_id: str, note_url: str) -> TaskView:
        self.platform_publishing.confirm_published(task_id, note_url)
        return self.status(task_id)

    def retry(self, task_id: str) -> TaskView:
        """继续失败节点，已完成的图节点由检查点复用；发布回执不明时先核对平台。"""
        self._require(task_id, ("failed", "submitting"), "只有执行失败或提交中断的任务才能重试")
        return self._execute(task_id, None)

    def export(self, task_id: str) -> Path:
        task = self._require(task_id, ("approved", "submitted", "unknown", "published"), "导出前请人工审核当前版本")
        return archive_task(task, self.data_dir / "outputs")

    def cancel(self, task_id: str) -> TaskView:
        self._require(task_id, ("pending_review", "approved"), "当前状态不支持取消")
        return self._resume(task_id, {"action": "cancel"})

    def _require(self, task_id: str, statuses, message: str) -> TaskView:
        task = self.status(task_id)
        allowed = statuses if isinstance(statuses, tuple) else (statuses,)
        if task.status not in allowed:
            raise ValueError(f"{message}（当前状态：{task.status}）")
        return task

    def close(self) -> None:
        if self._platform_store is not None:
            self._platform_store.close()
        self.store.close()
