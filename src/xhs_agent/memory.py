"""完整存档与模型工作上下文分离；摘要不承担任何权限或状态判定。"""
import json
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .llm import ChatMessage, LLMResponseError


class ContextPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recent_delegations: int = Field(default=6, ge=1, le=30)
    recent_user_messages: int = Field(default=6, ge=1, le=30)
    recent_tool_rounds: int = Field(default=4, ge=1, le=20)
    max_input_chars: int = Field(default=96000, ge=4000, le=1000000)


class PreferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preferences: list[str] = Field(default_factory=list, max_length=50)


_active = ContextVar("agent_context_session", default=None)


def current_context():
    return _active.get()


def supervisor_payload(context, policy=None):
    """只投影模型输入；context 及其完整历史继续用于程序校验。"""
    policy = policy or ContextPolicy()
    payload = context.model_dump(mode="json")
    history, users = payload["history"], payload["user_messages"]
    old_history = history[:-policy.recent_delegations]
    old_users = users[:-policy.recent_user_messages]
    outcomes = {}
    for record in old_history:
        result = record["result"]
        outcomes[result["agent_id"]] = {
            "agent_id": result["agent_id"], "status": result["status"],
            "summary": result["summary"][:600],
            "artifacts": [{"artifact_id": ref["artifact_id"], "version": ref["version"]} for ref in result["artifacts"]],
        }
    payload["task_summary"] = {
        "covered_delegations": len(old_history), "covered_user_messages": len(old_users),
        "last_earlier_outcomes": list(outcomes.values()),
        "earlier_user_notes": [text[:600] for text in old_users[-6:]],
        "omitted_earlier_notes": max(0, len(old_users) - 6),
        "method": "结构化进展与早期消息摘录；完整记录留存在存档，不能据此推断批准或改变状态",
    }
    payload["history"] = history[-policy.recent_delegations:]
    payload["user_messages"] = users[-policy.recent_user_messages:]
    payload["context_window"] = {"total_delegations": len(history), "total_user_messages": len(users)}
    return payload


def message_chars(messages):
    """与工作上下文预算一致的字符计数，不包含图片 base64。"""
    return sum(len(item.content) + sum(len(json.dumps(call.model_dump(), ensure_ascii=False))
                                      for call in item.tool_calls) for item in messages)


def _tool_excerpt(message, limit):
    if message.role != "tool" or message.is_error or len(message.content) <= limit:
        return message
    notice = "工作上下文仅含部分结果，完整工具返回已存档；不能将省略部分当作已读证据。需要更多正文时用原工具分页读取。"
    try:
        value = json.loads(message.content)
    except ValueError:
        value = None
    # 网页正文保留可追溯元数据与准确的续读位置，避免截断 JSON 或丢失 URL。
    if (isinstance(value, dict) and isinstance(value.get("text"), str) and isinstance(value.get("url"), str)
            and type(value.get("offset", 0)) is int and value.get("offset", 0) >= 0):
        metadata = {key: item for key, item in value.items() if key != "text"}
        if len(json.dumps(metadata, ensure_ascii=False)) <= 2000:
            if len(value["text"]) <= limit:
                return message
            text = value["text"][:limit]
            value = {**metadata, "text": text, "context_excerpt": True,
                     "total_chars": value.get("total_chars", len(value["text"])), "truncated": True,
                     "next_offset": value.get("offset", 0) + len(text), "notice": notice}
        else:
            value = None
    else:
        value = None
    if value is None:
        value = {"context_excerpt": True, "original_chars": len(message.content),
                 "content_excerpt": message.content[:limit], "notice": notice}
    return message.model_copy(update={"content": json.dumps(value, ensure_ascii=False)})


def _fit_tool_excerpts(messages, budget):
    """只压缩工具正文投影，保留调用、全部回包、错误及必要任务输入。"""
    low, high, best = 256, max((len(item.content) for item in messages if item.role == "tool"), default=0), None
    while low <= high:
        limit = (low + high) // 2
        projected = [_tool_excerpt(item, limit) for item in messages]
        if message_chars(projected) <= budget:
            best, low = projected, limit + 1
        else:
            high = limit - 1
    return best


def compact_messages(messages, policy):
    """保留必要输入与最近轮次，超预算时工具正文显式摘录，不拆开调用/回包。"""
    prefix, groups = messages[:2], []
    for message in messages[2:]:
        if message.role == "assistant" or not groups:
            groups.append([])
        groups[-1].append(message)
    keep = min(len(groups), policy.recent_tool_rounds)
    while True:
        dropped = groups[:-keep] if keep else []
        retained = groups[-keep:] if keep else []
        summary = []
        if dropped:
            notes = []
            for group in dropped[-8:]:
                for message in group:
                    if message.role == "tool":
                        notes.append({"tool_call_id": message.tool_call_id, "is_error": message.is_error,
                                      "result_excerpt": message.content[:300]})
                    elif message.role == "assistant" and message.content:
                        notes.append({"assistant_excerpt": message.content[:300]})
            summary = [ChatMessage(role="user", content="此前执行摘要（仅供参考，原始记录已存档）：" + json.dumps(
                {"archived_rounds": len(dropped), "recent_earlier_notes": notes[-12:]}, ensure_ascii=False))]
        projected = [*prefix, *summary, *(item for group in retained for item in group)]
        # 图片由供应商按图像计费；这里预算的是文本和工具参数字符，不把 base64 当文本 Token。
        size = message_chars(projected)
        if size <= policy.max_input_chars:
            return projected
        if keep <= 1:
            fitted = _fit_tool_excerpts(projected, policy.max_input_chars)
            if fitted is not None:
                return fitted
            raise LLMResponseError(
                f"必要任务输入和最近一轮已超过上下文字符预算（{policy.max_input_chars} 字符），"
                "工具正文摘录后仍无法容纳；请缩小素材或提高 execution.context.max_input_chars")
        keep -= 1


class ContextSession:
    def __init__(self, store, task_id, policy, redact):
        self.store, self.task_id, self.policy, self.redact = store, task_id, policy, redact
        self.preferences = store.preferences(store.memory_scope(task_id))["preferences"]

    def prepare_supervisor(self, context):
        payload = supervisor_payload(context, self.policy)
        self.store.save_stage(self.task_id, "context_summary", payload["task_summary"])
        return payload

    def archive(self, invocation_id, role, messages, status):
        def clean(value):
            if isinstance(value, str):
                return self.redact(value)
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            return value
        # 不持久化模型隐藏推理和供应商 thinking 签名；任务、工具、公开回答完整保留。
        payload = clean([item.model_dump(mode="json", exclude={"reasoning_content", "anthropic_blocks"}) for item in messages])
        self.store.save_context_archive(self.task_id, invocation_id, role, payload, status)


@contextmanager
def context_session(store, task_id, policy, redact):
    session = ContextSession(store, task_id, policy, redact)
    token = _active.set(session)
    try:
        yield session
    finally:
        _active.reset(token)


def new_invocation_id():
    return uuid4().hex
