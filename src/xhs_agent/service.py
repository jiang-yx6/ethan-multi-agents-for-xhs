"""任务服务：组装可注入能力，保存输入快照，恢复人工中断和失败节点。"""
from __future__ import annotations

import sqlite3
import sys
import uuid
from contextlib import contextmanager
from collections.abc import Callable
from pathlib import Path
from time import perf_counter

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .agents.base import CreationAgent, IdeationAgent, Publisher, ResearchAgent, ReviewAgent
from .agents.factory import create_agents
from .agents.model import ModelAgent
from .agents.registry import build_agent_registry
from .agents.delegation import SpecialistDelegate
from .artifacts import archive_task
from .checkpoints import checkpoint_serializer
from .config import AgentConfig
from .conversation import conversation_messages
from .graph import compile_graph
from .flow_log import FlowLog, error_details
from .memory import context_session
from .team_store import TeamStore
from .models import ArtifactReference, ContentRequest, DraftContent, IdeaCandidate, TaskView, TrendItem, UserProfile, WorkflowView
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
                 config: AgentConfig | None = None,
                 skill_library: SkillLibrary | None = None,
                 connector_factory=create_platform_connector,
                 on_event: Callable[[dict], None] | None = None,
                 supervisor_agent=None, delegated_agents: dict | None = None,
                 search_provider=None, image_provider=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.skill_library = skill_library or SkillLibrary()
        self.checkpoint_path = self.data_dir / "checkpoints.db"
        self.store = Store(self.data_dir / "store.db")
        self.config = config or AgentConfig()
        self.flow_log = FlowLog(self.data_dir, self.config)
        self.on_event = on_event
        self.trend_provider = trend_provider or EmptyTrendProvider()
        self._connector_factory = connector_factory
        self._platform_store: PlatformStore | None = None
        try:
            agents = create_agents(self.config, self.store, overrides={
                "ideation": ideation_agent, "research": research_agent, "creation": creation_agent,
                "review": review_agent, "supervisor": supervisor_agent, "publishing": publisher,
            }, on_event=on_event, search_provider=search_provider, image_provider=image_provider)
            self.ideation_agent, self.research_agent, self.creation_agent, self.review_agent, self.supervisor_agent = (
                agents[role] for role in ("ideation", "research", "creation", "review", "supervisor"))
            self.publisher = agents["publishing"]
            self._model_agents = tuple(agent for agent in agents.values() if isinstance(agent, ModelAgent))
            if set(delegated_agents or {}) - {"ideation", "research", "creation", "review"}:
                raise ValueError("只能替换已注册的专业 Agent")
            if len({id(agent) for agent in agents.values()}) != len(agents):
                raise ValueError("不同角色必须使用独立 Agent 实例，避免权限互相覆盖")
            workers = {role: SpecialistDelegate(role, agent, self.store, self.trend_provider)
                       for role, agent in agents.items() if role not in {"supervisor", "publishing"}}
            self.agent_registry = build_agent_registry(self.skill_library, {
                **workers, **(delegated_agents or {}), "supervisor": self.supervisor_agent, "publishing": self.publisher,
            }, execution=self.config.execution)
        except Exception:
            self.store.close()
            raise

    def _require_agent_graph(self, task_id: str) -> None:
        if self.status(task_id).workflow_version != 2:
            raise WorkflowConflictError("该任务使用已移除的旧图，只能查看已有数据；请新建父子 Agent 任务")

    @contextmanager
    def _open_graph(self, task_id: str):
        self._require_agent_graph(task_id)
        conn = sqlite3.connect(str(self.checkpoint_path), check_same_thread=False)
        try:
            saver = SqliteSaver(conn, serde=checkpoint_serializer())
            saver.setup()
            yield compile_graph(self.store, self.agent_registry, self.supervisor_agent,
                                self._task_publisher(task_id), saver, self.config, self._emit)
        finally:
            conn.close()

    def _config(self, task_id: str) -> dict:
        return {"configurable": {"thread_id": task_id},
                "recursion_limit": (max(60, self.config.team_max_rounds * 3 + 20)
                                    if self.config.team_max_rounds is not None else sys.maxsize)}

    def _execute(self, task_id: str, value) -> TaskView:
        with self.flow_log.run(task_id, uuid.uuid4().hex), context_session(
                self.store, task_id, self.config.execution.context, self.flow_log.redact):
            start = perf_counter()
            self._emit({"kind": "execution_started", "mode": "resume" if isinstance(value, Command)
                        else "retry" if value is None else "start", "message": "开始执行 Agent 流程"})
            try:
                task = self._execute_graph(task_id, value)
            except Exception as exc:
                self._emit({"kind": "execution_failed", **error_details(exc),
                            "elapsed_ms": round((perf_counter() - start) * 1000), "message": "Agent 流程执行失败"})
                raise
            self._emit({"kind": "execution_finished", "status": task.status, "version": task.current_version,
                        "elapsed_ms": round((perf_counter() - start) * 1000), "message": "本次 Agent 流程执行结束"})
            return task

    def _execute_graph(self, task_id: str, value) -> TaskView:
        self._require_agent_graph(task_id)
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
        self.flow_log.write(event)
        if self.on_event:
            self.on_event(event)

    def workflow(self, task_id: str) -> WorkflowView:
        if self.status(task_id).workflow_version != 2:
            return WorkflowView(thread_id=task_id, status="unsupported",
                                error="旧图已移除，已有数据可以查看；请新建父子 Agent 任务")
        with self._open_graph(task_id) as graph:
            return describe_workflow(task_id, graph.get_state(self._config(task_id)))

    def _resume_interrupt(self, task_id: str, action: str, interrupt_id: str | None = None):
        task, workflow = self.status(task_id), self.workflow(task_id)
        if len(workflow.interrupts) != 1 or gate_status(task, workflow) != task.status:
            raise WorkflowConflictError("图当前未停在可操作的人工节点，请刷新任务状态")
        pause = workflow.interrupts[0]
        if interrupt_id is not None and interrupt_id != pause.id:
            raise WorkflowConflictError("人工暂停位置已经变化，请刷新后重新操作")
        if action not in pause.actions:
            raise WorkflowConflictError("当前人工暂停节点不支持该操作")
        return pause

    def validate_resume(self, task_id: str, action: str, interrupt_id: str | None = None) -> str:
        return self._resume_interrupt(task_id, action, interrupt_id).id

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
        self._emit({"kind": "user_action", "task_id": task_id, "action": payload.get("action", "answer"),
                    "interrupt_ids": [interrupt_id], "message": "收到人工操作"})
        return self._execute(task_id, Command(resume={interrupt_id: payload}))

    def create_task(self, request: ContentRequest | str,
                    profile: UserProfile | None = None,
                    trends: list[TrendItem] | None = None, *, memory_scope: str = "default") -> TaskView:
        task = self.register_task(request, profile, trends, memory_scope=memory_scope)
        return self.start_task(task.task_id)

    def register_task(self, request: ContentRequest | str, profile: UserProfile | None = None,
                      trends: list[TrendItem] | None = None, *, task_id: str | None = None,
                      memory_scope: str = "default") -> TaskView:
        """只登记输入快照，供 Web 在模型执行前返回任务标识。"""
        # 保留 create_task("主题") 的 Python 调用方式。
        if not isinstance(memory_scope, str) or not memory_scope.strip() or len(memory_scope) > 200:
            raise ValueError("记忆范围必须是 1 到 200 字的非空字符串")
        request = ContentRequest(goal=request, topic=request) if isinstance(request, str) else request
        request = ContentRequest.model_validate(request).model_copy(deep=True)
        for index, source in enumerate(request.input_images):
            path = local_image_path(source)
            if path is None or not path.is_file():
                raise ValueError("输入图片必须是已有的本地文件")
            request.input_images[index] = str(path)
        profile = UserProfile.model_validate(profile or UserProfile()).model_copy(deep=True)
        validated_trends = [TrendItem.model_validate(t).model_dump(mode="json") for t in trends] if trends is not None else None
        task_id = task_id or "xhs-" + uuid.uuid4().hex
        try:
            self.store.create_task(task_id, request.topic or request.goal, request, profile, memory_scope=memory_scope)
        except sqlite3.IntegrityError:
            existing = self.status(task_id)
            if existing.request != request or existing.profile != profile or self.store.memory_scope(task_id) != memory_scope:
                raise ValueError("任务标识已绑定其他输入") from None
            return existing
        if validated_trends is not None:
            self.store.save_stage(task_id, "trends", validated_trends if request.use_trends else [])
        self._emit({"kind": "task_registered", "task_id": task_id, "status": "pending", "message": "已登记创作任务"})
        return self.status(task_id)

    def start_task(self, task_id: str) -> TaskView:
        self._require(task_id, "pending", "只有尚未执行的任务才能开始")
        return self._execute(task_id, {"task_id": task_id})

    def candidate_ideas(self, task_id: str) -> list[IdeaCandidate]:
        self.status(task_id)
        ledger = TeamStore(self.store)
        refs = [ref for ref in ledger.artifacts(task_id) if ref.kind == "ideas"]
        cached = ledger.resolve(refs[-1]) if refs else self.store.get_stage(task_id, "ideas")
        return [IdeaCandidate.model_validate(i) for i in cached or []]

    def context_archive(self, task_id: str) -> dict:
        self.status(task_id)
        return {"task_id": task_id, "task_summary": self.store.get_stage(task_id, "context_summary"),
                "runs": self.store.context_archives(task_id)}

    def status(self, task_id: str) -> TaskView:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"任务不存在：{task_id}")
        return task

    def collaboration(self, task_id: str) -> dict:
        task = self.status(task_id)
        if task.workflow_version != 2:
            return {"mode": "unsupported", "history": [], "artifacts": [], "error": "旧图已移除，请新建父子 Agent 任务"}
        with self._open_graph(task_id) as graph:
            state = graph.get_state(self._config(task_id)).values or {}
        refs = [ref.model_dump(mode="json") for ref in TeamStore(self.store).artifacts(task_id)]
        latest_ideas = next((ref for ref in reversed(refs) if ref["kind"] == "ideas"), None)
        selection = state.get("idea_selection")
        if selection and selection["candidates"] != latest_ideas:
            selection = None
        return {"mode": "team", "decision": state.get("decision"), "history": state.get("history", []),
                "messages": conversation_messages(state),
                "idea_selection": selection,
                "user_messages": state.get("user_messages", []), "summary": state.get("summary"),
                "rounds": state.get("rounds", 0), "turn": state.get("turn", 0),
                "artifacts": refs}

    def artifact(self, task_id: str, artifact_id: str, version: int) -> dict:
        self.status(task_id)
        artifacts = TeamStore(self.store)
        ref = next((ref for ref in artifacts.artifacts(task_id)
                    if ref.artifact_id == artifact_id and ref.version == version), None)
        if ref is None:
            raise KeyError("当前任务没有该产物版本")
        return {"reference": ref.model_dump(mode="json"), "content": artifacts.resolve(ref)}

    def message(self, task_id: str, text: str, *, interrupt_id: str | None = None) -> TaskView:
        self._require(task_id, ("pending_review", "approved"), "当前状态暂不能向父 Agent 发送新消息")
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise ValueError("消息必须是 1 到 10000 字的非空文字")
        pause = self.validate_resume(task_id, "message", interrupt_id)
        return self._resume(task_id, {"action": "message", "text": text.strip()}, pause)

    def answer(self, task_id: str, answer: str, *, interrupt_id: str | None = None) -> TaskView:
        self._require(task_id, "waiting_input", "当前任务没有等待用户补充信息")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 10000:
            raise ValueError("回复必须是 1 到 10000 字的非空文字")
        pause = self.validate_resume(task_id, "answer", interrupt_id)
        return self._resume(task_id, {"answer": answer.strip()}, pause)

    def validate_idea_action(self, task_id: str, action: str, ideas_artifact_id: str, ideas_version: int,
                             *, interrupt_id: str | None = None, idea_index: int | None = None) -> str:
        pause = self._resume_interrupt(task_id, action, interrupt_id)
        ref = pause.value.get("ideas", {})
        if (ref.get("artifact_id"), ref.get("version")) != (ideas_artifact_id, ideas_version):
            raise WorkflowConflictError("候选选题已变化，请刷新后重新操作")
        if action == "select_idea":
            candidates = TeamStore(self.store).resolve(ArtifactReference.model_validate(ref))
            if type(idea_index) is not int or not 0 <= idea_index < len(candidates):
                raise ValueError("请选择有效的候选选题")
        return pause.id

    def select_idea(self, task_id: str, idea_index: int, *, ideas_artifact_id: str, ideas_version: int,
                    interrupt_id: str | None = None) -> TaskView:
        pause = self.validate_idea_action(task_id, "select_idea", ideas_artifact_id, ideas_version,
                                         interrupt_id=interrupt_id, idea_index=idea_index)
        return self._resume(task_id, {"action": "select_idea", "idea_index": idea_index,
                                     "ideas_artifact_id": ideas_artifact_id, "ideas_version": ideas_version}, pause)

    def regenerate_ideas(self, task_id: str, feedback: str, *, ideas_artifact_id: str, ideas_version: int,
                         interrupt_id: str | None = None) -> TaskView:
        if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 10000:
            raise ValueError("重新生成要求必须是 1 到 10000 字的非空文字")
        pause = self.validate_idea_action(task_id, "regenerate_ideas", ideas_artifact_id, ideas_version,
                                         interrupt_id=interrupt_id)
        return self._resume(task_id, {"action": "regenerate_ideas", "feedback": feedback.strip(),
                                     "ideas_artifact_id": ideas_artifact_id, "ideas_version": ideas_version}, pause)

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
        self._require(task_id, ("failed", "submitting", "pending"), "只有未执行或执行中断的任务才能重试")
        if self.reconcile_task(task_id):
            return self.status(task_id)
        with self._open_graph(task_id) as graph:
            has_checkpoint = bool(graph.get_state(self._config(task_id)).values)
        value = None if has_checkpoint else {"task_id": task_id}
        return self._execute(task_id, value)

    def export(self, task_id: str) -> Path:
        task = self._require(task_id, ("approved", "submitted", "unknown", "published"), "导出前请人工审核当前版本")
        return archive_task(task, self.data_dir / "outputs")

    def cancel(self, task_id: str, *, interrupt_id: str | None = None) -> TaskView:
        self._require(task_id, ("pending_review", "approved", "waiting_input"), "当前状态不支持取消")
        pause = self.validate_resume(task_id, "cancel", interrupt_id)
        return self._resume(task_id, {"action": "cancel"}, pause)

    def _require(self, task_id: str, statuses, message: str) -> TaskView:
        task = self.status(task_id)
        allowed = statuses if isinstance(statuses, tuple) else (statuses,)
        if task.status not in allowed:
            raise ValueError(f"{message}（当前状态：{task.status}）")
        return task

    def close(self) -> None:
        for agent in self._model_agents:
            agent.close()
        if self._platform_store is not None:
            self._platform_store.close()
        self.store.close()
