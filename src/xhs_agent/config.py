"""各入口共用的流程设置。"""
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .llm import LLMConfig
from .execution import ExecutionConfig

ModelRole = Literal["supervisor", "ideation", "research", "creation", "review"]
MODEL_ROLES: tuple[ModelRole, ...] = ("supervisor", "ideation", "research", "creation", "review")


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    llm_config_path: str | None = None
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    supervisor_backend: Literal["llm", "mock"] = "llm"
    supervisor_llm: LLMConfig | None = None
    team_max_rounds: int = Field(default=20, ge=1, le=100)
    ideation_backend: Literal["llm", "mock"] = "llm"
    ideation_llm: LLMConfig | None = None
    research_backend: Literal["llm", "mock"] = "mock"
    research_llm: LLMConfig | None = None
    creation_backend: Literal["llm", "mock"] = "mock"
    creation_llm: LLMConfig | None = None
    review_backend: Literal["llm", "rules", "mock"] = "rules"
    review_llm: LLMConfig | None = None
    ideation_max_model_calls: int = Field(default=6, ge=1, le=20)
    ideation_max_tool_calls: int = Field(default=12, ge=0, le=40)
    ideation_max_output_retries: int = Field(default=1, ge=0, le=3)


def load_role_llm(path: str | Path, role: str, *, required: bool = True) -> LLMConfig | None:
    """读取指定角色的独立配置；缺少该角色时明确失败。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError):
        raise ValueError("模型配置文件必须是有效的 UTF-8 JSON") from None
    if isinstance(data, dict) and role not in data and not required:
        return None
    if not isinstance(data, dict) or not isinstance(data.get(role), dict):
        raise ValueError(f"模型配置文件必须包含 {role} 配置对象")
    try:
        return LLMConfig.model_validate(data[role])
    except ValidationError:
        raise ValueError(f"{role} 模型配置无效，请检查 provider、model 和连接参数") from None
