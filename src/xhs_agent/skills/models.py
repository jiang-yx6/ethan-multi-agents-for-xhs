"""技能注册信息与 Agent 访问配置。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models.base import AGENT_ROLES, AgentRole


class SkillSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    revision: str = ""
    original_path: str = ""
    license: str = "unknown"
    license_file: str = ""


class SkillDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    path: str
    source: SkillSource
    enabled: bool = True
    required_tools: list[str] = Field(default_factory=list)
    resources: dict[str, str] = Field(default_factory=dict)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    notes: str = ""


class SkillRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    skills: list[SkillDefinition] = Field(default_factory=list)
    agents: dict[AgentRole, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_grants(self):
        identifiers = [skill.id for skill in self.skills]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("技能 ID 重复")
        for role, grants in self.agents.items():
            if len(grants) != len(set(grants)):
                raise ValueError(f"{role} 的技能授权重复")
            if any(identifier not in identifiers for identifier in grants):
                raise ValueError(f"{role} 引用了未注册技能")
        return self


class SkillSummary(BaseModel):
    """常驻目录只包含路由所需元信息，不暴露其他角色的技能。"""

    id: str
    name: str
    description: str
    required_tools: list[str]


class LoadedSkill(BaseModel):
    id: str
    instructions: str
    resources: list[str]
    required_tools: list[str]
    notes: str
