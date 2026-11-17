"""本地规则审核；真实语义审核通过 ReviewAgent 协议注入。"""
from ..content_limits import (
    XIAOHONGSHU_BODY_MAX_CHARS, XIAOHONGSHU_TITLE_MAX_CHARS, xiaohongshu_rendered_body,
)
from ..models import ContentRequest, Draft, ResearchBrief, ReviewIssue, ReviewReport, UserProfile
from .base import SkillEnabledAgent


class RuleReviewAgent(SkillEnabledAgent):
    def review(self, request: ContentRequest, profile: UserProfile,
               brief: ResearchBrief, draft: Draft) -> ReviewReport:
        issues = []
        if not draft.title.strip():
            issues.append(ReviewIssue(message="标题不能为空", severity="error"))
        elif len(draft.title) > XIAOHONGSHU_TITLE_MAX_CHARS:
            issues.append(ReviewIssue(
                message=f"标题当前 {len(draft.title)} 个字符，超过小红书 {XIAOHONGSHU_TITLE_MAX_CHARS} 字符限制",
                severity="error",
            ))
        if not draft.body.strip():
            issues.append(ReviewIssue(message="正文不能为空", severity="error"))
        else:
            rendered_length = len(xiaohongshu_rendered_body(draft.body, draft.tags))
            if rendered_length > XIAOHONGSHU_BODY_MAX_CHARS:
                issues.append(ReviewIssue(
                    message=(f"正文与话题合计 {rendered_length} 个字符，超过小红书 "
                             f"{XIAOHONGSHU_BODY_MAX_CHARS} 字符限制"),
                    severity="error",
                ))
        source_urls = {source.url for source in brief.sources}
        if any(e.source_url not in source_urls for e in brief.evidence):
            issues.append(ReviewIssue(message="部分证据缺少对应来源", severity="warning", target="research"))
        if any(e.is_mock for e in brief.evidence):
            issues.append(ReviewIssue(message="研究包含 Mock 资料，不能作为真实事实依据", severity="warning"))
        if brief.pending_questions:
            issues.append(ReviewIssue(message="仍有信息需要用户确认", severity="warning"))
        if draft.revision_notes:
            issues.append(ReviewIssue(message="Mock 仅记录修改意见，需人工落实或接入真实创作模型", severity="warning"))
        if any(image.source.startswith("mock://") for image in draft.images):
            issues.append(ReviewIssue(message="配图仍为方案或占位引用", severity="warning"))
        return ReviewReport(draft_version=draft.version, issues=issues)
