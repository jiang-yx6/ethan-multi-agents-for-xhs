"""父图及其模型子图共用的检查点序列化配置。"""
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from .llm import ChatMessage, ToolCall
from .models import (
    AskUserDecision, DelegateDecision, Draft, DraftContent, FinishDecision,
    IdeaCandidate, ResearchBrief, ReviewReport,
)


def checkpoint_serializer() -> JsonPlusSerializer:
    # Runtime 的 messages 和 result 含自定义模型；子图会继承父图的 saver。
    # 只注册实际写入检查点的类型，保留旧检查点编码兼容性，禁止任意模块加载。
    return JsonPlusSerializer(allowed_msgpack_modules=(
        ChatMessage, ToolCall, IdeaCandidate, ResearchBrief, Draft, DraftContent,
        ReviewReport, DelegateDecision, AskUserDecision, FinishDecision,
    ))
