"""Mock 创作 Agent。"""
from __future__ import annotations

from ..models import Draft, ImageRef, ResearchBrief


class MockCreationAgent:
    """Mock 创作 Agent：根据简报生成标题、正文与图片引用。

    确定性输出；未来可用真实 LLM 实现替换（实现
    :class:`~xhs_agent.agents.base.CreationAgent` 协议）。
    """

    def create(self, brief: ResearchBrief, feedback: str | None = None) -> Draft:
        revision = bool(feedback and feedback.strip())
        title = (
            f"{brief.topic}｜根据反馈修订版"
            if revision
            else f"{brief.topic}｜新手友好·一篇讲透"
        )

        parts: list[tuple[str, str]] = [
            ("开篇", f"大家好，今天来聊聊「{brief.topic}」。这篇内容面向{brief.target_audience}，帮你少走弯路。"),
            ("适合谁看", brief.target_audience),
            ("痛点", "、".join(brief.pain_points)),
            ("内容角度", "；".join(brief.angles)),
        ]
        if revision:
            parts.append(("本次修订", feedback))
        parts.append(("关键词", " ".join(brief.keywords)))
        parts.append(("标签", " ".join(brief.tags)))

        body = "\n\n".join(f"【{heading}】\n{content}" for heading, content in parts)

        images = [
            ImageRef(description=f"封面图：{brief.topic}", source="mock://cover"),
            ImageRef(
                description=f"配图：{brief.angles[0] if brief.angles else brief.topic}",
                source="mock://figure",
            ),
        ]
        return Draft(title=title, body=body, images=images)
