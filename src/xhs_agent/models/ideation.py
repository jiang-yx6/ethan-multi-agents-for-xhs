"""选题 Agent 的输出模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class IdeaCandidate(BaseModel):
    """候选选题及其与用户需求、画像、热点的关联。"""

    title: str          # 具体选题标题
    why: str            # 选题价值 / 为什么值得写
    audience: str       # 目标受众
    angle: str          # 切入角度
    profile_match: str = ""
    content_type: str = "分享"
    trend_urls: list[str] = Field(default_factory=list)
    required_materials: list[str] = Field(default_factory=list)
