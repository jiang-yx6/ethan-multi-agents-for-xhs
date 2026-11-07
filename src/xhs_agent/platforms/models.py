"""平台契约：账号、内容和回执独立于 Agent 与浏览器实现。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..models.base import utcnow


class AccountRef(BaseModel):
    platform: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    account_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

    @field_validator("account_id")
    @classmethod
    def normalize_account_id(cls, value: str) -> str:
        # Windows 文件名不区分大小写，不能让两个 DB 账号共享同一个浏览器目录。
        value = value.lower()
        reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                    *(f"lpt{i}" for i in range(1, 10))}
        if value in reserved:
            raise ValueError("账号 ID 不能使用系统保留文件名")
        return value


class AccountIdentity(BaseModel):
    state: Literal["authenticated", "logged_out", "unknown"]
    user_id: str = ""
    nickname: str = ""
    message: str = ""


class PlatformAccount(AccountRef):
    label: str = ""
    user_id: str = ""
    nickname: str = ""


class PlatformCapabilities(BaseModel):
    platform: str
    login: bool = True
    image_publish: bool = False
    video_publish: bool = False
    draft: bool = False
    schedule: bool = False
    automatic_verification: bool = False
    analytics: bool = False


class MediaAsset(BaseModel):
    path: str
    sha256: str


class PlatformContent(BaseModel):
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)
    images: list[MediaAsset] = Field(default_factory=list)


class ValidationReport(BaseModel):
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def require_valid(self) -> None:
        if self.errors:
            raise ValueError("平台预检失败：" + "；".join(self.errors))


class PublishRequest(BaseModel):
    task_id: str
    draft_version: int
    account: AccountRef
    expected_user_id: str
    content: PlatformContent
    content_hash: str


ReceiptState = Literal["submitted", "published", "unknown", "failed"]


class PublishReceipt(BaseModel):
    state: ReceiptState
    message: str
    platform_post_id: str = ""
    url: str = ""
    verification: Literal["none", "platform", "manual"] = "none"
    updated_at: datetime = Field(default_factory=utcnow)


class PublishJob(BaseModel):
    job_id: str
    request: PublishRequest
    state: Literal["ready", "submitting", "submitted", "published", "unknown", "failed"] = "ready"
    receipt: PublishReceipt | None = None
