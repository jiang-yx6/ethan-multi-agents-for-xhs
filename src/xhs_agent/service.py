"""任务服务：把存储、Agent、LangGraph 编排串起来，对外提供操作接口。"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .agents.base import CreationAgent, Publisher, ResearchAgent
from .agents.creation import MockCreationAgent
from .agents.publishing import MockPublisher
from .agents.research import MockResearchAgent
from .graph import compile_graph
from .models import TaskView
from .storage import Store


class TaskService:
    """高层任务服务。

    - ``data_dir/checkpoints.db``：LangGraph SQLite 检查点（thread_id 恢复）。
    - ``data_dir/store.db``：业务数据（任务 / 草稿版本 / 发布记录）。

    可通过构造参数注入真实 Agent / 发布器，替换 Mock 实现。
    """

    def __init__(
        self,
        data_dir: str | Path = "data",
        research_agent: ResearchAgent | None = None,
        creation_agent: CreationAgent | None = None,
        publisher: Publisher | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.data_dir / "checkpoints.db"
        self.store = Store(self.data_dir / "store.db")
        self.research_agent = research_agent or MockResearchAgent()
        self.creation_agent = creation_agent or MockCreationAgent()
        self.publisher = publisher or MockPublisher(self.store)

    # ---- 图生命周期 ----
    @contextmanager
    def _open_graph(self):
        """每次操作独立打开 SQLite 检查点，模拟进程重启后的恢复。"""
        conn = sqlite3.connect(str(self.checkpoint_path), check_same_thread=False)
        try:
            saver = SqliteSaver(conn)
            saver.setup()
            graph = compile_graph(
                self.store, self.research_agent, self.creation_agent, self.publisher, saver
            )
            yield graph
        finally:
            conn.close()

    def _config(self, task_id: str) -> dict:
        return {"configurable": {"thread_id": task_id}}

    def _resume(self, task_id: str, action: str, comment: str = "") -> TaskView:
        with self._open_graph() as graph:
            graph.invoke(Command(resume={"action": action, "comment": comment}), self._config(task_id))
        return self.store.get_task(task_id)

    # ---- 对外操作 ----
    def create_task(self, topic: str) -> TaskView:
        task_id = "xhs-" + uuid.uuid4().hex[:8]
        self.store.create_task(task_id, topic)
        with self._open_graph() as graph:
            graph.invoke(
                {"task_id": task_id, "topic": topic, "phase": "pending"},
                self._config(task_id),
            )
        return self.store.get_task(task_id)

    def status(self, task_id: str) -> TaskView:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"任务不存在：{task_id}")
        return task

    def approve(self, task_id: str) -> TaskView:
        self._require(task_id, "pending_review", "只有待审核的任务才能批准")
        return self._resume(task_id, "approve")

    def request_changes(self, task_id: str, comment: str) -> TaskView:
        self._require(task_id, ("pending_review", "approved"), "当前状态不支持提出修改意见")
        if not comment.strip():
            raise ValueError("修改意见不能为空")
        return self._resume(task_id, "request_changes", comment)

    def publish(self, task_id: str) -> TaskView:
        self._require(task_id, "approved", "只有已批准的任务才能发布")
        return self._resume(task_id, "publish")

    def cancel(self, task_id: str) -> TaskView:
        self._require(task_id, ("pending_review", "approved"), "当前状态不支持取消")
        return self._resume(task_id, "cancel")

    def _require(self, task_id: str, statuses, message: str) -> TaskView:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"任务不存在：{task_id}")
        allowed = statuses if isinstance(statuses, tuple) else (statuses,)
        if task.status not in allowed:
            raise ValueError(f"{message}（当前状态：{task.status}）")
        return task

    def close(self) -> None:
        self.store.close()
