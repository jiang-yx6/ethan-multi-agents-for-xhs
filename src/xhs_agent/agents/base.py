"""父子 Agent 协作接口、专业能力接口与角色受限技能绑定。"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..skills import AgentSkills
from ..execution import ROLE_TOOLS, SKILL_TOOLS

from ..models import (
    ContentRequest, Draft, IdeaCandidate, PublishResult, ResearchBrief,
    ResearchRequest, ReviewReport, TrendItem, UserProfile,
)
from ..models.collaboration import DelegationResult, DelegationTask, SupervisorContext, SupervisorDecision

SKILL_READ_TOOLS = SKILL_TOOLS


class SkillEnabledAgent:
    """服务层绑定角色读取接口；具体模型可将 catalog_prompt 与工具接入调用。"""

    def bind_skills(self, skills: AgentSkills) -> None:
        self._skills = skills

    def bind_tools(self, tool_names: tuple[str, ...]) -> None:
        if not set(tool_names).issubset(ROLE_TOOLS[self.skills.role]):
            raise ValueError("工具不属于当前角色已实现的能力范围")
        self._allowed_tools = tuple(tool_names)

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return getattr(self, "_allowed_tools", getattr(self, "available_tools", SKILL_READ_TOOLS))

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
    """专业 Agent 的统一执行入口；原业务接口通过 SpecialistDelegate 适配。"""

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
