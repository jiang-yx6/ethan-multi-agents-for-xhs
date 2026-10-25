"""可替换接口（Protocol）。

未来接入真实模型 / 真实平台工具时，只需实现这些协议并在
``TaskService`` 构造时注入即可，无需改动编排逻辑：

- 真实 LLM 研究 Agent：实现 :class:`ResearchAgent`。
- 真实 LLM 创作 Agent：实现 :class:`CreationAgent`。
- 真实小红书发布器：实现 :class:`Publisher`（本版仅提供 Mock）。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Draft, PublishResult, ResearchBrief


@runtime_checkable
class ResearchAgent(Protocol):
    """根据选题生成结构化简报。"""

    def research(self, topic: str) -> ResearchBrief: ...


@runtime_checkable
class CreationAgent(Protocol):
    """根据简报（及可选修改意见）生成草稿。"""

    def create(self, brief: ResearchBrief, feedback: str | None = None) -> Draft: ...


@runtime_checkable
class Publisher(Protocol):
    """发布一篇草稿（真实实现会调用小红书开放平台）。"""

    def publish(self, task_id: str, draft: Draft) -> PublishResult: ...
