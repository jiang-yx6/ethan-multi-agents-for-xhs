"""命令行入口。"""
from __future__ import annotations

import argparse
import os
import sys

from .models import TaskView
from .service import TaskService


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
    if t.draft:
        print("-" * 40)
        print(f"标题：{t.draft.title}")
        print(f"正文：\n{t.draft.body}")
        print("图片引用：")
        for img in t.draft.images:
            print(f"  - {img.description}  ({img.source})")
    if t.history:
        print("-" * 40)
        print("版本历史：")
        for h in t.history:
            mark = "已批准" if h["approved"] else "未批准"
            print(f"  v{h['version']}  {h['title']}  [{mark}]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xhs", description="小红书多 Agent 内容生产系统（第一版）")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认 $XHS_DATA_DIR 或 ./data）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="创建任务并生成首版草稿")
    p.add_argument("topic", help="选题/主题")

    p = sub.add_parser("status", help="查看任务状态")
    p.add_argument("task_id", help="任务 ID")

    p = sub.add_parser("approve", help="批准当前草稿")
    p.add_argument("task_id", help="任务 ID")

    p = sub.add_parser("modify", help="提出修改意见（生成新版本并重新审核）")
    p.add_argument("task_id", help="任务 ID")
    p.add_argument("--comment", "-c", required=True, help="修改意见")

    p = sub.add_parser("publish", help="发布已批准的草稿（Mock）")
    p.add_argument("task_id", help="任务 ID")

    p = sub.add_parser("cancel", help="取消任务")
    p.add_argument("task_id", help="任务 ID")

    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8()
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir or _data_dir()
    svc = TaskService(data_dir)
    try:
        if args.command == "create":
            _print_task(svc.create_task(args.topic))
        elif args.command == "status":
            _print_task(svc.status(args.task_id))
        elif args.command == "approve":
            _print_task(svc.approve(args.task_id))
        elif args.command == "modify":
            _print_task(svc.request_changes(args.task_id, args.comment))
        elif args.command == "publish":
            _print_task(svc.publish(args.task_id))
        elif args.command == "cancel":
            _print_task(svc.cancel(args.task_id))
    except (KeyError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    finally:
        svc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
