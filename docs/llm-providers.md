# LLM Provider

`xhs_agent.llm` 定义统一对话接口，以两个适配器处理 OpenAI Chat Completions 和 Anthropic Messages 协议。厂商预设只提供默认地址、鉴权环境变量与参数名，模型 ID 始终显式指定。

当前支持同步、非流式的文本对话和客户端工具调用消息。Provider 返回工具调用，不执行工具；技能范围和人工发布门禁仍由 Agent/任务层控制。默认选题已由 `LLMIdeationAgent` 调用 Provider，并通过 LangGraph 执行角色受限的 Skill 读取工具；资料整理、创作和规则审核仍按既有实现运行。

## 最小调用

先将对应厂商密钥放入环境变量。然后：

```python
import os

from xhs_agent.llm import ChatMessage, ChatRequest, create_llm_provider

with create_llm_provider("deepseek", model=os.environ["XHS_LLM_MODEL"]) as llm:
    response = llm.chat(ChatRequest(messages=[
        ChatMessage(role="system", content="根据用户画像提供具体、可落实的选题。"),
        ChatMessage(role="user", content="为面向上班族的城市生活账号设计三个周末选题。"),
    ]))
    print(response.text)
    print(response.usage.total_tokens)
```

工厂创建实例时不发请求；`chat()` 才调用服务。`with` 会关闭 HTTP 客户端，也可以显式调用 `close()`。

## 厂商预设

| provider | 协议 | 默认 base_url | API Key 环境变量 |
|---|---|---|---|
| openai | openai | https://api.openai.com/v1 | OPENAI_API_KEY |
| anthropic / claude | anthropic | https://api.anthropic.com | ANTHROPIC_API_KEY |
| deepseek | openai | https://api.deepseek.com | DEEPSEEK_API_KEY |
| qwen / dashscope / aliyun | openai | https://dashscope.aliyuncs.com/compatible-mode/v1 | DASHSCOPE_API_KEY |
| zhipu / glm | openai | https://open.bigmodel.cn/api/paas/v4 | ZHIPU_API_KEY |
| minimax | anthropic | https://api.minimax.io/anthropic | MINIMAX_API_KEY |
| ollama | openai | http://localhost:11434/v1 | 本地默认可不提供 |

区域、工作空间和套餐可能使用不同地址，可以通过 `base_url` 覆盖。通义默认使用北京地区共享域名，厂商也提供工作空间专属域名；密钥应与所选地区、服务匹配。

OpenAI 协议在 base_url 后追加 `/chat/completions`。Anthropic 协议追加 `/v1/messages`；base_url 已以 `/v1` 结尾时只追加 `/messages`。配置中填写基础地址，不包含最终接口路径。

OpenAI 原生预设使用 `max_completion_tokens`；其他 OpenAI 兼容预设默认使用 `max_tokens`。可以通过 `token_parameter` 覆盖。Anthropic 使用 `max_tokens`。`temperature` 默认不发送，是否支持由具体模型决定。

## 环境变量与配置

```powershell
$env:XHS_LLM_PROVIDER = "deepseek"
$env:XHS_LLM_MODEL = "填写账号可用的模型ID"
$env:DEEPSEEK_API_KEY = "填写API密钥"
```

```python
from xhs_agent.llm import create_llm_provider

llm = create_llm_provider()  # 从 XHS_LLM_* 和厂商密钥变量读取
try:
    ...
finally:
    llm.close()
```

支持 `XHS_LLM_PROVIDER/MODEL/PROTOCOL/BASE_URL/API_KEY/API_KEY_ENV/TIMEOUT/MAX_TOKENS/TEMPERATURE/TOKEN_PARAMETER/AUTH_REQUIRED/AUTH_MODE/ANTHROPIC_VERSION/TRUST_ENV`。

工厂可以接收厂商字符串、`LLMConfig` 或配置字典。显式参数覆盖配置或环境变量；密钥优先级为：显式 `api_key` → 显式 `api_key_env` 指向的变量 → `XHS_LLM_API_KEY` → 厂商预设变量。指定 `api_key_env` 但该变量为空时，不回退到其他密钥。

配置模型使用 `SecretStr`，repr 和 JSON 导出不会输出明文密钥。读取环境变量不会自动加载 `.env` 文件。

按 Agent 分别设置模型时，可以使用不同前缀：

```python
from xhs_agent.llm import LLMConfig, create_llm_provider

ideation_config = LLMConfig.from_env(prefix="XHS_IDEATION_LLM_")
creation_config = LLMConfig.from_env(prefix="XHS_CREATION_LLM_")
ideation_llm = create_llm_provider(ideation_config)
creation_llm = create_llm_provider(creation_config)
```

JSON 配置示例见 `examples/llm-config.json`。先替换模型 ID，密钥通过 `api_key_env` 引用环境变量。CLI 已支持 `xhs --llm-config examples/llm-config.json create "主题"`，本次只将 `ideation` 配置接入真实模型选题，其他角色仍按原有方式运行。Python 调用也可以用 `json.load()` 读取角色配置并传入工厂。完整说明见 [真实模型选题](model-ideation.md)。

## 自定义厂商与两种鉴权方式

不在预设列表中的服务，只要兼容其中一种协议，就可以直接配置：

```python
llm = create_llm_provider(
    "my-gateway",
    protocol="anthropic",  # 或 openai
    base_url="https://gateway.example/anthropic",
    model="你的部署ID",
    api_key_env="MY_GATEWAY_API_KEY",
    auth_mode="bearer",   # Authorization: Bearer ...
)
```

默认鉴权方式为 OpenAI 的 `bearer` 和 Anthropic 的 `x-api-key`。兼容网关可以显式指定 `auth_mode`。本地免鉴权服务可以使用 `auth_required=False`。

也可以注册厂商预设：

```python
from xhs_agent.llm import ProviderPreset, register_provider_preset, create_llm_provider

register_provider_preset(
    "my-company",
    ProviderPreset(
        protocol="openai",
        base_url="https://gateway.example/v1",
        api_key_env="COMPANY_LLM_KEY",
    ),
    aliases=("company",),
)
llm = create_llm_provider("company", model="部署ID")
```

注册只影响当前进程，不保存凭证或修改文件；重新启动后需要重新注册。默认拒绝同名覆盖，显式更新使用 `replace=True`。`list_provider_presets()` 返回当前预设的副本。

同一厂商切换协议时，需要同时指定对应 `base_url`。例如 DeepSeek 的 Anthropic 兼容地址可通过 `provider="deepseek", protocol="anthropic", base_url="https://api.deepseek.com/anthropic"` 配置。

## 工具调用与统一输出

`ChatRequest.tools` 接收 `ToolDefinition`，`tool_choice` 支持 `auto/none/required`。OpenAI 转换为 `tools[].function`，Anthropic 转换为 `tools[].input_schema`。

模型要求调用工具时，`response.finish_reason == "tool_calls"`，调用信息位于 `response.message.tool_calls`。调用方检查权限、执行工具后，将结果作为 `ChatMessage(role="tool", tool_call_id=..., content=...)` 加回历史，继续调用 `chat()`。

有多个工具调用时，必须全部回传结果再继续对话。Provider 会拒绝响应中未提供给模型的工具名称。它不会代替技能访问器做资源权限判断，也不会自行运行脚本。

Anthropic 的 tool_result 会编码为 user 消息内容块；其 thinking 签名内容块会保留用于同协议续传。OpenAI 兼容服务的 `reasoning_content` 也会保留。包含协议专属原始内容的历史不能直接跨协议复用。

`ChatResponse` 统一提供 `text`、`message`、结束原因、模型、厂商、请求 ID 和输入/输出 token 数。工具参数和结果、结束原因等转换由适配器处理。

厂商扩展选项放在 `LLMConfig.extra_body` 或 `ChatRequest.extra_body`，请求级选项优先。可用它传递厂商支持的 `thinking`、`reasoning_effort`、`response_format` 等参数，但不能覆盖 model、messages、工具和 token 参数等公共字段。

## 错误与边界

- 配置或消息无法编码：`LLMConfigurationError`。
- 超时：`LLMTimeoutError`。
- 连接错误：`LLMConnectionError`。
- HTTP 非成功状态：`LLMHTTPError`，含 `status_code`、`retryable`。
- 响应损坏或不支持的内容：`LLMResponseError`。

当前不自动重试、不跟随重定向；调用方决定重试策略。错误不回显上游响应正文和连接细节。默认 `trust_env=False`；确需使用系统代理时设置 `trust_env=True`。

当前未覆盖流式输出、多模态、OpenAI Responses API 和厂商原生专有接口；不同模型支持的可选参数与工具能力仍以厂商文档为准。测试使用模拟 HTTP 验证协议，未调用真实付费模型。

## 协议依据

- [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create)
- [DeepSeek API](https://api-docs.deepseek.com/quick_start/)
- [通义基础地址](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/base-url)
- [智谱 OpenAI 兼容](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction)
- [MiniMax Anthropic 兼容](https://platform.minimax.io/docs/api-reference/text-anthropic-api)
- [Ollama OpenAI 兼容](https://docs.ollama.com/api/openai-compatibility)
