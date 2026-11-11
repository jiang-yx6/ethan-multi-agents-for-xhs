"""研究 Agent 包：Mock 实现与内部模型。"""
from .agent import MockResearchAgent, MockSearchProvider
from .model import RetrievedDocument, SearchHit, SearchProvider

__all__ = [
    "MockResearchAgent",
    "MockSearchProvider",
    "SearchHit",
    "RetrievedDocument",
    "SearchProvider",
]
