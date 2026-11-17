# 记忆、完整存档与工作上下文

存档与模型输入分别管理。完整用户消息、公开对话、委派历史、人工选题和版本化产物继续保留在父图检查点及业务库中，压缩不会裁剪这些原始记录。`memory.py` 只创建发送模型的投影，不覆盖权威历史。

## 父 Agent

默认发送最近 6 次委派、最近 6 条用户消息，以及当前各类产物的最新引用。早期委派按角色汇总最近执行状态与结果摘要，早期用户消息保留最近 6 条的短摘录，并明确标注覆盖数量和省略数量。投影包含 `task_summary`、`context_window`、`long_term_preferences` 和 `control_state`。摘要是确定性的结构化进展与摘录，不是额外调用模型生成的语义摘要，不保证保留所有早期文字细节。当前摘要快照保存在 `stage_results.context_summary`。

初始用户需求、画像、选定方向与当前有效产物仍随请求提供。人工选题使用 `IdeaSelection`；版本、批准状态以及尚未产生新版本的修改要求通过 `control_state` 显式传递。所有引用、重复委派、修改后新版本、人工审核与发布校验都继续读取完整历史和真实业务状态，不能从摘要里的“已经批准”等文字获得权限。展示给用户的完整公开对话不会被工作窗口裁剪。

## 专业 Agent

同一次执行中的完整消息一直留在本次运行状态。每次调用模型前，保留系统指令、初始任务及最近 4 轮助手/工具消息；更早轮次转成工具结果短摘录。工具调用与该批全部结果、图片消息成组保留或移出，避免出现没有对应调用的 tool result。保留轮数不够满足预算时继续缩小，至少保留最近一轮。最近一批工具结果仍超限时，仅在模型输入中对成功的工具正文作显式摘录，保留全部调用及回包、错误结果和必要任务输入；不改变原始消息及存档。摘录标记 `context_excerpt`，不能当作已经读过全文。

调研的 `web_fetch` 默认每页 8000 字符，`max_chars` 可设为 256～12000；按 `next_offset` 续读。相同来源在本次委派内缓存，翻页不会重复请求检索服务。来源 URL、页起点与正文长度继续提供；预算摘录后的 `next_offset` 指向实际保留正文的末尾。证据摘录必须匹配本次工具实际读取的连续正文范围，不能引用尚未读取的页面。

如果系统指令、初始任务、工具参数等必要部分在正文摘录后仍超预算，则明确失败，不截断用户目标或改变人工选题、版本、批准状态。

每次 Runtime 调用拥有独立 invocation ID，完整任务、工具及公开回答追加进入该次内存状态，并更新 `context_archives` 的完整执行存档。状态为 running、completed 或 failed；重试生成新 invocation ID，不覆盖之前的执行。模型隐藏推理与供应商 thinking 签名不持久化，已知密钥会脱敏。图片消息可留在本地存档，存档不是恢复工具会话的检查点。

Runtime 使用普通模型/工具循环，不创建内部图，完整模型消息只进入执行存档。父图仍保存结构化业务历史与人工中断；历史版本已经写入的内部检查点不删除，类型白名单继续支持读取旧编码。

预算按消息正文与工具调用参数的**字符数**统计，默认 96000；不等同于精确 Token 数，也不包括工具 schema 和图片的供应商计费。`context_compacted` 日志记录压缩前后的消息数量与字符数，不记录原始对话正文。

在执行配置的 `execution` 对象中可添加：

```json
"context": {
  "recent_delegations": 6,
  "recent_user_messages": 6,
  "recent_tool_rounds": 4,
  "max_input_chars": 96000
}
```

配置启动时加载。父图窗口及专业 Agent 输入共用此策略；直接创建独立 Runtime 时使用默认值。

## 长期用户偏好

偏好单独保存在 `preference_memories`，修改历史保存在 `preference_history`，不从普通任务对话或网页自动学习。用户明确提交后才更新；去重，最多 50 条，每条最多 1000 字。本次明确需求优先，偏好不能改变工具权限、人工选题或批准状态。

Web 任务在创建时绑定 `profile:画像ID` 范围，绑定和任务输入原子落库，之后不会因为切换画像而串用偏好。Python/CLI 默认范围为 `default`，可显式指定其他范围。画像快照与独立偏好记录分开存储。清除操作清空当前生效偏好，修改审计记录保留。

已有任务迁移到 `default` 范围，不根据相似画像猜测归属。需要使用画像偏好的旧 Web 任务可新建任务。一次执行开始时读取偏好快照；执行中修改偏好，在下一次执行时生效。

```powershell
# Web 默认画像对应的偏好；--set 可重复，替换整个当前列表
uv run xhs --memory-scope profile:default preferences --set "用自然简短的句子" --set "不使用夸张标题"
uv run xhs --memory-scope profile:default preferences
uv run xhs --memory-scope profile:default preferences --clear

# CLI 创作绑定同一范围；也可用任意独立范围隔离不同账号
uv run xhs --memory-scope profile:default create "生活记录"

# 查看任务摘要和各次模型调用的完整执行记录
uv run xhs context-archive TASK_ID
```

这些偏好及存档命令不需要启动模型。后端接口：

- `GET /api/profiles/{profile_id}/preferences`：查询当前偏好。
- `PUT /api/profiles/{profile_id}/preferences`：接收 `{"preferences":["偏好文本"]}`，替换偏好。
- `DELETE /api/profiles/{profile_id}/preferences`：清空生效偏好。
- `GET /api/tasks/{task_id}/context-archive`：查看该任务的摘要与模型执行记录。

当前网页没有独立偏好编辑面板，可使用这些 API 或 CLI 管理。`profiles.py` 的静态画像加载能力保留；这不是跨账号的向量检索或自动学习记忆。
