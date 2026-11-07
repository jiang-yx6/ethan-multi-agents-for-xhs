"""创作 Agent 的草稿与图片引用模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .base import utcnow


class ImageRef(BaseModel):
    """图片引用（未来可替换为真实平台上传后返回的 URL）。"""

    description: str
    source: str = "mock://placeholder"


class DraftContent(BaseModel):
    """人工替换的完整内容；版本和批准状态由工作流管理。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    body: str
    images: list[ImageRef] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    content_type: str = "分享"


class Draft(DraftContent):
    """一篇内容草稿（含版本与批准状态）。"""

    version: int = 0
    revision_notes: list[str] = Field(default_factory=list)
    approved: bool = False
    approved_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
