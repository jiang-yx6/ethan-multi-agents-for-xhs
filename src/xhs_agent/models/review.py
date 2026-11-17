"""审核 Agent 的问题与报告模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ReviewIssue(BaseModel):
    message: str
    # 语义审核以建议为主；只有程序能够确认的致命问题才显式标记 error。
    severity: Literal["warning", "error"] = "warning"
    target: Literal["create", "research"] = "create"


class ReviewReport(BaseModel):
    """自动审核只提供建议，人工批准仍由状态机控制。"""

    draft_version: int
    issues: list[ReviewIssue] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)
