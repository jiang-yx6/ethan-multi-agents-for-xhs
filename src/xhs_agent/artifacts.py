"""按任务和版本归档内容；导出本地材料不会产生发布记录。"""
import json
from pathlib import Path

from .models import TaskView


def archive_task(task: TaskView, root: Path) -> Path:
    if task.draft is None:
        raise ValueError("任务还没有草稿")
    destination = root / task.task_id / f"v{task.current_version:03d}"
    destination.mkdir(parents=True, exist_ok=True)
    files = {
        "note.md": task.draft.body,
        "meta.json": task.draft.model_dump(mode="json"),
        "request.json": task.request.model_dump(mode="json") if task.request else {},
        "profile.json": task.profile.model_dump(mode="json"),
        "references/brief.json": task.brief.model_dump(mode="json") if task.brief else {},
        "references/trends.json": [t.model_dump(mode="json") for t in task.trends],
        "review.json": task.review.model_dump(mode="json") if task.review else {},
    }
    for name, content in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    return destination
