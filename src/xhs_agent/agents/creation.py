"""离线创作示例，不虚构用户经历；真实模型实现可替换此类。"""
from __future__ import annotations

from ..models import ContentRequest, Draft, ImageRef, ResearchBrief, UserProfile
from .base import SkillEnabledAgent


class MockCreationAgent(SkillEnabledAgent):
    def create(self, brief: ResearchBrief, feedback: str | None = None, *,
               request: ContentRequest, profile: UserProfile,
               previous_draft: Draft | None = None) -> Draft:
        if previous_draft and feedback:
            # 离线示例保留原文并记录意见，不伪装成已完成语义修改。
            revised = previous_draft.model_copy(deep=True)
            revised.approved = False
            revised.approved_at = None
            revised.revision_notes.append(feedback)
            revised.body += f"\n\n【待落实的修改意见（Mock）】\n{feedback}"
            return revised

        parts = [f"【离线示例草稿】\n{brief.topic}",
                 f"【本次目标】\n{request.goal}",
                 f"【面向读者】\n{brief.target_audience}",
                 f"【表达风格】\n{profile.style}",
                 "【内容结构】\n" + "\n".join(brief.angles)]
        if brief.supplied_materials:
            parts.append("【用户提供的素材】\n" + "\n".join(brief.supplied_materials))
        real_evidence = [e for e in brief.evidence if not e.is_mock]
        if real_evidence:
            parts.append("【参考资料摘录（待核实）】\n" + "\n".join(
                f"{e.excerpt}（{e.source_url}）" for e in real_evidence))
        if brief.pending_questions:
            parts.append("【待确认】\n" + "\n".join(brief.pending_questions))
        return Draft(
            title=brief.topic, body="\n\n".join(parts),
            tags=brief.tags, content_type=request.content_type,
            images=[ImageRef(description=f"封面方案：{brief.topic}", source="mock://cover")],
        )
