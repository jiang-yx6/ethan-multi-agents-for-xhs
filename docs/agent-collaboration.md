# Agent 协作协议（第一阶段）

本阶段定义主 Agent 与专业 Agent 交互的契约，以及应用组装的角色、技能和工具白名单。当前业务仍使用既有 LangGraph 流程；真实主 Agent 模型、专业 Agent 的统一执行适配器、动态团队图与 Web 协作界面属于后续阶段。

## 主 Agent 的职责

1. 理解用户目标、画像、素材和约束，识别缺失信息。
2. 根据现有产物拆分任务，选择已注册且允许委派的专业 Agent。
3. 提供明确的任务要求、输入资料和完成条件。
4. 检查执行状态、摘要和产物，再决定委派、提问或结束。

`agents/supervisor.py` 提供职责常量、`supervisor_system_prompt()` 和决策 schema/解析器。`agents/base.py` 提供两个供后续实现遵守的 Protocol：

```python
class SupervisorAgent(Protocol):
    def decide(self, context: SupervisorContext) -> SupervisorDecision: ...

class DelegatedAgent(Protocol):
    def run(self, task: DelegationTask) -> DelegationResult: ...
```

现有 `ideate/research/create/review` 接口继续工作，尚未实现统一的 `run()` 适配；注册已有实例不会自动将它变为 `DelegatedAgent`。

## 统一的数据模型

模型位于 `src/xhs_agent/models/collaboration.py`，也可以从 `xhs_agent.models` 导入。共享角色类型位于 `models/base.py`。协议拒绝额外字段，字符串不能空白；协议集合使用 tuple，并以 JSON 数组传输。

| 模型 | 主要字段与含义 |
| --- | --- |
| `ArtifactReference` | `task_id`、`artifact_id`、`kind`、正整数 `version`、`summary`；只引用产物，不携带专业 Agent 的完整对话 |
| `DelegationTask` | `task_id`、`delegation_id`、`target_agent`、`instruction`、`input_artifacts`、`materials`、`acceptance_criteria` |
| `DelegationResult` | 业务任务与委派 ID、`agent_id`、`status`、`summary`、`artifacts`、`missing_information`、`failure_reason` |
| `DelegationRecord` | 一对匹配的 `task` 和 `result` |
| `SupervisorContext` | 需求、画像、当前可用 Agent 目录、产物目录、执行历史和用户补充信息 |
| `AgentDefinition` | 角色、描述、职责、技能 ID、工具名称、启用状态和是否可委派 |

`task_id` 是整个内容任务的 ID，`delegation_id` 是其中一次委派的 ID。没有指定委派 ID 时生成 UUID；新的委派使用新 ID，执行重试复用原委派对象及 ID。协议本身不实现持久化、幂等调度或自动重试。

产物类型支持 `material/trends/ideas/idea/brief/draft/review`。引用是逻辑标识，不能用于直接读取任意文件。当前阶段尚未实现产物解析服务；后续由服务层根据业务任务解析输入、加载需求与画像，并检查产物实际存在及版本。主 Agent 的 `artifacts` 目录应由可信服务层提供。

一次委派示例：

```json
{
  "task_id": "task-1",
  "delegation_id": "delegation-1",
  "target_agent": "review",
  "instruction": "检查草稿是否满足用户要求，指出证据不足的段落",
  "input_artifacts": [{
    "task_id": "task-1",
    "artifact_id": "draft-1",
    "kind": "draft",
    "version": 1,
    "summary": "基于用户出游素材创作的第一版草稿"
  }],
  "materials": ["用户要求：保留实际路线，不添加没有去过的景点"],
  "acceptance_criteria": ["列出具体问题与修改建议", "标记缺少来源的事实"]
}
```

执行结果有三个状态：

- `completed`：完成当前委派，返回摘要与可选产物；不能同时声明缺失信息或失败原因。完成某次审核委派不代表稿件已经通过审核，更不代表用户批准发布。
- `needs_input`：无法继续，需要填写非空 `missing_information`。
- `failed`：执行失败，需要填写非空 `failure_reason`。

`needs_input/failed` 也允许返回已有的部分产物，主 Agent 根据结果决定下一步。`DelegationRecord` 和 `registry.validate_result(task, result)` 会检查任务、委派 ID 和 Agent 是否匹配。

## 主 Agent 的三类决策

`SupervisorDecision` 是以 `action` 为判别字段的联合类型：

| action | 必要内容 |
| --- | --- |
| `delegate` | `reason`、完整的 `task: DelegationTask` |
| `ask_user` | `reason`、`question`、非空 `missing_information` |
| `finish` | `reason`、`summary`，可选的 `artifacts` |

解析 JSON 时使用 `SUPERVISOR_DECISION_ADAPTER`，需要执行目录与上下文检查时使用 `registry.validate_decision()`。后者会额外检查目标角色是否可委派、是否出现在当前目录中、业务任务是否匹配、委派 ID 是否重复，以及引用是否来自当前产物目录。

```python
from xhs_agent.agents import build_agent_registry
from xhs_agent.models import ContentRequest, SupervisorContext, UserProfile
from xhs_agent.skills import SkillLibrary

registry = build_agent_registry(SkillLibrary())
context = SupervisorContext(
    task_id="task-1",
    request=ContentRequest(goal="整理一篇周末出游笔记"),
    profile=UserProfile(),
    available_agents=registry.catalog(),
)
decision = registry.validate_decision({
    "action": "delegate",
    "reason": "先整理选题方向",
    "task": {
        "task_id": "task-1",
        "target_agent": "ideation",
        "instruction": "围绕周末出游提出三个可执行的选题",
        "acceptance_criteria": ["结合用户画像", "列明还需要的真实素材"],
    },
}, context)
assert decision.task.target_agent == "ideation"
```

schema 校验不能判断目标是否真正完成、事实是否真实，后续主 Agent 与业务服务需要检查内容。`finish` 表示结束本轮协作，不写入发布状态，也不代替人工批准。

## 注册表与权限

`agents/registry.py` 的 `AgentRegistry` 只能由可信应用代码组装；模型无法修改注册表，也不能通过输出模块路径加载 Python 对象。

默认定义六个角色：

| 角色 | 可委派 | 技能与工具 |
| --- | --- | --- |
| `supervisor` | 否 | 默认无技能、无工具；定义职责但尚未绑定模型实现 |
| `ideation` | 是 | 技能库中的选题授权；两个技能读取工具 |
| `research` | 是 | 技能库中的调研授权；两个技能读取工具 |
| `creation` | 是 | 技能库中的创作授权；两个技能读取工具 |
| `review` | 是 | 技能库中的审核授权；两个技能读取工具 |
| `publishing` | 否 | 发布服务；默认无技能读取工具 |

`catalog()` 默认只返回启用且可委派的角色，`catalog(delegatable_only=False)` 包含其他启用角色。`agent(id)` 只返回应用实际注入的实例，尚未绑定实现时会报错。仅有定义并不意味着模型与工具执行能力已接入。

`AgentDefinition.skill_ids` 与 `skills/registry.json` 的角色授权取交集。注册时禁止扩大角色授权；每次读取继续检查技能是否被撤销或禁用。注册表可进一步缩小单个角色范围，不能通过技能正文获得新权限。默认注册表取构建时的技能授权快照，增加授权后需要重建服务/注册表。

`tool_names` 当前只允许已实现的 `load_skill/read_skill_resource`。例如只授权 `load_skill`，Runtime 就不会向模型提供资源读取工具；模型即使自行生成资源读取调用，也会收到工具错误。技能的 `required_tools` 不会自动变成实际工具。

```python
from xhs_agent.agents import AgentRegistry
from xhs_agent.models import AgentDefinition

restricted_registry = AgentRegistry(SkillLibrary())
restricted_registry.register(AgentDefinition(
    agent_id="ideation",
    description="只使用选题评估技能的选题 Agent",
    responsibilities=("评估选题与画像的匹配程度",),
    skill_ids=("skill-topic-evaluator",),
    tool_names=("load_skill",),
    delegatable=True,
))
```

`TaskService.agent_registry` 已登记当前专业 Agent 和发布器实例，并绑定对应技能和工具范围。现有 LangGraph 节点继续调用原业务方法，检查点和 Web 人工暂停行为保持原有机制。自定义 Agent 若自行实现外部工具调用，需要遵守同样的权限契约；注册表不提供 Python 沙箱。

发布继续经过服务层的人工确认、账号核验、版本与指纹校验及防重复提交机制。主 Agent 决策不包含直接发布操作。
