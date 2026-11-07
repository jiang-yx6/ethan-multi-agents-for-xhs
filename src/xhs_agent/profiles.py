"""从用户指定的 JSON 文件加载画像，不修改全局配置。"""
from pathlib import Path

from .models import UserProfile


def load_profile(path: str | Path) -> UserProfile:
    return UserProfile.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))
