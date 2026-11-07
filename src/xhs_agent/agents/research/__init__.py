"""研究 Agent 包：Mock 实现与内部模型。"""
from .agent import MockResearchAgent, MockSearchProvider
from .model import ResearchQuestion, RetrievedDocument, SearchHit, SearchProvider

__all__ = [
    "MockResearchAgent",
    "MockSearchProvider",
    "ResearchQuestion",
    "SearchHit",
    "RetrievedDocument",
    "SearchProvider",
]
