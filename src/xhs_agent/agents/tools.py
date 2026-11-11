"""可信应用注入的工具；每次委派创建独立的工具会话。"""
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from ..llm import ToolDefinition


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LoadSkillArgs(ToolArgs):
    skill_id: str = Field(min_length=1)


class ReadResourceArgs(LoadSkillArgs):
    relative: str = Field(min_length=1)


@dataclass(frozen=True)
class ToolResult:
    content: str
    images: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable

    def definition(self):
        return ToolDefinition(name=self.name, description=self.description, input_schema=self.args_model.model_json_schema())

    def invoke(self, arguments):
        result = self.handler(self.args_model.model_validate(arguments))
        return result if isinstance(result, ToolResult) else ToolResult(str(result))


def skill_tools(skills):
    return (
        AgentTool("load_skill", "加载当前角色已授权技能正文和资源目录", LoadSkillArgs,
                  lambda args: skills.load(args.skill_id).model_dump_json()),
        AgentTool("read_skill_resource", "读取已授权技能的参考文件", ReadResourceArgs,
                  lambda args: skills.read_resource(args.skill_id, args.relative)),
    )
