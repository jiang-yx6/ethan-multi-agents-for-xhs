"""本地规则审核；真实语义审核通过 ReviewAgent 协议注入。"""
from ..models import ContentRequest, Draft, ResearchBrief, ReviewIssue, ReviewReport, UserProfile
from .base import SkillEnabledAgent


class RuleReviewAgent(SkillEnabledAgent):
    def review(self, request: ContentRequest, profile: UserProfile,
               brief: ResearchBrief, draft: Draft) -> ReviewReport:
        issues = []
        if not draft.title.strip() or not draft.body.strip():
            issues.append(ReviewIssue(message="标题和正文不能为空"))
        source_urls = {source.url for source in brief.sources}
        if any(e.source_url not in source_urls for e in brief.evidence):
            issues.append(ReviewIssue(message="部分证据缺少对应来源", target="research"))
        if any(e.is_mock for e in brief.evidence):
            issues.append(ReviewIssue(message="研究包含 Mock 资料，不能作为真实事实依据", severity="warning"))
        if brief.pending_questions:
            issues.append(ReviewIssue(message="仍有信息需要用户确认", severity="warning"))
        if draft.revision_notes:
            issues.append(ReviewIssue(message="Mock 仅记录修改意见，需人工落实或接入真实创作模型", severity="warning"))
        if any(image.source.startswith("mock://") for image in draft.images):
            issues.append(ReviewIssue(message="配图仍为方案或占位引用", severity="warning"))
        return ReviewReport(draft_version=draft.version, issues=issues)
