"""领域模型（Pydantic）。"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    """带时区的 UTC 当前时间。"""
    return datetime.now(timezone.utc)


class ResearchBrief(BaseModel):
    """研究 Agent 产出的结构化选题简报。"""

    topic: str
    target_audience: str
    pain_points: list[str]
    angles: list[str]
    keywords: list[str]
    tags: list[str]


class ImageRef(BaseModel):
    """图片引用（未来可替换为真实平台上传后返回的 URL）。"""

    description: str
    source: str = "mock://placeholder"


class Draft(BaseModel):
    """一篇内容草稿（含版本与批准状态）。"""

    version: int = 0
    title: str
    body: str
    images: list[ImageRef] = Field(default_factory=list)
    approved: bool = False
    approved_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class PublishResult(BaseModel):
    """发布结果。"""

    published: bool
    reason: str
    draft_version: int
    task_id: str


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
