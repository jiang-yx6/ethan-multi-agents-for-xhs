"""命令行入口。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

from .config import AgentConfig, load_role_llm
from .execution import ExecutionConfig, load_execution_config
from .models import ContentRequest, DraftContent, TaskView, TrendItem, UserProfile
from .platforms import AccountRef, AccountService, PlatformStore, create_platform_connector
from .profiles import load_profile
from .service import TaskService
from .skills import AGENT_ROLES, DEFAULT_SKILLS_DIR, SkillLibrary, SkillSource


def _data_dir() -> str:
    return os.environ.get("XHS_DATA_DIR", "data")


def _ensure_utf8() -> None:
    """在 Windows 上强制 UTF-8 输出，避免中文在终端中乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _print_task(t: TaskView) -> None:
    print(f"任务 ID   ：{t.task_id}")
    print(f"选题      ：{t.topic}")
    print(f"状态      ：{t.status}")
    print(f"当前版本  ：{t.current_version}")
    print(f"是否发布  ：{'是' if t.published else '否'}")
    print(f"账号画像  ：{t.profile.name}")
    if t.request:
        print(f"创作目标  ：{t.request.goal}")
    if t.last_error:
        print(f"执行错误  ：{t.last_error}")
    if t.publish_result:
        result = t.publish_result
        print(f"发布回执  ：{result.status or ('published' if result.published else 'failed')} / {result.reason}")
        if result.platform:
            print(f"目标账号  ：{result.platform}/{result.account_id}")
            print(f"发布作业  ：{result.job_id}")
        if result.url:
            print(f"笔记链接  ：{result.url}（核实方式：{result.verification}）")
    if t.draft:
        print("-" * 40)
        print(f"标题：{t.draft.title}")
        print(f"正文：\n{t.draft.body}")
        print("图片引用：")
        for img in t.draft.images:
            print(f"  - {img.description}  ({img.source})")
        print(f"标签：{' '.join(t.draft.tags)}")
    if t.review:
        print(f"自动审核  ：{'通过规则检查' if t.review.passed else '仍有问题，需人工处理'}")
        for issue in t.review.issues:
            print(f"  [{issue.severity}] {issue.message}")
    if t.history:
        print("-" * 40)
        print("版本历史：")
        for h in t.history:
            mark = "已批准" if h["approved"] else "未批准"
            print(f"  v{h['version']}  {h['title']}  [{mark}]")


def _print_pause(service, task_id):
    workflow = service.workflow(task_id)
    if workflow.status == "unsupported":
        print(workflow.error)
    for pause in workflow.interrupts:
        if pause.kind == "ask_user":
            print("主 Agent 提问：" + pause.value.get("question", ""))
            print(f'回复：uv run xhs answer {task_id} --text "补充的信息"')
        elif pause.kind == "review":
            print(f"下一步：人工审核后执行 uv run xhs approve {task_id}，或使用 modify 提出修改意见")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xhs", description="父子 Agent 内容生产系统（父 Agent 动态委派，各角色独立配置模型）")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 $XHS_DATA_DIR 或 ./data）")
    parser.add_argument("--skills-dir", default=os.environ.get("XHS_SKILLS_DIR", str(DEFAULT_SKILLS_DIR)),
                        help="技能库目录（默认项目 skills，可用 XHS_SKILLS_DIR 覆盖）")
    parser.add_argument("--llm-config", default=os.environ.get("XHS_LLM_CONFIG"),
                        help="按角色配置的 JSON 文件；supervisor 与 ideation 分别配置")
    parser.add_argument("--execution-config", default=os.environ.get("XHS_EXECUTION_CONFIG"),
                        help="专业 Agent 的提示词、技能/工具授权、检索与图片接口配置文件")
    parser.add_argument("--mock-ideation", action="store_true", help="仅使用离线选题，不影响父 Agent")
    parser.add_argument("--mock-supervisor", action="store_true", help="仅使用离线父 Agent，不影响选题 Agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("web", help="启动本地 Web 创作工作台")
    p.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    p = sub.add_parser("create", help="创建任务：由父 Agent 根据需求、画像与可选热点协调子 Agent")
    p.add_argument("domain", help="主题或创作需求，如「周末出游」")
    p.add_argument("--goal", help="本次创作目标，默认使用主题")
    p.add_argument("--profile", help="画像 JSON 文件路径")
    p.add_argument("--trends", help="热点 JSON 数组文件路径")
    p.add_argument("--content-type", default="分享", help="内容类型，如攻略、观点、测评")
    p.add_argument("--material", action="append", default=[], help="用户素材文本，可重复传入")
    p.add_argument("--constraint", action="append", default=[], help="本次约束，可重复传入")
    p.add_argument("--image", action="append", default=[], help="授权本次创作使用的本地素材图片，可重复")
    p.add_argument("--generate-images", action="store_true", help="要求创作配图，需要配置 execution.images")
    p.add_argument("--no-trends", action="store_true", help="不结合热点")
    p.add_argument("--no-research", action="store_true", help="仅整理用户素材，不调用外部检索")

    p = sub.add_parser("status", help="查看任务状态")
    p.add_argument("task_id", help="任务 ID")

    p = sub.add_parser("answer", help="回复主 Agent 的问题并继续协作")
    p.add_argument("task_id")
    p.add_argument("--text", required=True)

    p = sub.add_parser("collaboration", help="查看主 Agent 决策、委派记录与产物")
    p.add_argument("task_id")

    p = sub.add_parser("approve", help="批准当前草稿")
    p.add_argument("task_id", help="任务 ID")

    p = sub.add_parser("modify", help="提出修改意见（生成新版本并重新审核）")
    p.add_argument("task_id", help="任务 ID")
    p.add_argument("--comment", "-c", required=True, help="修改意见")

    p = sub.add_parser("publish", help="发布已批准草稿；默认 Mock，指定平台账号后真实提交，已绑定任务沿用原目标")
    p.add_argument("task_id", help="任务 ID")
    p.add_argument("--platform", help="真实平台，当前支持 xiaohongshu")
    p.add_argument("--account", dest="account_id", help="已登记并登录的本地账号 ID")

    p = sub.add_parser("replace-draft", help="从 JSON 替换文案和本地图片，产生新版本并重新审核")
    p.add_argument("task_id")
    p.add_argument("--file", required=True, help="包含 title/body/images/tags 的 JSON 文件")

    p = sub.add_parser("preflight", help="离线检查真实发布内容和账号登记，不启动浏览器、不发布")
    p.add_argument("task_id")
    p.add_argument("--platform", required=True)
    p.add_argument("--account", dest="account_id", required=True)

    p = sub.add_parser("publish-jobs", help="查看平台提交作业及回执")
    p.add_argument("task_id")

    p = sub.add_parser("confirm-published", help="在平台后台人工核实后登记发布链接，不再次提交")
    p.add_argument("task_id")
    p.add_argument("--note-url", required=True)

    sub.add_parser("platforms", help="查看已实现平台及能力范围")
    p = sub.add_parser("accounts", help="管理平台登录账号，与创作画像分别保存")
    accounts = p.add_subparsers(dest="account_action", required=True)
    accounts.add_parser("list", help="列出本地账号及已绑定身份（不输出 Cookie）")
    for action in ("add", "login", "whoami"):
        item = accounts.add_parser(action, help={"add": "登记本地账号", "login": "打开浏览器窗口登录", "whoami": "实际校验浏览器登录身份"}[action])
        item.add_argument("account_id")
        item.add_argument("--platform", default="xiaohongshu")
        if action == "add":
            item.add_argument("--label", default="")
        if action == "login":
            item.add_argument("--timeout", type=int, default=180, help="窗口内完成登录的等待秒数，默认 180")

    p = sub.add_parser("cancel", help="取消任务")
    p.add_argument("task_id", help="任务 ID")

    for name, help_text in [("retry", "从检查点重试失败节点"), ("export", "导出已人工审核的文案与元数据，不发布")]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("task_id", help="任务 ID")

    p = sub.add_parser("skills", help="管理本地技能库与 Agent 访问范围")
    actions = p.add_subparsers(dest="skill_action", required=True)
    actions.add_parser("init", help="初始化空技能库，不覆盖现有配置")
    listing = actions.add_parser("list", help="列出指定 Agent 可见的技能元信息")
    listing.add_argument("--agent", choices=AGENT_ROLES, required=True)
    for name, help_text in [("show", "读取允许访问的技能正文"), ("read", "读取允许访问的参考文件"),
                            ("grant", "管理员授权"), ("revoke", "管理员撤销授权")]:
        action = actions.add_parser(name, help=help_text)
        action.add_argument("agent", choices=AGENT_ROLES)
        action.add_argument("skill_id", help="技能 ID，如 skill-trending-topics")
        if name == "read":
            action.add_argument("resource", help="技能内相对路径或已登记的共享资源别名")
    importing = actions.add_parser("import", help="复制本地外部技能目录，不运行脚本")
    importing.add_argument("directory", help="包含 SKILL.md 的目录")
    importing.add_argument("--id", dest="skill_id", help="项目内唯一技能 ID，默认使用 SKILL.md 中的 name")
    importing.add_argument("--source", default="local", help="来源仓库或来源说明，默认 local")
    importing.add_argument("--revision", default="", help="来源版本/commit")
    importing.add_argument("--license-id", default="unknown", help="许可证标识")
    importing.add_argument("--license-file", help="同时复制的许可证文件")
    importing.add_argument("--agent", choices=AGENT_ROLES, action="append", default=[], help="显式授权角色，可重复；默认无授权")
    importing.add_argument("--tool", action="append", default=[], help="声明运行所需工具，不自动授权或安装")

    return parser


def _run_skills(args) -> int:
    library = (SkillLibrary.initialize(args.skills_dir) if args.skill_action == "init"
               else SkillLibrary(args.skills_dir))
    if args.skill_action == "init":
        print(f"技能库已就绪：{library.root}")
    elif args.skill_action == "list":
        print(json.dumps([s.model_dump() for s in library.for_agent(args.agent).catalog()],
                         ensure_ascii=False, indent=2))
    elif args.skill_action == "show":
        loaded = library.for_agent(args.agent).load(args.skill_id)
        print(loaded.instructions)
        print(f"\n可读资源：{json.dumps(loaded.resources, ensure_ascii=False)}")
        print(f"依赖声明（未自动授权）：{', '.join(loaded.required_tools) or '无'}")
    elif args.skill_action == "read":
        print(library.for_agent(args.agent).read_resource(args.skill_id, args.resource))
    elif args.skill_action in {"grant", "revoke"}:
        getattr(library, args.skill_action)(args.agent, args.skill_id)
        print(f"已更新 {args.agent} 的技能访问范围")
    elif args.skill_action == "import":
        imported = library.import_skill(
            args.directory, skill_id=args.skill_id,
            source=SkillSource(repository=args.source, revision=args.revision,
                               original_path=str(Path(args.directory)), license=args.license_id),
            agents=list(dict.fromkeys(args.agent)), required_tools=args.tool, license_path=args.license_file,
        )
        print(f"已导入 {imported.id}；授权角色：{', '.join(args.agent) or '无'}")
    return 0


def _run_accounts(args, data_dir: str) -> int:
    store = PlatformStore(data_dir)
    try:
        service = AccountService(store)
        if args.account_action == "list":
            print(json.dumps([a.model_dump() for a in store.accounts()], ensure_ascii=False, indent=2))
            return 0
        ref = AccountRef(platform=args.platform, account_id=args.account_id)
        if args.account_action == "add":
            print(service.add(ref, args.label).model_dump_json(indent=2))
            return 0
        print("正在打开浏览器；请在窗口中完成扫码或平台要求的验证。", flush=True)
        identity = (service.login(ref, timeout=args.timeout) if args.account_action == "login"
                    else service.whoami(ref))
        print(identity.model_dump_json(indent=2))
        return 0 if identity.state == "authenticated" else 1
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv(override=False, encoding="utf-8-sig")
    _ensure_utf8()
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir or _data_dir()
    svc = None
    try:
        if args.command == "skills":
            return _run_skills(args)
        if args.command == "platforms":
            print(create_platform_connector("xiaohongshu").capabilities().model_dump_json(indent=2))
            return 0
        if args.command == "accounts":
            return _run_accounts(args, data_dir)
        optional_models = {}
        if args.llm_config:
            for role in ("research", "creation", "review"):
                llm = load_role_llm(args.llm_config, role, required=False)
                if llm is not None:
                    optional_models.update({f"{role}_llm": llm, f"{role}_backend": "llm"})
        config = AgentConfig(
            **optional_models,
            execution=load_execution_config(args.execution_config or args.llm_config)
                if args.execution_config or args.llm_config else ExecutionConfig(),
            llm_config_path=str(Path(args.llm_config).resolve()) if args.llm_config else None,
            supervisor_backend="mock" if args.mock_supervisor else "llm",
            ideation_backend="mock" if args.mock_ideation else "llm",
            ideation_llm=load_role_llm(args.llm_config, "ideation") if args.llm_config and not args.mock_ideation else None,
            supervisor_llm=load_role_llm(args.llm_config, "supervisor") if args.llm_config and not args.mock_supervisor else None,
        )
        if args.command == "web":
            try:
                import uvicorn
                from .web import create_app
            except ImportError:
                raise RuntimeError("请先安装 Web 依赖：uv sync --extra web --extra browser") from None
            uvicorn.run(create_app(data_dir, config=config, skill_library=SkillLibrary(args.skills_dir)),
                        host=args.host, port=args.port, workers=1)
            return 0
        svc = TaskService(data_dir, skill_library=SkillLibrary(args.skills_dir), config=config)
        if args.command == "create":
            profile = load_profile(args.profile) if args.profile else UserProfile()
            trends = [TrendItem.model_validate(t) for t in json.loads(
                Path(args.trends).read_text(encoding="utf-8-sig")
            )] if args.trends else None
            task = svc.create_task(ContentRequest(
                goal=args.goal or args.domain, topic=args.domain,
                content_type=args.content_type, use_trends=not args.no_trends,
                needs_research=not args.no_research, supplied_materials=args.material,
                constraints=args.constraint,
                input_images=args.image, generate_images=args.generate_images,
            ), profile=profile, trends=trends)
            _print_task(task)
            ideas = svc.candidate_ideas(task.task_id)
            if ideas:
                print("-" * 40)
                print("候选选题：")
                for i, idea in enumerate(ideas):
                    print(f"  [{i}] {idea.title}")
                    print(f"      受众：{idea.audience}")
                    print(f"      角度：{idea.angle}")
                    print(f"      推荐原因：{idea.why}")
                    print(f"      画像匹配：{idea.profile_match}")
                    if idea.trend_urls:
                        print(f"      热点来源：{' '.join(idea.trend_urls)}")
            _print_pause(svc, task.task_id)
        elif args.command == "status":
            _print_task(svc.status(args.task_id))
            _print_pause(svc, args.task_id)
        elif args.command == "answer":
            _print_task(svc.answer(args.task_id, args.text))
            _print_pause(svc, args.task_id)
        elif args.command == "collaboration":
            print(json.dumps(svc.collaboration(args.task_id), ensure_ascii=False, indent=2))
        elif args.command == "approve":
            _print_task(svc.approve(args.task_id))
        elif args.command == "modify":
            _print_task(svc.request_changes(args.task_id, args.comment))
        elif args.command == "publish":
            _print_task(svc.publish(args.task_id, platform=args.platform, account_id=args.account_id))
        elif args.command == "replace-draft":
            path = Path(args.file).resolve()
            content = DraftContent.model_validate_json(path.read_text(encoding="utf-8-sig"))
            for image in content.images:
                if "://" not in image.source and not Path(image.source).is_absolute():
                    image.source = str(path.parent / image.source)
            _print_task(svc.replace_draft(args.task_id, content))
        elif args.command == "preflight":
            report = svc.platform_publishing.preflight(args.task_id, AccountRef(
                platform=args.platform, account_id=args.account_id))
            print(json.dumps({"valid": report.valid, **report.model_dump()}, ensure_ascii=False, indent=2))
        elif args.command == "publish-jobs":
            print(json.dumps([job.model_dump(mode="json") for job in svc.accounts.store.jobs(args.task_id)],
                             ensure_ascii=False, indent=2))
        elif args.command == "confirm-published":
            _print_task(svc.confirm_published(args.task_id, args.note_url))
        elif args.command == "cancel":
            _print_task(svc.cancel(args.task_id))
        elif args.command == "retry":
            _print_task(svc.retry(args.task_id))
        elif args.command == "export":
            print(f"已导出（不代表已发布）：{svc.export(args.task_id)}")
    except (KeyError, ValueError, OSError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", []):
            if note.startswith("任务 ID："):
                print(note, file=sys.stderr)
        return 1
    finally:
        if svc is not None:
            svc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
