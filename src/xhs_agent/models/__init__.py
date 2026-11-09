"""按 Agent 职责组织模型，并通过包入口统一导出。"""
from .base import ContentRequest, TaskView, TrendItem, UserProfile, WorkflowInterrupt, WorkflowView, utcnow
from .creation import Draft, DraftContent, ImageRef
from .ideation import IdeaCandidate
from .publishing import PublishResult
from .research import Evidence, ResearchBrief, ResearchRequest, Source
from .review import ReviewIssue, ReviewReport

# TaskView 汇总各阶段结果；在模型加载完成后解析前向引用，
# 使 base 不需要在运行时反向导入各 Agent 的模型。
TaskView.model_rebuild(
    _types_namespace={
        "Draft": Draft,
        "ResearchBrief": ResearchBrief,
        "ReviewReport": ReviewReport,
        "PublishResult": PublishResult,
    }
)

__all__ = [
    "utcnow",
    "UserProfile",
    "ContentRequest",
    "TrendItem",
    "IdeaCandidate",
    "ResearchRequest",
    "Source",
    "Evidence",
    "ResearchBrief",
    "ImageRef",
    "Draft",
    "DraftContent",
    "PublishResult",
    "ReviewIssue",
    "ReviewReport",
    "TaskView",
    "WorkflowInterrupt",
    "WorkflowView",
]
