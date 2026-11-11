# Agent 流程文件日志

CLI 与 Web 通过 `TaskService` 执行任务时默认启用，无需新增启动参数或配置。日志保存在当前数据目录的 `logs/agents-YYYY-MM-DD.jsonl`，例如 `data/logs/agents-2026-11-11.jsonl`。日期和 `timestamp` 使用 UTC，时间戳明确带时区。

每行是一条 UTF-8 JSON 记录，按天分文件，服务重启、人工回复和重试继续追加。任务跨天时可用任务 ID 搜索多份日志。

```powershell
# 持续查看当天日志
Get-Content data/logs/agents-2026-11-11.jsonl -Encoding UTF8 -Tail 30 -Wait

# 搜索某个任务的全部记录
rg "TASK_ID" data/logs
```

| 字段 | 含义 |
| --- | --- |
| `task_id` | 整个创作任务的标识 |
| `run_id` | 本次启动、恢复或重试的执行标识，同一任务可以有多次运行 |
| `node` | LangGraph 节点，例如 supervisor、execute_agent、ask_user、review、publish |
| `agent_id` | 实际处理工作的角色；人工 review 门禁与专业 review Agent 可通过 node/agent_id 区分 |
| `delegation_id` | 本次专业委派；模型及工具事件继承同一标识 |
| `kind` | 事件类型 |
| `elapsed_ms` | 节点、模型或工具的耗时，单位毫秒 |

主要事件：

- `task_registered`：登记任务。
- `execution_started/finished/failed`：一次运行的开始、结果状态与失败。
- `node_started/completed/paused/failed`：节点生命周期；LangGraph interrupt 记为暂停。
- `supervisor_decision`：父 Agent 的 action、决策轮次、缓存复用与选题索引。
- `delegation_started/result`：执行要求、完成条件、输入/输出产物引用、选题索引、结果摘要或失败原因。
- `model_call/result/error`：调用次数、厂商/模型、停止原因、Token 用量和异常。
- `tool_started`、`skill_tool`、`agent_tool`：工具名称、调用 ID、结果状态、耗时及失败说明。
- `output_validation`：校验失败原因、修正次数及是否继续。
- `graph_paused/completed`、`user_action`：暂停位置与恢复、批准、发布等人工操作。

排查“直接采用候选 0”时，按 `task_id` 依次看 `supervisor_decision` 和 `delegation_started` 的 `idea_index`，并检查之前是否有 `node_paused`（ask_user）以及 `user_action`（answer）。日志记录实际发生的流程，不会额外增加人工确认或改变选题规则。`cached=true` 表示本次复用了已保存的决策或委派结果。

日志记录公开任务要求和执行摘要，不记录模型请求/响应原文、技能正文、工具结果正文、图片 base64 或内部推理。配置及环境中的密钥、Bearer 凭据和常见密钥格式会脱敏；产物仅记录逻辑引用。异常包含安全处理后的消息以及文件名、函数名、行号，不记录局部变量。默认日志位于已被 Git 忽略的 `data/` 下。

日志文件与现有 Web 作业事件、SQLite 检查点并行使用。日志写入故障会提示到终端，不使已提交的业务操作重新执行。实现位于 `flow_log.py`，节点接入位于 `graph.py`，模型/工具接入位于 `agents/runtime.py`，运行生命周期位于 `service.py`。
