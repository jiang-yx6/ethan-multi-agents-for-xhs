"""可信应用组装的 Agent 白名单；模型输出只能选择已登记角色。"""
from __future__ import annotations

from collections.abc import Mapping

from ..models.base import AgentRole
from ..models.collaboration import (
    AgentDefinition, ArtifactReference, DelegateDecision, DelegationResult, DelegationTask,
    FinishDecision, SupervisorContext, SupervisorDecision,
)
from ..skills import AgentSkills, SkillLibrary
from ..execution import ExecutionConfig, ROLE_TOOLS
from .base import SKILL_READ_TOOLS, SkillEnabledAgent
from .supervisor import SUPERVISOR_DECISION_ADAPTER, SUPERVISOR_RESPONSIBILITIES


class AgentRegistry:
    """注册定义可独立于实现存在；负责目录、实例绑定与权限校验。"""

    def __init__(self, skill_library: SkillLibrary):
        self._library = skill_library
        self._definitions: dict[AgentRole, AgentDefinition] = {}
        self._agents: dict[AgentRole, object] = {}

    def register(self, definition: AgentDefinition, agent: object | None = None) -> None:
        identifier = definition.agent_id
        if identifier in self._definitions:
            raise ValueError(f"Agent 已注册：{identifier}")
        if agent is not None and any(agent is existing for existing in self._agents.values()):
            raise ValueError("不同角色必须使用独立 Agent 实例，避免权限互相覆盖")
        granted = {skill.id for skill in self._library.for_agent(identifier).catalog()}
        if not set(definition.skill_ids).issubset(granted):
            raise PermissionError("Agent 注册不能扩大技能库的角色授权")
        if not set(definition.tool_names).issubset(ROLE_TOOLS[identifier]):
            raise ValueError("注册表只能授权该角色当前已实现的工具")
        if isinstance(agent, SkillEnabledAgent):
            agent.bind_skills(self._library.for_agent(identifier, skill_ids=definition.skill_ids))
            agent.bind_tools(definition.tool_names)
        self._definitions[identifier] = definition
        if agent is not None:
            self._agents[identifier] = agent

    def definition(self, agent_id: str) -> AgentDefinition:
        definition = self._definitions.get(agent_id)
        if definition is None or not definition.enabled:
            raise PermissionError(f"Agent 未注册或已禁用：{agent_id}")
        return definition

    def catalog(self, *, delegatable_only: bool = True) -> tuple[AgentDefinition, ...]:
        return tuple(item for item in self._definitions.values()
                     if item.enabled and (item.delegatable or not delegatable_only))

    def agent(self, agent_id: str) -> object:
        """取得应用注入的实现；团队模式的专业角色绑定统一 run 适配器。"""
        self.definition(agent_id)
        if agent_id not in self._agents:
            raise RuntimeError(f"Agent 尚未绑定执行实现：{agent_id}")
        return self._agents[agent_id]

    def skills_for(self, agent_id: str) -> AgentSkills:
        definition = self.definition(agent_id)
        return self._library.for_agent(definition.agent_id, skill_ids=definition.skill_ids)

    def tools_for(self, agent_id: str) -> tuple[str, ...]:
        return self.definition(agent_id).tool_names

    def validate_delegation(self, task: DelegationTask) -> AgentDefinition:
        definition = self.definition(task.target_agent)
        if not definition.delegatable:
            raise PermissionError("目标 Agent 不允许被委派")
        return definition

    def validate_result(self, task: DelegationTask, result: DelegationResult) -> DelegationResult:
        self.validate_delegation(task)
        result = DelegationResult.model_validate(result)
        result.validate_for(task)
        return result

    def validate_decision(self, value: str | dict | SupervisorDecision,
                          context: SupervisorContext) -> SupervisorDecision:
        decision = (SUPERVISOR_DECISION_ADAPTER.validate_json(value) if isinstance(value, str)
                    else SUPERVISOR_DECISION_ADAPTER.validate_python(value))
        if isinstance(decision, DelegateDecision):
            task = decision.task
            self.validate_delegation(task)
            advertised = {item.agent_id for item in context.available_agents if item.enabled and item.delegatable}
            if task.target_agent not in advertised:
                raise PermissionError("目标 Agent 不在当前决策上下文的可用目录中")
            if task.task_id != context.task_id:
                raise ValueError("委派必须属于当前任务")
            if task.delegation_id in {record.task.delegation_id for record in context.history}:
                raise ValueError("新的决策不能重复使用已完成的委派 ID")
            if context.history:
                previous = context.history[-1]
                if (previous.result.status != "completed"
                        and len(context.user_messages) <= previous.user_message_count
                        and task.model_dump(exclude={"delegation_id"}) == previous.task.model_dump(exclude={"delegation_id"})):
                    raise ValueError("上一委派尚未完成；需要调整要求、输入或完成条件，或向用户提问，不能原样重复委派")
            self._validate_references(task.input_artifacts, context)
        elif isinstance(decision, FinishDecision):
            self._validate_references(decision.artifacts, context)
        return decision

    @staticmethod
    def _validate_references(refs: tuple[ArtifactReference, ...], context: SupervisorContext) -> None:
        available = {(ref.artifact_id, ref.version): ref for ref in context.artifacts}
        keys = [(ref.artifact_id, ref.version) for ref in refs]
        if len(keys) != len(set(keys)):
            raise ValueError("产物引用不能重复")
        if any(ref.task_id != context.task_id or available.get((ref.artifact_id, ref.version)) != ref
               for ref in refs):
            raise ValueError("决策只能引用当前上下文已有的产物和版本")


def build_agent_registry(skill_library: SkillLibrary,
                         agents: Mapping[AgentRole, object] | None = None,
                         execution: ExecutionConfig | None = None) -> AgentRegistry:
    """可信默认配置；技能取角色现有授权的快照，访问时仍检查实时撤销/禁用。"""
    descriptions: dict[AgentRole, tuple[str, tuple[str, ...]]] = {
        "supervisor": ("主 Agent：协调内容团队", SUPERVISOR_RESPONSIBILITIES),
        "ideation": ("选题 Agent", ("结合用户需求、画像和可选热点提出选题与所需素材",)),
        "research": ("调研 Agent", ("整理用户材料、核实事实与来源、指出证据缺口",)),
        "creation": ("创作 Agent", ("根据要求与资料创作内容，并落实修改意见",)),
        "review": ("审核 Agent", ("检查内容质量、来源与约束，返回问题及修改建议",)),
        "publishing": ("发布服务", ("在服务层确认和校验后执行平台提交与回执核验",)),
    }
    agents = agents or {}
    if set(agents) - set(descriptions):
        raise ValueError("不能绑定未知 Agent 角色")
    registry = AgentRegistry(skill_library)
    for role, (description, responsibilities) in descriptions.items():
        specialist = role not in {"supervisor", "publishing"}
        settings = (execution or ExecutionConfig()).for_role(role)
        available = getattr(agents.get(role), "available_tools", SKILL_READ_TOOLS) if specialist else ()
        granted = tuple(skill.id for skill in skill_library.for_agent(role).catalog())
        tools = settings.tools if specialist and settings.tools is not None else available
        if not set(tools).issubset(available):
            raise ValueError(f"{role} 请求的工具未配置执行实现")
        registry.register(AgentDefinition(
            agent_id=role, description=description, responsibilities=responsibilities,
            skill_ids=settings.skill_ids if specialist and settings.skill_ids is not None else granted,
            tool_names=tools, delegatable=specialist,
        ), agents.get(role))
    return registry
