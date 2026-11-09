"""各 Agent 共用的画像、需求、热点、任务视图与时间工具。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from .creation import Draft
    from .research import ResearchBrief
    from .review import ReviewReport
    from .publishing import PublishResult


def utcnow() -> datetime:
    """带时区的 UTC 当前时间。"""
    return datetime.now(timezone.utc)


class UserProfile(BaseModel):
    """账号画像；真实经历与表达偏好分开保存。"""

    name: str = "默认账号"
    positioning: str = "分享有用、有个人特色的内容"
    audience: str = "对当前主题感兴趣的读者"
    style: str = "自然、具体、简洁"
    interests: list[str] = Field(default_factory=list)
    experiences: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)


class ContentRequest(BaseModel):
    """本次创作需求；热点是可选输入，不覆盖用户目标。"""

    goal: str = Field(min_length=1)
    topic: str = ""
    content_type: str = "分享"
    use_trends: bool = True
    needs_research: bool = True
    supplied_materials: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)

    @field_validator("goal")
    @classmethod
    def nonblank_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("创作目标不能为空")
        return value.strip()


class TrendItem(BaseModel):
    """外部或用户提供的热点；时间与来源可追溯，不虚构热度。"""

    title: str = Field(min_length=1)
    url: str
    summary: str = ""
    keywords: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)


class TaskView(BaseModel):
    """面向 CLI 的任务状态视图。"""

    task_id: str
    topic: str
    status: str
    current_version: int
    draft: Draft | None = None
    published: bool = False
    published_version: int | None = None
    history: list[dict] = Field(default_factory=list)
    request: ContentRequest | None = None
    profile: UserProfile = Field(default_factory=UserProfile)
    trends: list[TrendItem] = Field(default_factory=list)
    brief: ResearchBrief | None = None
    review: ReviewReport | None = None
    last_error: str | None = None
    publish_result: PublishResult | None = None


class WorkflowInterrupt(BaseModel):
    """可供客户端展示并精确恢复的人工暂停。"""

    id: str
    node: str
    kind: str
    value: dict[str, Any] = Field(default_factory=dict)
    actions: list[str] = Field(default_factory=list)


class WorkflowView(BaseModel):
    """检查点的公开视图，不包含完整图状态、提示词或模型消息。"""

    thread_id: str
    checkpoint_id: str | None = None
    status: Literal["not_started", "ready", "waiting_input", "failed", "completed", "inconsistent", "queued", "running"]
    next_nodes: list[str] = Field(default_factory=list)
    interrupts: list[WorkflowInterrupt] = Field(default_factory=list)
    error: str | None = None
