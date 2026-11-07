"""草稿到平台内容的确定性转换，以及审核时的本地媒体快照。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from ..models import Draft
from .models import MediaAsset, PlatformContent


def local_image_path(source: str) -> Path | None:
    if source.startswith("file://"):
        parsed = urlsplit(source)
        if parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
            raise ValueError("图片 file URI 只支持本机文件且不能包含查询或片段")
        return Path(url2pathname(parsed.path)).expanduser().resolve()
    if "://" in source or source.startswith("mock:"):
        return None
    return Path(source).expanduser().resolve() if source.strip() else None


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def draft_fingerprint(draft: Draft) -> str:
    content = draft.model_dump(mode="json", exclude={"approved", "approved_at", "created_at"})
    media = []
    for image in draft.images:
        path = local_image_path(image.source)
        media.append({"path": str(path) if path else image.source,
                      "sha256": file_hash(path) if path and path.is_file() else None})
    serialized = json.dumps({"draft": content, "media": media}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def adapt_draft(draft: Draft) -> PlatformContent:
    images = []
    for image in draft.images:
        path = local_image_path(image.source)
        if path is None or not path.is_file():
            raise ValueError("真实发布需要本地图片；请用 replace-draft 替换占位/远程引用并重新审核")
        images.append(MediaAsset(path=str(path), sha256=file_hash(path)))
    return PlatformContent(title=draft.title, body=draft.body, tags=draft.tags, images=images)
