"""专业 Agent 的执行预算、提示词、权限及外部工具配置。"""
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from .llm import LLMConfig, LLMConfigurationError
from .models.base import SpecialistRole

SKILL_TOOLS = ("load_skill", "read_skill_resource")
ROLE_TOOLS = {
    "supervisor": (), "publishing": (),
    "ideation": SKILL_TOOLS,
    "research": (*SKILL_TOOLS, "web_search", "web_fetch"),
    "creation": (*SKILL_TOOLS, "generate_image", "inspect_image"),
    "review": (*SKILL_TOOLS, "run_rules", "inspect_image"),
}


class ExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoleExecution(ExecutionModel):
    prompt: str = ""
    skill_ids: tuple[str, ...] | None = None
    tools: tuple[str, ...] | None = None
    max_model_calls: int = Field(default=8, ge=1, le=20)
    max_tool_calls: int = Field(default=12, ge=0, le=40)
    max_output_retries: int = Field(default=2, ge=0, le=3)
    supports_vision: bool = False

    def runtime_options(self):
        return self.model_dump(include={"max_model_calls", "max_tool_calls", "max_output_retries"})


class APISettings(ExecutionModel):
    base_url: str
    api_key: SecretStr | None = Field(default=None, repr=False)
    api_key_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    timeout: float = Field(default=120, gt=0)
    trust_env: bool = False

    @field_validator("base_url")
    @classmethod
    def check_url(cls, value):
        try:
            return LLMConfig(provider="custom", model="tool", base_url=value).base_url
        except ValidationError:
            raise ValueError("工具服务地址必须是无凭证、无查询参数的 HTTP(S) 地址") from None

    def key(self):
        value = self.api_key.get_secret_value() if self.api_key is not None else os.environ.get(self.api_key_env, "")
        if not value or any(char.isspace() for char in value):
            raise LLMConfigurationError("外部工具密钥未配置或包含空白字符")
        return value

    def endpoint(self, suffix):
        # 复用现有无凭证、无查询参数的服务地址校验。
        return LLMConfig(provider="custom", model="tool", base_url=self.base_url).base_url + suffix


class SearchSettings(APISettings):
    provider: Literal["tavily"] = "tavily"
    base_url: str = "https://api.tavily.com"
    api_key_env: str = Field(default="TAVILY_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class ImageSettings(APISettings):
    provider: Literal["openai"] = "openai"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = Field(default="XHS_IMAGE_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    model: str = Field(min_length=1)
    size: str = "1024x1536"
    supports_edit: bool = True
    response_format: Literal["b64_json"] | None = None


class ExecutionConfig(ExecutionModel):
    roles: dict[SpecialistRole, RoleExecution] = Field(default_factory=dict)
    search: SearchSettings | None = None
    images: ImageSettings | None = None

    def for_role(self, role):
        return self.roles.get(role, RoleExecution())


def load_execution_config(path: str | Path) -> ExecutionConfig:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return ExecutionConfig.model_validate(value.get("execution", {}))
    except (ValueError, AttributeError, ValidationError):
        raise ValueError("execution 配置无效，请检查角色、工具和服务连接参数") from None
