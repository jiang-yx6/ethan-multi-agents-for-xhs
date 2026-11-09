"""各入口共用的流程设置。"""
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .llm import LLMConfig


class WorkflowConfig(BaseModel):
    max_auto_revisions: int = Field(default=2, ge=0, le=5)
    ideation_backend: Literal["llm", "mock"] = "llm"
    ideation_llm: LLMConfig | None = None
    ideation_max_model_calls: int = Field(default=6, ge=1, le=20)
    ideation_max_tool_calls: int = Field(default=12, ge=0, le=40)
    ideation_max_output_retries: int = Field(default=1, ge=0, le=3)


def load_ideation_llm(path: str | Path) -> LLMConfig:
    """读取按角色配置文件中的 ideation；其他角色本次仍按既有方式运行。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError):
        raise ValueError("模型配置文件必须是有效的 UTF-8 JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("ideation"), dict):
        raise ValueError("模型配置文件必须包含 ideation 配置对象")
    try:
        return LLMConfig.model_validate(data["ideation"])
    except ValidationError:
        raise ValueError("ideation 模型配置无效，请检查 provider、model 和连接参数") from None
