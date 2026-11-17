"""父图检查点序列化；精确白名单兼容旧模型子图保存的编码。"""
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from .llm import ChatMessage, ToolCall
from .models import (
    AskUserDecision, DelegateDecision, Draft, DraftContent, FinishDecision,
    IdeaCandidate, ResearchBrief, ReviewReport,
)


def checkpoint_serializer() -> JsonPlusSerializer:
    # 新 Runtime 不再继承父图 saver；保留旧类型白名单，只为读取历史编码。
    return JsonPlusSerializer(allowed_msgpack_modules=(
        ChatMessage, ToolCall, IdeaCandidate, ResearchBrief, Draft, DraftContent,
        ReviewReport, DelegateDecision, AskUserDecision, FinishDecision,
    ))
