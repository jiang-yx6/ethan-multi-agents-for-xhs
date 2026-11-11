# 平台连接层与小红书首版接入

内容生产流程继续使用独立 Agent；登录、上传和提交放在 `xhs_agent.platforms`。首个连接器采用 Playwright 操作小红书创作者网页，不是小红书官方开放 API。默认发布仍使用 Mock；只有显式指定平台和账号，任务才会绑定真实目标。绑定后跨进程沿用该目标，不会因遗漏参数退回 Mock。

## 支持范围

| 能力 | 当前实现 |
| --- | --- |
| 账号 | 本地登记、多账号独立浏览器目录、窗口扫码/手动登录、实际身份校验 |
| 图文发布 | 本地图片预检、上传、标题/正文输入、话题处理、一次最终提交 |
| 审核边界 | 替换内容产生新版本；批准时记录内容和图片 SHA-256；上传前再次核对 |
| 发布回执 | 持久化提交作业；区分已提交、已核实发布、结果不明、提交前失败 |
| 结果核实 | 当前需用户到平台后台检查后，明确登记笔记链接，记录 `verification=manual` |
| 平台草稿箱、视频、定时发布、数据分析、自动查询发布结果 | 尚未实现，能力声明为 false；不支持的方法明确报错 |

首版连接器主动限制标题 20 字符、正文与话题合计 1000 字符、最多 10 个话题、1～9 张 JPG/PNG/WebP 本地图片、每张最多 20 MB。这是本实现的支持范围，不是对平台最新全量规格的保证。超限会要求修改并重新审核，不自动截断文字。图片文件头检查属于基础预检，不代替平台解码校验。

话题只选择文字完全匹配的联想项；没有匹配项时保留普通 `#话题` 文字，不保证绑定到平台的话题实体。页面选择器、内部身份接口、图片上传组件和发布组件位置可能随改版失效。

浏览器使用明确的 `1440×1000` 桌面视口。“上传图文”标签按完整文字匹配，优先选择当前视口内可交互的候选，再尝试将离屏候选滚动到视口；点击前检查稳定性、禁用状态和遮挡。存在同名离屏副本时不再固定点击第一个元素。页面异步加载期间会在有限时间内重新定位；标签仍不可用时返回明确的提交前失败，不强制触发点击或删除页面弹层。

若遇到“无法点击上传图文”提示，检查发布窗口中的登录验证、弹窗和布局。修复连接器代码后，需要在运行 Web 的终端停止并重新启动服务，再从已批准任务重新准备发布。实际提交结果为 `submitted/unknown` 的任务继续按回执核实流程处理。

目前已完成离线集成和模拟浏览器验证，尚未进行真实账号登录/发帖验收。真实使用时应在浏览器窗口中检查平台提示。验证码、短信验证和其他平台验证由用户完成。

## 使用步骤

安装可选依赖和浏览器：

```powershell
uv sync --extra browser
uv run playwright install chromium
uv run xhs platforms
```

如果电脑已经安装 Chrome 或 Edge，也可以跳过 Chromium 下载，在当前 PowerShell 中指定系统浏览器：

```powershell
$env:XHS_BROWSER_CHANNEL = "chrome"  # 使用 Edge 时填 msedge
```

该环境变量会用于登录、身份校验和发布。未设置时使用 Playwright 自带的 Chromium。

登记账号并在可见窗口中登录。账号 ID 会规范为小写，与画像中的 `UserProfile.name` 无关：

```powershell
uv run xhs accounts add main --platform xiaohongshu --label "主账号"
uv run xhs accounts login main --timeout 180
uv run xhs accounts whoami main
uv run xhs accounts list
```

首次身份校验成功后绑定平台用户 ID。网络异常不会抹去已有绑定；浏览器切换到其他用户会被拒绝，需要使用另一个本地账号 ID。每次提交前，连接器还会再次向创作者身份接口核对目标用户 ID。

按原有流程创建任务和选择选题，再替换为准备好的文案与真实图片：

```powershell
uv run xhs create "周末路线" --no-trends --no-research
# 父 Agent 提问时回复，交付草稿后继续审核
uv run xhs answer TASK_ID --text "采用第一个方向"
uv run xhs replace-draft TASK_ID --file examples/xhs-note.json
uv run xhs status TASK_ID
uv run xhs preflight TASK_ID --platform xiaohongshu --account main
uv run xhs approve TASK_ID
```

先编辑示例 JSON 并放入真实图片。JSON 图片的相对路径相对于 JSON 文件所在目录；Python 调用中的相对路径相对于当前工作目录。支持本地绝对路径和 `file:///` URI，远程 URL、`mock://` 占位不能直接发布。`replace-draft` 会增加版本、清除旧批准、运行审核，并停在人工审核；不能通过 JSON 指定版本或伪造 `approved`。

`preflight` 只检查本地内容与已登记身份，不启动浏览器，不刷新登录态，也不创建提交作业。批准后修改图片文件会阻止发布，需要替换草稿并重新审核。

下面的命令会打开浏览器并向指定账号真实提交已批准内容：

```powershell
uv run xhs publish TASK_ID --platform xiaohongshu --account main
uv run xhs publish-jobs TASK_ID
uv run xhs status TASK_ID
```

页面出现明确的“发布成功”提示时，先记录 `submitted`；尚未证明笔记公开发布，任务的 `published` 仍为 false。点击超时、无明确回执，或进程在提交后中断，进入 `unknown` 或遗留 `submitting`。这些作业不会自动再次提交。

到创作者后台核对内容、账号和笔记链接。确认已经发布后，明确登记人工回执：

```powershell
uv run xhs confirm-published TASK_ID --note-url "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"
```

将示例链接换成实际笔记链接。这个命令不再次发帖，也不声称自动验证链接；它记录用户的人工确认，任务才变为 `published`，并保存笔记 ID、链接和核实方式。若后台仍在审核或无法确定结果，保留原状态，暂不确认。

明确发生在最终点击前的失败会保留批准状态、回到发布确认，可修复登录/页面问题后再次运行 `publish TASK_ID`；这时沿用原绑定账号。提交期间任务为 `submitting`，不能修改或取消草稿。进程异常导致任务 `failed`，或直接退出遗留 `submitting` 时，可运行原有 `retry TASK_ID`，已有提交作业会阻止重复点击。结果不明但后台确实没有笔记的情况，首版没有自动解锁重发入口，需要先完成核实再处理作业。

## 代码分层

```text
platforms/
  models.py        账号引用、身份、能力、平台内容、发布请求/作业/回执
  base.py          PlatformConnector 协议与不支持能力的错误
  factory.py       根据平台名创建连接器
  accounts.py      账号登记、身份绑定、登录与 whoami
  content.py       草稿适配、本地文件解析、审核指纹
  storage.py       独立账号与发布作业 SQLite 库，原子占用作业
  publishing.py    编排预检/提交/人工核实，适配原有 Publisher
  xiaohongshu.py   小红书网页实现与集中维护的选择器
```

`PlatformConnector` 提供 `capabilities/login/whoami/validate/submit/create_draft/reconcile`。新增平台实现该协议并在工厂中登记；Agent 不依赖平台名、Cookie 或网页选择器。Skill 的访问许可不授予浏览器操作或发布权限，SkillLibrary 仍然只读取技能内容。

`TaskService` 负责人工审核、选择目标账号和图的状态；`PlatformPublisher` 在原有发布节点中调用连接层。明确的提交前失败返回发布确认，已提交/未知结果终止本次图执行并等待核实。首版工作流每个任务绑定一个目标账号；发布作业库已分别存储平台和账号，但多平台扇出、按目标分别适配和审核尚未接入。

## 持久化与幂等

```text
data/
  store.db                                  原有任务、内容、批准指纹、发布目标及回执
  checkpoints.db                            原有 LangGraph 检查点
  platforms.db                              平台账号与提交作业
  accounts/xiaohongshu/<account_id>/browser/  Chromium 持久化登录态
```

浏览器目录按平台和账号隔离，使用进程退出后自动释放的 OS 文件锁，避免同时打开相同会话。Cookie 由浏览器维护，不放进图状态、提示词或 Skill 中；账号和作业查看命令不输出 Cookie。浏览器目录含有登录凭据，应保留在本机 `data/` 中。

提交作业由 `(task_id, draft_version, platform, account_id)` 唯一标识，保存不可覆盖的请求快照和图片摘要。SQLite 原子更新保证只有一个调用能把 `ready/failed` 作业变为 `submitting`。从最终点击开始任何异常均记为不明；不能将遗留 `submitting` 当作安全失败重试。

```text
ready → submitting → submitted → published（当前通过人工确认）
                 ├→ unknown   → published（当前通过人工确认）
                 └→ failed    → submitting（明确的点击前失败，可重试）
```

跨库落盘不是一个分布式事务，因此允许通过原提交作业恢复或重复登记相同人工回执。相同版本的真实发布目标持久保存；重启重试时不会改用 Mock。上传时使用经过 SHA-256 核对的内存字节，避免上传过程中读取到审核后被替换的文件。

## 来源说明

小红书网页地址、内部身份接口和选择器参考 [Easel 的 xhs_publish.py](https://github.com/ZJU-REAL/Easel/blob/5c12e42e9019d276368130b52385b770749ffa6f/skills/shared/scripts/xhs_publish.py)。Easel 文件说明其部分选择器与操作流程来源于 xpzouying/xiaohongshu-mcp。许可文本保留在 [skills/LICENSE](../skills/LICENSE)。本项目重新组织为连接器与持久化作业，没有复制整套脚本运行环境。
