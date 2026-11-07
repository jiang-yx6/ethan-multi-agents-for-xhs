"""Anthropic Messages 协议适配；保留内容块供工具调用续传。"""
from __future__ import annotations

from typing import Any

from .base import LLMConfigurationError
from .http import HTTPProvider
from .models import ChatMessage, ChatRequest, ChatResponse, TokenUsage, ToolCall


class AnthropicProvider(HTTPProvider):
    def _endpoint(self) -> str:
        base = self.config.base_url
        return base + ("/messages" if base.endswith("/v1") else "/v1/messages")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "anthropic-version": self.config.anthropic_version,
                   **self._auth_headers("x-api-key")}
        return self._extra_headers(headers)

    def _encode(self, request: ChatRequest) -> dict[str, Any]:
        systems, messages = [], []
        for message in request.messages:
            if message.reasoning_content is not None:
                raise LLMConfigurationError("OpenAI reasoning_content 不能直接切换到 Anthropic 协议")
            if message.role == "system":
                if messages:
                    raise LLMConfigurationError("当前 Anthropic 适配要求 system 消息位于对话开头")
                if message.content:
                    systems.append(message.content)
                continue
            if message.role == "tool":
                block = {"type": "tool_result", "tool_use_id": message.tool_call_id, "content": message.content}
                if message.is_error:
                    block["is_error"] = True
                # 同一轮多个工具结果必须在同一条 user 消息中紧跟工具调用。
                if messages and messages[-1]["role"] == "user":
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
                continue
            if message.anthropic_blocks is not None:
                content = message.anthropic_blocks
                # 防止修改通用字段后仍被旧原始块静默覆盖。
                original_text = "".join(b["text"] for b in content if b.get("type") == "text")
                original_calls = [ToolCall(id=b["id"], name=b["name"], arguments=b["input"])
                                  for b in content if b.get("type") == "tool_use"]
                if original_text != message.content or original_calls != message.tool_calls:
                    raise LLMConfigurationError("修改 Anthropic 历史文本或工具调用后，请清除 anthropic_blocks")
            else:
                content = [{"type": "text", "text": message.content}] if message.content else []
                content += [{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                            for call in message.tool_calls]
            if not content:
                raise LLMConfigurationError("Anthropic 对话消息不能为空")
            messages.append({"role": message.role, "content": content})
        if not messages:
            raise LLMConfigurationError("Anthropic 请求至少需要一条非 system 消息")
        body = {"model": self.config.model, "messages": messages,
                "max_tokens": request.max_tokens if request.max_tokens is not None else self.config.max_tokens,
                "stream": False, **self._options(request)}
        if body.get("temperature", 0) > 1:
            raise LLMConfigurationError("Anthropic temperature 必须在 0 到 1 之间")
        if systems:
            body["system"] = "\n\n".join(systems)
        if request.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                             for t in request.tools]
            if request.tool_choice is not None:
                body["tool_choice"] = {"type": {"auto": "auto", "none": "none", "required": "any"}[request.tool_choice]}
        return body

    def _decode(self, payload: dict[str, Any]) -> ChatResponse:
        if payload.get("role", "assistant") != "assistant":
            raise ValueError
        blocks = payload["content"]
        if not isinstance(blocks, list):
            raise ValueError
        text, calls = [], []
        for block in blocks:
            kind = block["type"]
            if kind == "text":
                if not isinstance(block["text"], str):
                    raise ValueError
                text.append(block["text"])
            elif kind == "tool_use":
                calls.append(ToolCall(id=block["id"], name=block["name"], arguments=block["input"]))
            elif kind not in {"thinking", "redacted_thinking"}:
                raise ValueError
        if len({call.id for call in calls}) != len(calls):
            raise ValueError
        reason = payload["stop_reason"]
        if not isinstance(reason, str) or (not blocks and reason not in {"max_tokens", "refusal"}):
            raise ValueError
        usage = payload.get("usage") or {}
        return ChatResponse(
            provider=self.config.provider, model=payload.get("model") or self.config.model,
            message=ChatMessage(role="assistant", content="".join(text), tool_calls=calls, anthropic_blocks=blocks),
            finish_reason={"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
                           "tool_use": "tool_calls", "refusal": "content_filter"}.get(reason, "other"),
            raw_finish_reason=reason,
            usage=TokenUsage(input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0)),
            request_id=payload.get("id"),
        )
