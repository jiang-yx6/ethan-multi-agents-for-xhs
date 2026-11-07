"""研究 Agent 的输入、简报、来源与证据模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .base import ContentRequest, TrendItem, UserProfile
from .ideation import IdeaCandidate


class ResearchRequest(BaseModel):
    """研究 Agent 的输入：一个已选定的选题 + 上下文。"""

    topic: str

    audience: str | None = None
    objective: str | None = None
    scenario: str | None = None
    style: str | None = None

    constraints: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)

    supplied_materials: list[str] = Field(default_factory=list)
    content_request: ContentRequest | None = None
    profile: UserProfile = Field(default_factory=UserProfile)
    idea: IdeaCandidate | None = None
    trends: list[TrendItem] = Field(default_factory=list)


class Source(BaseModel):
    """简报中引用的来源。"""

    title: str
    url: str
    retrieved_at: str | None = None
    published_at: str | None = None


class Evidence(BaseModel):
    """支持写作的原文片段；Mock 数据不能作为真实事实。"""

    source_url: str
    excerpt: str
    claim: str = ""
    limitations: str = ""
    is_mock: bool = False


class ResearchBrief(BaseModel):
    """研究 Agent 产出的结构化选题简报。"""

    topic: str
    target_audience: str
    pain_points: list[str]
    angles: list[str]
    keywords: list[str]
    tags: list[str]
    style: str = "自然、具体、简洁"
    sources: list[Source] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    supplied_materials: list[str] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)
    content_type: str = "分享"
    objective: str = ""
