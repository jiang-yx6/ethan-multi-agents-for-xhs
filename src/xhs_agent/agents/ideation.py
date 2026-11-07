"""离线选题示例：需求优先，热点只在相关时提供切入角度。"""
from __future__ import annotations

from ..models import ContentRequest, IdeaCandidate, TrendItem, UserProfile
from .base import SkillEnabledAgent


class MockIdeationAgent(SkillEnabledAgent):
    def ideate(self, request: ContentRequest, profile: UserProfile,
               trends: list[TrendItem]) -> list[IdeaCandidate]:
        topic = request.topic or request.goal
        # 简单关键词匹配仅用于离线演示；真实选题 Agent 可替换为语义匹配。
        anchors = [request.topic, request.goal, *profile.interests]
        related = [trend for trend in trends if request.use_trends and any(
            word and word.casefold() in " ".join(
                [trend.title, trend.summary, *trend.keywords]
            ).casefold() for word in anchors
        )]
        ideas = []
        if related:
            trend = related[0]
            ideas.append(IdeaCandidate(
                title=f"从{trend.title}聊聊{topic}",
                why=f"结合相关话题，服务本次目标：{request.goal}",
                audience=profile.audience, angle="热点观察",
                profile_match=f"结合账号定位：{profile.positioning}",
                content_type=request.content_type, trend_urls=[trend.url],
                required_materials=["核实事件最新进展与原始出处"],
            ))
        angles = [("实用清单", f"{topic}：值得收藏的实用清单"),
                  ("观点表达", f"关于{topic}，换个角度看"),
                  ("素材分享", f"围绕{topic}，整理一份分享")]
        for angle, title in angles:
            if len(ideas) == 3:
                break
            ideas.append(IdeaCandidate(
                title=title, why=f"围绕用户需求：{request.goal}",
                audience=profile.audience, angle=angle,
                profile_match=f"适合{profile.positioning}，表达风格：{profile.style}",
                content_type=request.content_type,
                required_materials=[] if request.supplied_materials else ["可选：用户的真实素材或观点"],
            ))
        return ideas
