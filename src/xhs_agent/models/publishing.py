"""发布适配器的执行结果模型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class PublishResult(BaseModel):
    """发布结果。"""

    published: bool
    reason: str
    draft_version: int
    task_id: str
    status: Literal["submitted", "published", "unknown", "failed"] | None = None
    platform: str | None = None
    account_id: str | None = None
    job_id: str | None = None
    platform_post_id: str = ""
    url: str = ""
    verification: Literal["none", "platform", "manual"] = "none"
