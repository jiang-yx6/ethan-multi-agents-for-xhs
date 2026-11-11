"""与具体厂商无关的文本对话、工具调用和响应模型。"""
from __future__ import annotations

from typing import Any, Literal
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

APIProtocol = Literal["openai", "anthropic"]
AuthMode = Literal["bearer", "x-api-key"]


class ToolCall(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolDefinition(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    images: list[str] = Field(default_factory=list, repr=False)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False
    # DeepSeek 等兼容服务在工具续传时可能需要原始 reasoning_content。
    reasoning_content: str | None = Field(default=None, repr=False)
    # 保留 Anthropic thinking 签名等 content blocks，以便原样续传。
    anthropic_blocks: list[dict[str, Any]] | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_role_fields(self):
        if self.images and (self.role != "user" or any(not re.fullmatch(
                r"data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+", image) for image in self.images)):
            raise ValueError("图片只允许 user 消息中的 PNG/JPEG/WebP base64 数据")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("工具结果必须包含 tool_call_id")
        if self.role != "tool" and (self.tool_call_id is not None or self.is_error):
            raise ValueError("tool_call_id/is_error 仅用于工具结果")
        if self.role != "assistant" and (self.tool_calls or self.reasoning_content is not None or self.anthropic_blocks):
            raise ValueError("工具调用和模型原始内容仅用于 assistant 消息")
        return self


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1)
    tools: list[ToolDefinition] = Field(default_factory=list)
    tool_choice: Literal["auto", "none", "required"] | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_tools(self):
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("工具名称不能重复")
        if self.tool_choice == "required" and not self.tools:
            raise ValueError("required 工具选择需要提供工具定义")
        pending: set[str] = set()
        used: set[str] = set()
        for message in self.messages:
            if pending and message.role != "tool":
                raise ValueError("必须先回传所有工具结果，再继续对话")
            if message.role == "tool":
                if message.tool_call_id not in pending:
                    raise ValueError("工具结果没有对应的待处理调用，或已重复回传")
                pending.remove(message.tool_call_id)
            for call in message.tool_calls:
                if call.id in used:
                    raise ValueError("工具调用 ID 不能重复")
                used.add(call.id)
                pending.add(call.id)
        if pending:
            raise ValueError("请求包含尚未回传结果的工具调用")
        return self


class TokenUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ChatResponse(BaseModel):
    provider: str
    model: str
    message: ChatMessage
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter", "other"]
    raw_finish_reason: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    request_id: str | None = None

    @property
    def text(self) -> str:
        return self.message.content
