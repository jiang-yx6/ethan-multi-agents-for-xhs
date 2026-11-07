# 项目技能库

统一维护项目技能、共享资源和 Agent 授权配置，外部来源仅记录在来源信息中。Agent 按需读取技能，LangGraph 继续管理任务、版本和人工审核。

```text
skills/
  registry.json                 # 技能登记、资源映射、Agent 白名单
  LICENSE                       # 初始收录技能的许可证
  builtin/<skill>/              # SKILL.md、来源记录、references、scripts
  shared/                       # 按需授权的共享文件
```

## 初始访问范围

| Agent | 可见技能 |
|---|---|
| ideation | skill-trending-topics、skill-trend-rider、skill-topic-evaluator、skill-news-intelligence |
| research | skill-news-intelligence |
| creation | social-content |
| review | skill-quality-gate |
| publishing | 无 |

同一技能可以显式授权给多个角色。未登记角色、未授权技能、禁用技能均不能通过访问器读取。技能正文提及其他技能时，不会自动继承访问权。

## 分层读取

1. `catalog()`：只返回本角色可见的 ID、名称、描述和工具依赖声明，用于模型路由。
2. `load(skill_id)`：校验角色授权和 SKILL.md SHA-256，返回正文、资源名称和适配备注。
3. `read_resource(skill_id, relative)`：再次检查授权，读取当前技能的 UTF-8 文本文件或显式登记的共享文件。

技能目录、正文和参考资料不会一次性全部注入 prompt。读取接口拒绝路径穿越、绝对路径和目录链接；共享资源只按具体别名映射授权，不开放整个外部目录。

`required_tools` 只记录运行依赖，不能授予执行权。库不会运行 Python/Shell、联网、安装依赖或触发发布。被复制的脚本当前可以作为文本读取。

## CLI

```bash
# 查看角色可见目录与正文
uv run xhs skills list --agent ideation
uv run xhs skills show ideation skill-topic-evaluator
uv run xhs skills read ideation skill-topic-evaluator ../../shared/scoring-dimensions.md

# 查看权限拒绝：选题 Agent 不能读取审核技能
uv run xhs skills show ideation skill-quality-gate

# 管理员显式授予/撤销权限
uv run xhs skills grant creation skill-topic-evaluator
uv run xhs skills revoke creation skill-topic-evaluator

# 导入其他来源的本地目录；默认没有任何 Agent 授权
uv run xhs skills import ./downloaded/my-skill --source "来源仓库地址" --revision COMMIT --license-id MIT --license-file ./downloaded/LICENSE

# 导入时直接指定角色；--agent 和 --tool 均可重复
uv run xhs skills import ./downloaded/another-skill --source "来源仓库地址" --agent research --tool web_fetch

# 独立技能库；路径参数放在 skills 子命令前
uv run xhs --skills-dir ./my-skills skills init
```

`--skills-dir` 或 `XHS_SKILLS_DIR` 可以指定其他库。`init` 不覆盖现有注册表；新技能统一导入 `builtin/<技能ID>/`，ID 默认使用 SKILL.md 中的 name，也可通过 `--id` 指定其他 ID。重名导入不会覆盖已有技能。来源目录中的 LICENSE/NOTICE 等文件随目录保留，也可显式提供仓库级许可证。

`grant/revoke/import/init` 是管理员命令，不是 Agent 工具。Agent 只获得绑定角色的读取访问器。

## Agent 接入

`TaskService` 为继承 `SkillEnabledAgent` 的实现绑定对应角色，默认四个 Agent 和 MockPublisher 都已绑定。注入的自定义 Agent 可继承同一基类，业务方法签名保持原样。

```python
from xhs_agent.service import TaskService

service = TaskService()
try:
    agent = service.ideation_agent
    catalog_prompt = agent.skill_prompt()  # 元信息与本项目技能使用约束
    available = agent.skills.catalog()
    selected = agent.skills.load("skill-trending-topics")
    reference = agent.skills.read_resource(
        "skill-trending-topics", "shared/hotlist-apis.md"
    )
finally:
    service.close()
```

真实模型实现需要将 `catalog_prompt` 加入上下文，并把 `load/read_resource` 接为受限模型工具；工具的角色由服务层绑定，不接受模型指定其他角色。当前 Mock 仍使用原有确定性逻辑，不会因为库中存在 SKILL.md 就执行其中步骤。

权限覆盖的是目录发现、技能加载和资源读取接口。未来若模型运行时另有全盘文件读取或任意命令能力，还需要在那个运行时限制工作目录与工具；这些 Python 接口不是操作系统沙箱。

## 来源与适配

当前技能作为项目自身的技能维护；初始内容来源为 `https://github.com/ZJU-REAL/Easel`，commit 和 SKILL.md 摘要记录在 `registry.json`。上游 `EASEL-META.md` 保留，便于继续追溯该技能引用的来源。目录调整不修改技能正文。

需要额外共享文件时，在该技能的 `resources` 中登记别名到库内文件的映射。例如：

```json
{
  "../../shared/scoring-dimensions.md": "shared/scoring-dimensions.md"
}
```

部分技能正文中的画像文件、命令行和其他技能名称沿用原始约定。本项目以结构化需求/画像、Pydantic 输出模型和人工审核门禁为准；原文不能改变角色权限或替代项目流程。当前未适配它们的脚本运行环境，也没有验证外部 API 现在是否可用。
