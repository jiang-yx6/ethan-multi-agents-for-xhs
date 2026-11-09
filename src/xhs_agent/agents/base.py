"""可替换接口（Protocol）。

未来接入真实模型 / 真实平台工具时，只需实现这些协议并在
``TaskService`` 构造时注入即可，无需改动编排逻辑：

- 真实 LLM 选题 Agent：实现 :class:`IdeationAgent`。
- 真实 LLM 研究 Agent：实现 :class:`ResearchAgent`。
- 真实 LLM 创作 Agent：实现 :class:`CreationAgent`。
- 质量审核：实现 :class:`ReviewAgent`。
- 平台发布适配器：实现 :class:`Publisher`，已提供 Mock 与小红书连接层实现。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..skills import AgentSkills

from ..models import (
    ContentRequest, Draft, IdeaCandidate, PublishResult, ResearchBrief,
    ResearchRequest, ReviewReport, TrendItem, UserProfile,
)
from ..models.collaboration import DelegationResult, DelegationTask, SupervisorContext, SupervisorDecision

SKILL_READ_TOOLS = ("load_skill", "read_skill_resource")


class SkillEnabledAgent:
    """服务层绑定角色读取接口；具体模型可将 catalog_prompt 与工具接入调用。"""

    def bind_skills(self, skills: AgentSkills) -> None:
        self._skills = skills

    def bind_tools(self, tool_names: tuple[str, ...]) -> None:
        if not set(tool_names).issubset(SKILL_READ_TOOLS):
            raise ValueError("当前运行时仅支持技能读取工具")
        self._allowed_tools = tuple(tool_names)

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return getattr(self, "_allowed_tools", SKILL_READ_TOOLS)

    @property
    def skills(self) -> AgentSkills:
        if not hasattr(self, "_skills"):
            raise RuntimeError("Agent 尚未绑定技能访问器，请通过 TaskService 组装或显式绑定")
        return self._skills

    def skill_prompt(self) -> str:
        return self.skills.catalog_prompt()


@runtime_checkable
class SupervisorAgent(Protocol):
    """理解目标、拆分与委派任务、检查结果，并决定继续、提问或结束。"""

    def decide(self, context: SupervisorContext) -> SupervisorDecision: ...


@runtime_checkable
class DelegatedAgent(Protocol):
    """后续专业 Agent 的统一执行入口；当前业务接口在迁移期间继续保留。"""

    def run(self, task: DelegationTask) -> DelegationResult: ...


@runtime_checkable
class IdeationAgent(Protocol):
    """根据需求、画像和可选热点生成候选选题。"""

    def ideate(self, request: ContentRequest, profile: UserProfile,
               trends: list[TrendItem]) -> list[IdeaCandidate]: ...


@runtime_checkable
class ResearchAgent(Protocol):
    """根据选定的选题（含上下文）生成结构化简报。"""

    def research(self, request: ResearchRequest) -> ResearchBrief: ...


@runtime_checkable
class CreationAgent(Protocol):
    """根据简报（及可选修改意见）生成草稿。"""

    def create(self, brief: ResearchBrief, feedback: str | None = None, *,
               request: ContentRequest, profile: UserProfile,
               previous_draft: Draft | None = None) -> Draft: ...


@runtime_checkable
class ReviewAgent(Protocol):
    def review(self, request: ContentRequest, profile: UserProfile,
               brief: ResearchBrief, draft: Draft) -> ReviewReport: ...


@runtime_checkable
class Publisher(Protocol):
    """发布已批准的草稿。真实实现需支持回执核对与重试幂等。"""

    def publish(self, task_id: str, draft: Draft) -> PublishResult: ...
