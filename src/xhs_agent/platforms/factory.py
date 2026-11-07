"""按平台名选择连接器；后续平台在这里注册，不扩散到 Agent。"""
from .base import PlatformConnector
from .xiaohongshu import XiaohongshuConnector


def create_platform_connector(platform: str) -> PlatformConnector:
    if platform == "xiaohongshu":
        return XiaohongshuConnector()
    raise ValueError(f"暂不支持的平台：{platform}")
