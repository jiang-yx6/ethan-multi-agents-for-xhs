# 小红书多 Agent 内容生产系统

根据**用户本次需求、账号画像和可选热点信息**生成内容，支持攻略、观点、生活记录、测评、科普等类型。用户目标优先，热点仅作为相关选题的参考。

基于 Python + uv + LangGraph + Pydantic。**选题默认使用真实模型**：通过统一 LLM Provider 调用配置的厂商，按需加载授权技能，并校验结构化选题结果。资料整理和创作仍采用 Mock，审核采用基本规则，默认发布只写本地模拟记录；小红书真实图文发布可单独启用。实时热点抓取和实际图片生成尚未接入。离线演示可显式选择 `--mock-ideation`。

Mock 修改会保留上一版正文、记录并展示待落实的修改意见，不代表已完成语义改写。默认审核只检查基本结构、证据来源对应关系与占位情况，风格、事实真实性、画像禁区和是否编造经历仍需人工检查或接入语义审核模型。

## Agent 与流程

四个 Agent 分别负责选题、资料整理、创作和质量审核；热点查询、画像加载和发布是独立工具/适配器。每个 Agent 独立编译为子图，父图负责人工决策和路由。

```text
需求 + 画像快照 + 可选热点
           ↓
选题 Agent → 人工选择
           ↓
资料整理 Agent（needs_research=false 时只整理用户素材）
           ↓
创作 Agent（修改时接收上一版草稿和反馈）
           ↓
质量审核 Agent
  ├─ 有错误且未达返工上限 → 资料整理 / 创作
  └─ 通过或达到上限 → 人工审核（展示未解决问题）
                         ├─ 修改 → 创作
                         ├─ 取消
                         └─ 批准 → 导出 / 发布确认
```

自动返工默认最多两轮，可通过 `WorkflowConfig(max_auto_revisions=...)` 设置。警告不触发自动返工，仍向人工展示。自动审核不会批准草稿或发布；达到上限后的人工审核可以决定继续修改、取消或批准。

## 主 Agent 协作协议

已建立统一委派任务、执行结果、主 Agent 三类决策，以及限制角色、技能和工具范围的 Agent 注册表。通过 `TaskService.agent_registry` 查看当前组装的角色。当前仍运行上面的既有流程；主 Agent 模型和动态团队图将在后续接入。模型字段、协议示例、权限边界与迁移范围见 [Agent 协作协议](docs/agent-collaboration.md)。

## 外部技能库

项目内的 `skills/` 统一维护六个项目技能，按 `builtin/` 与 `shared/` 组织，来源版本和许可证保留在登记信息中。`skills/registry.json` 用显式白名单划分选题、研究、创作、审核和发布角色的访问范围。

```bash
uv run xhs skills list --agent ideation
uv run xhs skills show ideation skill-trending-topics
uv run xhs skills list --agent review
```

Agent 通过 `agent.skills.catalog()/load()/read_resource()` 按需读取，使用 `agent.skill_prompt()` 构建技能目录上下文。默认 Agent 已由 TaskService 绑定对应角色。选题模型通过 LangGraph 的模型/工具循环调用 `load_skill`、`read_skill_resource`；其余 Mock 阶段仍使用既有逻辑。技能声明不能授予额外权限，库不会自动执行第三方脚本。完整配置、导入及模型接入方式见 [技能库说明](skills/README.md)。

## 安装与使用

```bash
uv sync
uv run pytest
uv run xhs --help

# 不配置密钥的离线演示
uv run xhs --mock-ideation create "周末出游"
```

默认真实选题需要先配置模型。在 [模型配置示例](examples/llm-config.json) 中填写 `ideation.model` 的实际模型 ID，并在 PowerShell 中设置对应密钥：

```powershell
$env:DEEPSEEK_API_KEY = "填写你的密钥"
uv run xhs --llm-config examples/llm-config.json create "周末出游"
```

也可以用 `XHS_LLM_PROVIDER`、`XHS_LLM_MODEL` 和厂商密钥环境变量配置默认选题模型，然后直接使用：

```bash
uv run xhs create "周末出游"

# 结合账号画像和具体需求；这里只整理用户提供的素材
uv run xhs create "城市漫步" --goal "整理一篇周末路线攻略" --profile examples/profile.json --content-type 攻略 --material "周六走过江边步道" --no-research --no-trends

# 从文件提供热点。示例文件只演示格式，不是实时热点
uv run xhs create "城市漫步" --profile examples/profile.json --trends examples/trends.json
```

选题模型的配置方式、Skill 调用、输出校验和 Python 注入示例见 [真实模型选题说明](docs/model-ideation.md)。缺少模型配置时会明确失败，配置好后可通过 `retry TASK_ID` 继续；不会切换为 Mock 结果。

将下面的 `TASK_ID` 替换为创建任务时返回的实际 ID：

```bash
uv run xhs select TASK_ID 0
uv run xhs status TASK_ID
uv run xhs modify TASK_ID -c "保留原有结构，补充交通方式"
uv run xhs approve TASK_ID

# 导出已人工审核版本；不产生发布记录
uv run xhs export TASK_ID

# 仅模拟发布到本地数据库
uv run xhs publish TASK_ID
```

- `--material`、`--constraint` 可以重复传入，当前接收文本；不会自动读取素材文件。
- `--no-trends` 不调用热点提供器，也不把传入的热点用于选题。
- 没有热点时直接围绕需求和画像选题，不生成假热点。
- 选题序号从 0 开始；越界会报错，任务继续等待选择。
- `cancel TASK_ID` 用于取消待审核或已批准任务。
- 执行失败后用 `retry TASK_ID` 继续失败节点。真实发布回执不明时应先核对平台结果；平台适配器须实现自身幂等。
- 默认数据目录是 `./data`；可用全局参数 `--data-dir`（放在子命令前）或 `XHS_DATA_DIR` 覆盖。

## 可选的小红书真实发布

已新增 `platforms/` 连接层，分开维护账号会话、平台内容和发布作业。先安装可选浏览器依赖，再登记和登录账号：

```powershell
uv sync --extra browser
uv run playwright install chromium
uv run xhs accounts add main --label "主账号"
uv run xhs accounts login main
uv run xhs accounts whoami main
```

编辑 [图文 JSON 示例](examples/xhs-note.json)，填写真实文案和本地图片路径。将 `TASK_ID` 换成已选择选题的任务 ID：

```powershell
uv run xhs replace-draft TASK_ID --file examples/xhs-note.json
uv run xhs preflight TASK_ID --platform xiaohongshu --account main
uv run xhs approve TASK_ID
# 此命令真实提交，需确认文案、图片及目标账号
uv run xhs publish TASK_ID --platform xiaohongshu --account main
uv run xhs publish-jobs TASK_ID
```

首版支持图文、独立登录会话、身份核对、预检和一次提交。`submitted/unknown` 不等于已核实发布，不自动重发；到后台检查后通过 `confirm-published TASK_ID --note-url 实际笔记链接` 登记人工回执。平台草稿箱、视频、定时发布、数据分析与自动回查尚未实现。已绑定真实目标的任务后续沿用原账号。完整流程、状态说明及扩展方式见 [平台接入说明](docs/platforms.md)。目前已通过模拟浏览器与离线测试，真实网站流程仍需实测。

## 数据与归档

- `data/store.db`：创作需求、画像快照、热点、简报、草稿、审核报告与模拟发布记录。
- `data/checkpoints.db`：LangGraph 检查点，以任务 ID 为 `thread_id`，支持跨进程继续选题和审核。
- `data/outputs/<task_id>/v001/`：每版草稿与审核结果自动归档。

```text
data/outputs/<task_id>/v001/
  note.md
  meta.json
  request.json
  profile.json
  review.json
  references/
    brief.json
    trends.json
```

默认图片仍是方案或占位引用，可通过 `replace-draft` 换成本地图片。图片引用保存在 `meta.json`，导出不复制图片文件。导出前需人工批准当前版本，导出不代表上传或发布。

新版本写入会使旧批准失效；同版本内容冲突会回滚。业务草稿写入后、检查点落盘前中断，重试复用已保存的该版本。SQLite 旧表会增量补列，旧草稿字段和批准状态保留。Agent 接口已扩展，旧的自定义 Agent 需要同步更新签名。

## 扩展真实能力

统一模型接口位于 `xhs_agent.llm`，通过 `create_llm_provider()` 创建 OpenAI、Anthropic、DeepSeek、通义、智谱、MiniMax、Ollama 或自定义兼容服务。支持文本对话、工具调用消息、环境变量配置和可注册的厂商预设。使用示例与接口范围见 [LLM Provider 说明](docs/llm-providers.md)，按角色配置示例见 [llm-config.json](examples/llm-config.json)。

`agents/base.py` 定义四个 Agent 和发布器的协议，通过构造参数注入：

```python
from xhs_agent.config import WorkflowConfig
from xhs_agent.models import ContentRequest, UserProfile
from xhs_agent.service import TaskService

service = TaskService(
    data_dir="data",
    # ideation_agent=..., research_agent=..., creation_agent=...,
    # review_agent=..., trend_provider=..., publisher=...,
    config=WorkflowConfig(max_auto_revisions=2),
)
try:
    task = service.create_task(
        ContentRequest(goal="分享周末路线", topic="城市漫步", content_type="攻略"),
        profile=UserProfile(name="城市周末", audience="上班族", style="轻松口语"),
    )
finally:
    service.close()
```

接口约定：

- `IdeationAgent.ideate(request, profile, trends)` → 候选选题。
- `ResearchAgent.research(ResearchRequest)` → 简报，包含输入上下文、来源、原文证据和待确认项。
- `CreationAgent.create(brief, feedback, *, request, profile, previous_draft)` → 草稿。
- `ReviewAgent.review(request, profile, brief, draft)` → 审核报告，问题可以路由到资料或创作阶段。
- `TrendProvider.collect(request, profile)` → 热点列表；默认 `EmptyTrendProvider` 返回空列表。
- `SearchProvider.search/fetch` → 搜索命中与文档，可注入 `MockResearchAgent(provider=...)` 验证资料传递。
- `Publisher.publish(task_id, draft)` → 发布回执；失败不会标记已发布。

资料来源保留 URL、时间和正文片段。默认 Mock 检索显式标记 `Evidence.is_mock=true`，不会作为真实证据写入正文。

画像中的 `experiences` 仅保存用户明确提供的经历，表达偏好放在 `style/preferences`；创作实现应区分二者，缺少体验或事实时提出待确认项。

## Web 创作工作台

第一版已实现 React + TypeScript 前端和 FastAPI 后端，连接真实任务、后台作业、画像、模型设置、媒体、账号和发布回执。启动时不会导入演示数据。安装和运行：

```powershell
uv sync --extra web --extra browser
cd web
npm ci
npm run build
cd ..
$env:XHS_BROWSER_CHANNEL = "chrome"
uv run --extra web --extra browser xhs web
```

访问 `http://127.0.0.1:8000`。在“技能与模型”填写实际模型 ID 和 API Key 即可配置真实选题，密钥在本机加密保存，后续执行无需重启服务。也保留环境变量方式。本地离线验收可用 `xhs --data-dir data/web-e2e --mock-ideation web --port 8011`。已保存的 Web 模型设置会优先用于后续任务。完整功能、数据目录和开发方式见 [Web 使用说明](web/README.md)。

## 代码结构

交互设计见 [前端设计方案](docs/web-frontend.md)，[独立原型](web/prototype/index.html) 保留用于设计参考；正式界面位于 `web/src/`，使用真实 Python API。

```text
src/xhs_agent/
  models/             # 按 Agent 职责组织，__init__.py 统一导出
    base.py           # 共用画像、创作需求、热点、任务视图与时间工具
    ideation.py       # 选题 Agent：候选选题
    research.py       # 研究 Agent：资料请求、来源、证据与简报
    creation.py       # 创作 Agent：草稿与图片引用
    review.py         # 审核 Agent：问题与审核报告
    publishing.py     # 发布适配器：发布结果
  graph.py            # Agent 子图、人工中断、有限返工
  service.py          # 组装、任务操作、失败重试与导出
  storage.py          # 数据迁移、快照、版本与发布记录
  artifacts.py        # 按任务/版本归档
  profiles.py         # JSON 画像加载
  providers.py        # 热点工具契约
  config.py           # 流程设置
  llm/                # 统一模型接口、OpenAI/Anthropic 适配与厂商工厂
  platforms/          # 平台连接器、账号会话、预检与持久化发布作业；首版小红书
  skills/             # 技能登记、角色访问器、按需加载和本地导入
  cli.py              # 命令行入口
  agents/
    base.py           # Agent 与发布适配器协议
    ideation.py       # 真实模型选题与显式 Mock 选题
    runtime.py        # LangGraph 模型/Skill 工具循环、输出修正与调用上限
    research/         # Mock 资料整理与搜索协议
    creation.py       # Mock 创作
    review.py         # 本地规则审核
    publishing.py     # Mock 发布
examples/             # 画像与热点文件示例
skills/               # 注册表、外部技能原文、共享资料和许可证
tests/                # 流程、恢复、迁移、返工与发布测试
```
