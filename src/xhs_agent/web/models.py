"""HTTP 输入模型：写操作携带幂等 ID 和预期版本。"""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from ..models import ContentRequest, TrendItem, UserProfile
from ..platforms import AccountRef
from ..config import ModelRole


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTask(InputModel):
    request_id: UUID
    request: ContentRequest
    profile_id: str = "default"
    trends: list[TrendItem] = Field(default_factory=list, max_length=50)


class TaskAction(InputModel):
    request_id: UUID
    expected_version: int = Field(ge=0)


class ResumeAction(TaskAction):
    interrupt_id: str = Field(min_length=1, max_length=200)


class ChangeAction(ResumeAction):
    comment: str = Field(min_length=1, max_length=10000)


class AnswerAction(ResumeAction):
    answer: str = Field(min_length=1, max_length=10000)

    @field_validator("answer")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("回复不能为空")
        return value.strip()


class MessageAction(ResumeAction):
    text: str = Field(min_length=1, max_length=10000)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("消息不能为空")
        return value.strip()


class ImageInput(InputModel):
    description: str = "配图"
    media_id: UUID | None = None
    existing_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def reference(self):
        if (self.media_id is None) == (self.existing_index is None):
            raise ValueError("图片须引用已上传媒体或当前版本中的图片")
        return self


class DraftAction(ResumeAction):
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=50000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    images: list[ImageInput] = Field(default_factory=list, max_length=18)
    content_type: str = "分享"


class TargetInput(InputModel):
    expected_version: int = Field(ge=1)
    account: AccountRef


class PublishAction(ResumeAction):
    account: AccountRef
    confirmed: Literal[True]


class ConfirmAction(TaskAction):
    note_url: str = Field(min_length=1, max_length=2000)
    confirmed: Literal[True]


class ProfileInput(InputModel):
    profile: UserProfile


class AccountInput(InputModel):
    account: AccountRef
    label: str = Field(default="", max_length=100)


class LoginAction(InputModel):
    request_id: UUID
    timeout: int = Field(default=180, ge=1, le=600)


class ModelSettings(InputModel):
    backend: Literal["llm", "mock", "rules"] = "llm"
    provider: str = Field(default="deepseek", min_length=1, max_length=100)
    model: str = Field(default="", max_length=200)
    credential_source: Literal["api_key", "environment"] = "api_key"
    api_key_env: str = Field(default="DEEPSEEK_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    protocol: Literal["openai", "anthropic"] | None = None
    base_url: str | None = None
    trust_env: bool = False
    max_tokens: int = Field(default=4096, gt=0)
    timeout: float = Field(default=120, gt=0)

    @model_validator(mode="before")
    @classmethod
    def legacy_environment_settings(cls, value):
        if isinstance(value, dict) and "credential_source" not in value and "api_key_env" in value:
            return {**value, "credential_source": "api_key" if value.get("api_key") else "environment"}
        return value

    @model_validator(mode="after")
    def validate_config(self):
        if self.backend == "llm":
            from ..llm import LLMConfig
            LLMConfig(provider=self.provider, model=self.model, api_key_env=self.api_key_env,
                      protocol=self.protocol, base_url=self.base_url, trust_env=self.trust_env,
                      max_tokens=self.max_tokens, timeout=self.timeout)
        return self


class ModelSettingsUpdate(ModelSettings):
    # 写入专用字段；不存入 settings JSON，也不加入公开响应。
    api_key: SecretStr | None = Field(default=None, repr=False)
    clear_api_key: bool = False

    @field_validator("api_key", mode="before")
    @classmethod
    def validate_api_key(cls, value):
        if value is None:
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str):
            raise ValueError("API Key 必须是文本")
        raw = raw.strip()
        if not raw:
            return None
        if len(raw) > 4096 or any(character.isspace() for character in raw):
            raise ValueError("API Key 不能包含空白字符，且长度不能超过 4096")
        return SecretStr(raw)

    @model_validator(mode="after")
    def validate_credential_change(self):
        if self.api_key and self.clear_api_key:
            raise ValueError("请分别执行替换或清除密钥")
        if self.api_key and self.credential_source != "api_key":
            raise ValueError("请切换到直接填写密钥方式后保存")
        return self


class CopyModelSettings(InputModel):
    source_role: ModelRole
