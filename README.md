# 小红书多 Agent 内容生产系统（第一版）

基于 **Python + uv + LangGraph + Pydantic** 的小红书内容生产流水线。本版为
**Mock 实现**：研究/创作 Agent 为确定性本地实现，发布模块只写本地记录，
**不连接小红书、不真实发布**。

## 功能

1. **Mock 研究 Agent**：根据用户选题生成结构化选题简报（受众、痛点、角度、关键词、标签）。
2. **Mock 创作 Agent**：根据简报生成标题、正文与图片引用；支持携带修改意见重新生成。
3. **LangGraph 编排**：`研究 → 创作 → 人工审核`，审核通过后进入发布。
4. **人工审核**：支持 `批准 / 提出修改意见 / 取消`；修改后再次审核。
5. **SQLite 检查点**：基于 `thread_id`，进程重启后可从上次中断处恢复。
6. **草稿版本管理**：每次修改生成新版本，**修改后旧批准自动失效**。
7. **Mock 发布模块**：只写本地发布记录，通过唯一约束**防止重复模拟发布**。
8. **CLI**：`create / status / approve / modify / publish / cancel`。
9. **可替换接口**：研究、创作、发布均以 `Protocol` 定义，可注入真实 LLM / 平台工具。

## 架构

```
用户选题
   │
   ▼
[research]  Mock 研究 Agent ── 结构化选题简报
   │
   ▼
[create]    Mock 创作 Agent ── 标题 / 正文 / 图片引用（版本 N）
   │
   ▼
[review]    人工审核（LangGraph interrupt 挂起）
   ├─ 批准        → [mark_approved] → [publish_gate]
   ├─ 提出修改意见 → 回到 [create]（版本 N+1，旧批准失效）
   └─ 取消        → [cancelled]
                         │
                         ▼
                [publish_gate] 二次人工确认
                   ├─ 发布            → [publish]（Mock）
                   ├─ 提出修改意见     → 回到 [create]
                   └─ 取消            → [cancelled]
```

### 状态机

| 状态           | 含义                       | 可用操作                     |
| -------------- | -------------------------- | ---------------------------- |
| `pending_review` | 已生成草稿，等待审核       | approve / modify / cancel    |
| `approved`     | 已批准，等待发布           | publish / modify / cancel    |
| `published`    | 已发布（终态）             | —                            |
| `cancelled`    | 已取消（终态）             | —                            |

## 目录结构

```
src/xhs_agent/
  models.py           # Pydantic 领域模型
  storage.py          # SQLite 业务存储（任务/草稿版本/发布记录）
  graph.py            # LangGraph 编排
  service.py          # 高层任务服务（组装与操作）
  cli.py              # 命令行入口
  agents/
    base.py           # 可替换接口（Protocol）
    research.py       # Mock 研究 Agent
    creation.py       # Mock 创作 Agent
    publishing.py     # Mock 发布器
tests/                # pytest 测试
```

## 安装与运行

```bash
# 1. 安装依赖（uv 会自动创建虚拟环境）
uv sync

# 2. 运行测试
uv run pytest

# 3. 运行 CLI
uv run xhs --help
```

## 使用示例

```bash
# 创建任务（生成首版草稿，进入待审核）
uv run xhs create "早C晚A护肤入门"

# 查看状态（含草稿与版本历史）
uv run xhs status xhs-xxxxxxxx

# 提出修改意见（生成新版本，旧批准失效）
uv run xhs modify xhs-xxxxxxxx -c "希望补充早晚护肤顺序"

# 批准（进入待发布）
uv run xhs approve xhs-xxxxxxxx

# 发布（Mock，只写本地记录）
uv run xhs publish xhs-xxxxxxxx

# 取消
uv run xhs cancel xhs-xxxxxxxx
```

> 默认数据目录为 `./data`，可用 `--data-dir` 或环境变量 `XHS_DATA_DIR` 覆盖。

## 关键设计说明

- **SQLite 检查点与恢复**：LangGraph 检查点写入 `data/checkpoints.db`，以
  `thread_id`（即任务 ID）标识。每个 CLI 进程独立打开检查点并
  `Command(resume=...)` 续跑，因此进程重启后可无缝恢复审核。
- **旧批准失效**：`Store.save_draft` 在写入新版本时，会将该任务所有旧版本的
  `approved` 置 0，确保旧批准不会“沿用”到新草稿。
- **防止重复发布**：`publish_records.task_id` 有 `UNIQUE` 约束，`MockPublisher`
  与 `Store.record_publish` 双重校验；重复发布返回 `duplicate` 且不写第二条记录。
- **可替换接口**：真实模型只需实现 `agents/base.py` 中的
  `ResearchAgent` / `CreationAgent` / `Publisher` 协议，并在 `TaskService`
  构造时注入即可，编排与存储逻辑无需改动。
