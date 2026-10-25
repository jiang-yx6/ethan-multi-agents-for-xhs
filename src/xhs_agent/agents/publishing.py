"""Mock 发布模块。"""
from __future__ import annotations

from ..models import Draft, PublishResult
from ..storage import Store


class MockPublisher:
    """Mock 发布器：只写本地发布记录，防止重复模拟发布。

    **不连接小红书、不真实发布。** 未来接入真实平台时，实现
    :class:`~xhs_agent.agents.base.Publisher` 协议即可替换。
    """

    def __init__(self, store: Store):
        self._store = store

    def publish(self, task_id: str, draft: Draft) -> PublishResult:
        if self._store.is_published(task_id):
            return PublishResult(
                published=False, reason="duplicate", draft_version=draft.version, task_id=task_id
            )
        ok = self._store.record_publish(task_id, draft)
        if not ok:
            # 并发/重复情况下的兜底：数据库 UNIQUE 约束已保证不重复写入。
            return PublishResult(
                published=False, reason="duplicate", draft_version=draft.version, task_id=task_id
            )
        return PublishResult(
            published=True, reason="ok", draft_version=draft.version, task_id=task_id
        )
