"""按需整理素材和检索证据的离线示例。"""
from __future__ import annotations

from datetime import datetime, timezone

from ...models import Evidence, ResearchBrief, ResearchRequest, Source
from ..base import SkillEnabledAgent
from .model import RetrievedDocument, SearchHit, SearchProvider


class MockSearchProvider:
    """确定性的假资料；始终携带 Mock 标识，不连接网络。"""

    is_mock = True

    def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        slug = "".join(ch for ch in query if ch.isalnum()) or "topic"
        return [SearchHit(title=f"[Mock] {query}", url=f"https://example.com/{slug}",
                          snippet="离线测试资料，不可作为事实依据")][:limit]

    def fetch(self, url: str) -> RetrievedDocument:
        return RetrievedDocument(
            title="[Mock] 示例背景资料", url=url,
            text="这是用于验证检索与证据传递的模拟文档，不包含可核实的真实结论。",
            retrieved_at=datetime.now(timezone.utc).isoformat(),
        )


class MockResearchAgent(SkillEnabledAgent):
    """保留检索正文片段，并将用户素材与外部证据分开。"""

    def __init__(self, provider: SearchProvider | None = None):
        self.provider = provider or MockSearchProvider()

    def research(self, request: ResearchRequest) -> ResearchBrief:
        content = request.content_request
        sources, evidence = [], []
        pending = []
        if content is None or content.needs_research:
            seen = set()
            for query in [request.topic, f"{request.topic} 背景"]:
                for hit in self.provider.search(query, limit=1):
                    if hit.url in seen:
                        continue
                    seen.add(hit.url)
                    doc = self.provider.fetch(hit.url)
                    sources.append(Source(
                        title=doc.title, url=doc.url, retrieved_at=doc.retrieved_at,
                        published_at=doc.published_at,
                    ))
                    evidence.append(Evidence(
                        source_url=doc.url, excerpt=doc.text[:1500],
                        limitations="原文摘录仍需结合上下文核实",
                        is_mock=bool(getattr(self.provider, "is_mock", False)),
                    ))
            if not evidence or all(e.is_mock for e in evidence):
                pending.append("补充或核实真实资料；当前未取得可用的真实事实依据")
        if not request.supplied_materials:
            pending.append("如需个人经历、体验或测评，请提供真实素材，不能代写成既成事实")
        angle = request.objective or "围绕用户目标组织内容"
        return ResearchBrief(
            topic=request.topic,
            target_audience=request.audience or request.profile.audience,
            pain_points=[content.goal if content else request.topic],
            angles=[angle, "结合已有素材展开", "给出适合读者的讨论或行动建议"],
            keywords=[content.topic or request.topic] if content else [request.topic],
            tags=[f"#{(content.topic or request.topic).replace(' ', '')}"] if content else [],
            style=request.style or request.profile.style,
            sources=sources, evidence=evidence,
            supplied_materials=request.supplied_materials,
            pending_questions=pending,
            content_type=content.content_type if content else "分享",
            objective=content.goal if content else angle,
        )
