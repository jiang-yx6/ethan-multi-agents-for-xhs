"""主 Agent 与专业 Agent 的协作契约；只传结构化任务、摘要及版本化产物引用。"""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, StrictInt, model_validator

from .base import AgentRole, ContentRequest, SpecialistRole, UserProfile

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CollaborationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactReference(CollaborationModel):
    """逻辑引用，不是任意文件路径；内容解析与持久化由 TeamStore 负责。"""

    task_id: Text
    artifact_id: Text
    kind: Literal["material", "trends", "ideas", "idea", "brief", "draft", "review"]
    version: Annotated[StrictInt, Field(ge=1)]
    summary: Text


class AgentDefinition(CollaborationModel):
    agent_id: AgentRole
    description: Text
    responsibilities: tuple[Text, ...] = Field(min_length=1)
    skill_ids: tuple[Text, ...] = ()
    tool_names: tuple[Text, ...] = ()
    enabled: bool = True
    delegatable: bool = False

    @model_validator(mode="after")
    def check_permissions(self):
        if self.agent_id in {"supervisor", "publishing"} and self.delegatable:
            raise ValueError("主 Agent 和发布服务不能作为专业 Agent 被委派")
        for values in (self.skill_ids, self.tool_names):
            if len(values) != len(set(values)):
                raise ValueError("技能和工具授权不能重复")
        return self


def _check_artifacts(task_id: str, artifacts: tuple[ArtifactReference, ...]) -> None:
    if any(ref.task_id != task_id for ref in artifacts):
        raise ValueError("产物必须属于当前任务")
    keys = [(ref.artifact_id, ref.version) for ref in artifacts]
    if len(keys) != len(set(keys)):
        raise ValueError("产物引用不能重复")


class DelegationTask(CollaborationModel):
    """task_id 标识业务任务；delegation_id 标识一次委派，重试应复用原 ID。"""

    task_id: Text
    delegation_id: Text = Field(default_factory=lambda: uuid4().hex)
    target_agent: SpecialistRole
    instruction: Text
    input_artifacts: tuple[ArtifactReference, ...] = ()
    materials: tuple[Text, ...] = ()
    acceptance_criteria: tuple[Text, ...] = Field(min_length=1)
    idea_index: Annotated[StrictInt, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def check_inputs(self):
        _check_artifacts(self.task_id, self.input_artifacts)
        kinds = [ref.kind for ref in self.input_artifacts]
        if len(kinds) != len(set(kinds)):
            raise ValueError("同次委派每类输入产物只能提供一个；多个素材请合并为 material 或 materials")
        return self


class DelegationResult(CollaborationModel):
    task_id: Text
    delegation_id: Text
    agent_id: SpecialistRole
    status: Literal["completed", "needs_input", "failed"]
    summary: Text
    artifacts: tuple[ArtifactReference, ...] = ()
    missing_information: tuple[Text, ...] = ()
    failure_reason: Text | None = None

    @model_validator(mode="after")
    def check_outcome(self):
        _check_artifacts(self.task_id, self.artifacts)
        if (self.status == "needs_input") != bool(self.missing_information):
            raise ValueError("只有 needs_input 状态需要且必须提供缺失信息")
        if (self.status == "failed") != (self.failure_reason is not None):
            raise ValueError("只有 failed 状态需要且必须提供失败原因")
        return self

    def validate_for(self, task: DelegationTask) -> None:
        if (self.task_id, self.delegation_id, self.agent_id) != (
            task.task_id, task.delegation_id, task.target_agent
        ):
            raise ValueError("执行结果与委派的任务、委派 ID 或目标 Agent 不匹配")


class DelegationRecord(CollaborationModel):
    task: DelegationTask
    result: DelegationResult
    user_message_count: Annotated[StrictInt, Field(ge=0)] = 0

    @model_validator(mode="after")
    def check_pair(self):
        self.result.validate_for(self.task)
        return self


class SupervisorContext(CollaborationModel):
    """主 Agent 的决策输入；专业 Agent 完整对话不进入此上下文。"""

    task_id: Text
    request: ContentRequest
    profile: UserProfile
    available_agents: tuple[AgentDefinition, ...]
    artifacts: tuple[ArtifactReference, ...] = ()
    history: tuple[DelegationRecord, ...] = ()
    user_messages: tuple[Text, ...] = ()
    remaining_rounds: int = Field(default=20, ge=0)

    @model_validator(mode="after")
    def check_context(self):
        _check_artifacts(self.task_id, self.artifacts)
        if any(record.task.task_id != self.task_id for record in self.history):
            raise ValueError("协作历史必须属于当前任务")
        ids = [record.task.delegation_id for record in self.history]
        if len(ids) != len(set(ids)):
            raise ValueError("协作历史的委派 ID 不能重复")
        agents = [agent.agent_id for agent in self.available_agents]
        if len(agents) != len(set(agents)):
            raise ValueError("可用 Agent 不能重复")
        return self


class DelegateDecision(CollaborationModel):
    action: Literal["delegate"]
    reason: Text
    task: DelegationTask


class AskUserDecision(CollaborationModel):
    action: Literal["ask_user"]
    reason: Text
    question: Text
    missing_information: tuple[Text, ...] = Field(min_length=1)


class FinishDecision(CollaborationModel):
    action: Literal["finish"]
    reason: Text
    summary: Text
    artifacts: tuple[ArtifactReference, ...] = ()


SupervisorDecision = Annotated[
    DelegateDecision | AskUserDecision | FinishDecision, Field(discriminator="action")
]
