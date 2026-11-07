"""热点工具契约。默认无网络输入，绝不将预置示例标成当前热点。"""
from typing import Protocol

from .models import ContentRequest, TrendItem, UserProfile


class TrendProvider(Protocol):
    def collect(self, request: ContentRequest, profile: UserProfile) -> list[TrendItem]: ...


class EmptyTrendProvider:
    def collect(self, request: ContentRequest, profile: UserProfile) -> list[TrendItem]:
        return []
