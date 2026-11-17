"""可信应用组装的 Agent 白名单；模型输出只能选择已登记角色。"""
from __future__ import annotations

from collections.abc import Mapping

from ..models.base import AgentRole
from ..models.collaboration import (
    AgentDefinition, DelegateDecision, DelegationResult, DelegationTask,
    FinishDecision, SupervisorContext, SupervisorDecision,
)
from ..skills import AgentSkills, SkillLibrary
from ..execution import ExecutionConfig, ROLE_TOOLS, SKILL_TOOLS
from .base import SkillEnabledAgent
from .supervisor import SUPERVISOR_RESPONSIBILITIES
from .decisions import parse_supervisor_decision


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
        decision = parse_supervisor_decision(value, context)
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
            if task.target_agent == "review":
                draft = next((ref for ref in task.input_artifacts if ref.kind == "draft"), None)
                reviewed = next((ref for ref in context.artifacts
                                 if ref.kind == "review" and draft is not None and ref.version == draft.version), None)
                if reviewed is not None:
                    raise ValueError(f"草稿 v{draft.version} 已完成审核；必须先生成新草稿版本，不能重复审核")
            ideas = next((ref for ref in context.artifacts if ref.kind == "ideas"), None)
            if context.idea_feedback and task.target_agent != "ideation":
                raise ValueError("用户要求重新生成选题，需要先委派 ideation，并落实 idea_feedback")
            if ideas and task.target_agent in {"research", "creation"}:
                selection = context.idea_selection
                if selection is None or selection.candidates != ideas:
                    raise ValueError("候选选题尚未由用户确认，不能继续调研或创作")
                if task.idea_index is not None and task.idea_index != selection.idea_index:
                    raise ValueError("委派选题序号必须与用户确认一致")
                if not (selection.idea in task.input_artifacts or
                        (ideas in task.input_artifacts and task.idea_index == selection.idea_index)):
                    raise ValueError("调研或创作必须引用用户确认的 idea，或候选集及已确认的 idea_index")
        elif isinstance(decision, FinishDecision):
            if context.idea_feedback:
                raise ValueError("用户要求重新生成选题，尚不能结束")
            draft = next((ref for ref in context.artifacts if ref.kind == "draft"), None)
            if context.idea_selection and draft and draft.version <= context.idea_selection.draft_version_at_selection:
                raise ValueError("用户已重新确认选题，必须围绕选定方向交付新草稿并重新审核")
        return decision


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
        available = getattr(agents.get(role), "available_tools", SKILL_TOOLS) if specialist else ()
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
