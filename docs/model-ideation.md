# 真实模型选题与 Skill 调用

`TaskService` 默认创建 `LLMIdeationAgent`，在第一次执行选题时创建 Provider。模型通过 `xhs_agent.llm` 统一接口调用，兼容现有 OpenAI Chat Completions 和 Anthropic Messages 适配器。默认资料整理、创作、规则审核和 Mock 发布保持各自的实现。

运行完整任务还需要独立配置父 Agent：文件中提供 `supervisor` 对象，或设置 `XHS_SUPERVISOR_*`。选题的模型与密钥不会自动用于父 Agent，详见 [协作配置](agent-collaboration.md)。

## CLI 使用

先安装依赖：

```powershell
uv sync
```

方式一：修改 `examples/llm-config.json` 中的 `ideation` 配置，把 `YOUR_MODEL_ID` 改成你账号可用、支持工具调用的实际模型 ID，然后设置对应密钥：

```powershell
$env:DEEPSEEK_API_KEY = "填写你的密钥"
uv run xhs --llm-config examples/llm-config.json create "周末散步" --profile examples/profile.json --no-trends
```

该文件中的 `research/creation/review` 配置本次不启用。密钥通过 `api_key_env` 引用，不需要写入 JSON。全局参数须放在 `create` 前。

方式二：使用已有环境变量配置：

```powershell
$env:XHS_LLM_PROVIDER = "deepseek"
$env:XHS_LLM_MODEL = "填写实际模型ID"
$env:DEEPSEEK_API_KEY = "填写你的密钥"
uv run xhs create "周末散步" --profile examples/profile.json --no-trends
```

也可设置 `XHS_LLM_CONFIG` 指向角色配置文件。显式 `--llm-config` 优先于该路径变量；有配置文件时，Provider、模型及连接选项从其中的 `ideation` 对象读取，密钥按 `api_key_env` 或厂商预设解析。没有配置文件时，使用已有 `XHS_LLM_*` 配置规则。

选择成功后继续原有流程：

```powershell
uv run xhs answer TASK_ID --text "采用第一个方向"
uv run xhs status TASK_ID
```

查看已有任务、继续已有选题之后的流程不要求模型密钥。`LLMIdeationAgent` 延迟创建 Provider，读取状态或管理平台账号时不会调用模型。选题失败时保存原任务和错误，可配置好后执行：

```powershell
uv run xhs --llm-config examples/llm-config.json retry TASK_ID
```

模型配置或接口失败不会生成 Mock 选题。离线演示需显式选择：

```powershell
uv run xhs --mock-ideation --mock-supervisor create "周末散步"
```

## 模型与技能的执行过程

```text
需求 + 画像 + 可选热点 + 角色技能目录
              ↓
          model 节点
      ┌───────┴──────────┐
   请求工具            最终 JSON
      ↓                    ↓
   tools 节点          validate 节点
      ↓               ├─ 有效 → 返回 IdeaCandidate 列表
 返回技能/资源        └─ 无效 → 有限修正，再调用模型
      └──→ model 节点
```

`agents/runtime.py` 使用自定义 `ChatMessage` 和 `ChatRequest`，消息状态采用追加 reducer。工具处理完同轮全部调用后继续调用 Provider，保留 OpenAI 兼容服务的 `reasoning_content` 和 Anthropic thinking 原始块。每次选题运行都有独立消息状态，账号和任务上下文不会混用。

两个工具的角色都通过服务层绑定的访问器固定：

- `load_skill(skill_id)`：检查角色授权及摘要后返回正文和资源目录。
- `read_skill_resource(skill_id, relative)`：检查授权、资源别名及路径边界后读取参考资料。

模型不能传入另一角色覆盖访问范围。非法参数、越权或不可读资源作为工具错误返回，允许模型在调用上限内调整。两个工具只读文本，不会运行技能脚本、自动抓热搜或发布内容。技能中的联网步骤只能在未来提供相应受限工具后执行。

最终输出必须是 `{"ideas": [...]}` 对象，含 1～5 个 `IdeaCandidate`。输出通过 Pydantic 严格类型校验，并额外检查关键字段非空、`trend_urls` 只引用本次输入热点 URL。`use_trends=false` 时不把热点传给模型，返回的 `trend_urls` 也必须为空。该校验验证格式和来源引用范围，内容质量与事实仍需后续审核。

## Python 使用

```python
import os

from xhs_agent.config import AgentConfig
from xhs_agent.llm import LLMConfig
from xhs_agent.service import TaskService

service = TaskService(config=AgentConfig(
    ideation_llm=LLMConfig(
        provider="deepseek",
        model=os.environ["XHS_LLM_MODEL"],
        api_key_env="DEEPSEEK_API_KEY",
    ),
))
try:
    task = service.create_task("周末散步")
    ideas = service.candidate_ideas(task.task_id)
finally:
    service.close()
```

也可显式注入已有 Provider，适合自定义网关或测试：

```python
from xhs_agent.agents import LLMIdeationAgent
from xhs_agent.llm import create_llm_provider
from xhs_agent.service import TaskService

with create_llm_provider() as provider:
    service = TaskService(ideation_agent=LLMIdeationAgent(provider))
    try:
        task = service.create_task("周末散步")
    finally:
        service.close()
```

服务关闭时会关闭 Agent 自己创建的 Provider；外部注入的 Provider 由调用者管理。直接使用 Agent 时，需先绑定 `SkillLibrary().for_agent("ideation")`。

## 调用限制和验证

`AgentConfig` 提供：

| 配置 | 默认 | 范围 |
| --- | --- | --- |
| `ideation_backend` | `llm` | `llm` / `mock` |
| `ideation_max_model_calls` | 6 | 1～20 |
| `ideation_max_tool_calls` | 12 | 0～40 |
| `ideation_max_output_retries` | 1 | 0～3 |

输出修正也占用模型调用次数。模型截断、拒绝、调用超限或多次输出无效会报错，HTTP 层不自动重试。父图继续使用原有 SQLite 检查点与阶段缓存，已完成并保存的候选选题会被复用；失败重试仍可能重新调用模型。

测试通过脚本化 Provider 和 `httpx.MockTransport` 验证两种协议、Skill 权限、输出修正、调用上限、CLI 配置和失败恢复，不依赖真实 API Key，也不调用付费服务。
