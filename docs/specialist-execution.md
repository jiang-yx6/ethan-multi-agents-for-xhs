# 专业 Agent 执行能力（第二阶段）

专业 Agent 通过 `SpecialistDelegate.run(DelegationTask)` 接收任务，保留各自的选题、调研、创作和审核业务模型。一次委派内，模型可以自主读取技能、选择工具、处理工具错误、修正不合法输出，直到交付或达到预算。父 Agent 只收到摘要及版本化产物引用。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| `agents/runtime.py` | `ModelToolRuntime`：通用 LangGraph 模型 → 工具 → 输出校验循环；无选题字段和业务工具分支 |
| `agents/tools.py` | `AgentTool`、严格参数 schema、工具结果及图片输入；技能读取也是普通工具 |
| `execution.py` | 每个角色的提示词补充、技能/工具范围、视觉能力和执行预算；检索/图片服务连接配置 |
| `agents/ideation.py` | 选题模型与 `IdeaBatch` 校验，只使用选题授权技能和已有热点 |
| `agents/structured.py` | 调研、创作、审核的真实模型实现，各自创建独立工具会话与业务校验器 |
| `agents/research/search.py` | Tavily Search/Extract 实现及来源会话，校验 URL、原文摘录和来源元信息 |
| `agents/images.py` | OpenAI Images 兼容的生成/编辑接口、图片文件保存与复用、受限看图工具 |
| `agents/registry.py` | 限制角色可使用的工具，不允许通过技能正文扩大权限 |

## 配置与启动

文本模型继续使用原有 `--llm-config` 和网页中五个角色的模型设置。检索和图片接口独立配置，不能把文本模型能看图等同于能生成图片，也不会隐式使用文本模型密钥。

执行配置示例为 `examples/execution-config.json`。支持同级 `execution` 对象写在原模型配置文件中，也可单独通过 `--execution-config` 指定；单独指定时优先使用该文件，或设置 `XHS_EXECUTION_CONFIG`。配置在启动时读取，不会被网页保存文本模型配置覆盖。

启动入口会自动通过 `python-dotenv` 读取项目根目录 `.env`，不覆盖终端已有变量。可在该文件填写 `TAVILY_API_KEY`、`XHS_IMAGE_API_KEY`，并设置 `XHS_LLM_CONFIG`、`XHS_EXECUTION_CONFIG`，随后使用 `uv run --extra web --extra browser xhs web` 启动。修改后重启生效；`.env` 不提交 Git，模板为 `.env.example`。行为依据 [python-dotenv 官方说明](https://github.com/theskumar/python-dotenv#readme)。

```powershell
uv run xhs --llm-config examples/llm-config.json --execution-config examples/execution-config.json web
```

使用示例前需要：在文本模型配置中提供 research/creation/review 对象，或在 Web 中把对应角色设为 `llm`；为检索和图片接口填写有效 `api_key` 或指定密钥变量；将示例的图片模型占位值改成所用服务实际支持的图片模型 ID。`api_key` 是直接密钥，`api_key_env` 是变量名，显式直接密钥优先。该文件应按本地私密配置管理；示例本身不包含凭据。

角色的 `tools` 和 `skill_ids` 只能缩小已实现能力与技能库授权。省略时使用该角色实际已配置的能力；空数组表示禁用。未配置检索/图片服务时不能显式授权其工具，服务组装会明确报错。离线或规则实现不具备真实网络/图片工具，因此启用完整示例时应同时配置对应真实专业模型。

调研、创作和审核默认仍分别为 Mock、Mock 和规则实现；已有离线运行方式继续可用。切到真实模型需显式配置角色，不自动把离线任务变成付费任务。

## 各角色的实际能力

| 角色 | 模型业务输出 | 可用工具 |
| --- | --- | --- |
| 选题 | `IdeaCandidate` 列表 | `load_skill`、`read_skill_resource` |
| 调研 | `ResearchBrief` | 技能工具，以及配置后的 `web_search`、`web_fetch` |
| 创作 | `Draft` | 技能工具、配置后的 `generate_image`，以及支持视觉时的 `inspect_image` |
| 审核 | `ReviewReport` | 技能工具、`run_rules`，以及支持视觉时的 `inspect_image` |

`GET /api/agents` 返回实际注册角色、技能及工具目录，不返回密钥。父 Agent 的决策上下文也使用这份目录。服务已配置不等于凭据与服务端模型能力已在线验证，HTTP 错误仍会明确反馈。

调研先搜索，再选择命中的 URL 提取正文；不能直接要求工具读取任意本地或未命中的地址。工具失败作为结构化消息回到子模型，子模型可以更换来源。最终来源只能来自本次成功读取的正文；标题与读取时间以工具记录为准，证据摘录必须确实存在于原文。没有正文证据时保留待核实问题，不能伪造已完成核实。`needs_research=false` 时不向模型提供检索工具。

创作模型收到用户需求、画像、简报、反馈与上一版草稿；有明确修改反馈时不能原样返回旧稿。图片输出只允许引用输入或本次生成工具返回的文件。`generate_image` 不带参考图时生成新图，带目录中的 `reference_image_id` 时上传对应图片执行编辑；改图不能仅改图片描述。每次委派最多生成四张，文件落在数据目录的 `generated-images/`；相同接口、模型、尺寸、提示词与参考图片的已保存结果会复用。请求中断发生在远端生成后、本地保存前时，无法保证不会再次计费。

视觉输入由角色配置 `supports_vision` 明确声明，并同时授权 `inspect_image`。项目不会根据厂商名称猜测任意网关的具体模型是否支持视觉。OpenAI Chat Completions 和 Anthropic Messages 均支持图片消息编码；只有声明视觉能力且本次调用看图工具时才发送图片。文本模型没有视觉能力时，仍可委派独立的图片生成模型，图片输出与看图能力分别判断。

CLI 可提供素材图片并要求生成配图：

```powershell
uv run xhs --llm-config examples/llm-config.json --execution-config examples/execution-config.json create "周末步道图文" --image "D:/photos/walk.png" --generate-images
```

Web API 的 `ContentRequest` 同样支持 `input_images`（已有本地素材路径）和 `generate_images`；当前页面生成后的图片继续通过草稿与预览展示，输入素材图片可使用 CLI/API 提供。

审核模型可主动调用规则检查并在有视觉能力时看图；最终报告始终合并确定性规则，模型不能删掉规则问题。审核不会批准或发布，版本、人工批准与平台提交仍由服务层控制。

## 上下文与验证

每次业务调用创建新的 Runtime 消息和工具会话；没有共享专业 Agent 的 messages。来源目录、已生成图片和工具权限都按本次调用创建，不能凭其他调用的图片 ID 或 URL 读取其工具结果。完整对话保存在子图检查点，父图历史只保留 `DelegationRecord` 的摘要和引用；Web 事件只记录工具名、角色和成功/失败，不包含原始模型响应或凭据。

`tests/test_specialist_execution.py` 使用真实协议适配器与 HTTP MockTransport 验证检索、正文提取、失败换源、证据修正、图片上传编辑、视觉编码、生成文件复用、真实改稿、规则合并及上下文隔离，不访问外网或调用付费模型。

接口依据：[Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)、[Tavily Extract](https://docs.tavily.com/documentation/api-reference/endpoint/extract)、[OpenAI 图片生成](https://developers.openai.com/api/reference/resources/images/methods/generate)、[OpenAI 图片编辑](https://developers.openai.com/api/reference/resources/images/methods/edit)、[OpenAI 视觉输入](https://developers.openai.com/api/docs/guides/images-vision)、[Anthropic 视觉输入](https://platform.claude.com/docs/en/build-with-claude/vision)。
