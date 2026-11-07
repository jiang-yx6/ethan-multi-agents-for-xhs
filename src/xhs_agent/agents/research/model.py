"""研究 Agent 内部模型：问题拆解与网络检索。"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class ResearchQuestion(BaseModel):
    """把选题拆解成的一个待调研问题。"""

    question: str
    purpose: str
    queries: list[str]
    required: bool = True


class SearchHit(BaseModel):
    """一次检索命中的结果条目。"""

    title: str
    url: str
    snippet: str


class RetrievedDocument(BaseModel):
    """抓取到的完整文档。"""

    title: str
    url: str
    text: str
    retrieved_at: str
    published_at: str | None = None


class SearchProvider(Protocol):
    """网络检索能力抽象：真实实现可注入搜索引擎 / 爬虫。"""

    def search(self, query: str, limit: int = 5) -> list[SearchHit]: ...

    def fetch(self, url: str) -> RetrievedDocument: ...
