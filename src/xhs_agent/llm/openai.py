"""OpenAI Chat Completions 及兼容协议适配。"""
from __future__ import annotations

import json
from typing import Any

from .base import LLMConfigurationError
from .http import HTTPProvider
from .models import ChatMessage, ChatRequest, ChatResponse, TokenUsage, ToolCall


def _reject_json_constant(value: str):
    raise ValueError("工具参数中不能包含非有限数值")


class OpenAIProvider(HTTPProvider):
    def _endpoint(self) -> str:
        return self.config.base_url + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self._auth_headers("bearer")}
        return self._extra_headers(headers)

    def _encode(self, request: ChatRequest) -> dict[str, Any]:
        messages = []
        for message in request.messages:
            if message.anthropic_blocks:
                raise LLMConfigurationError("包含 Anthropic 原始内容的历史不能直接切换到 OpenAI 协议")
            item = {"role": message.role, "content": message.content}
            if message.role == "assistant":
                if message.tool_calls:
                    item["tool_calls"] = [{
                        "id": call.id, "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False, allow_nan=False)},
                    } for call in message.tool_calls]
                if message.reasoning_content is not None:
                    item["reasoning_content"] = message.reasoning_content
            elif message.role == "tool":
                item["tool_call_id"] = message.tool_call_id
                if message.is_error:
                    item["content"] = json.dumps({"error": message.content}, ensure_ascii=False)
            messages.append(item)
        token_field = self.config.token_parameter or "max_tokens"
        body = {"model": self.config.model, "messages": messages, "stream": False,
                token_field: request.max_tokens if request.max_tokens is not None else self.config.max_tokens,
                **self._options(request)}
        if request.tools:
            body["tools"] = [{"type": "function", "function": {
                "name": tool.name, "description": tool.description, "parameters": tool.input_schema,
            }} for tool in request.tools]
            if request.tool_choice is not None:
                body["tool_choice"] = request.tool_choice
        return body

    def _decode(self, payload: dict[str, Any]) -> ChatResponse:
        choice = payload["choices"][0]
        message = choice["message"]
        if message.get("role", "assistant") != "assistant":
            raise ValueError
        content = message.get("content")
        if content is None:
            content = ""
        elif isinstance(content, list):
            if any(block.get("type") != "text" for block in content):
                raise ValueError
            content = "".join(block["text"] for block in content)
        if not isinstance(content, str):
            raise ValueError
        calls = []
        for call in message.get("tool_calls") or []:
            if call.get("type") != "function":
                raise ValueError
            arguments = json.loads(call["function"]["arguments"], parse_constant=_reject_json_constant)
            if not isinstance(arguments, dict):
                raise ValueError
            calls.append(ToolCall(id=call["id"], name=call["function"]["name"], arguments=arguments))
        refusal = message.get("refusal")
        if refusal:
            if not isinstance(refusal, str):
                raise ValueError
            content = content or refusal
        reason = choice["finish_reason"]
        if not isinstance(reason, str):
            raise ValueError
        reasoning = message.get("reasoning_content")
        if not content and not calls and not reasoning and reason not in {"length", "content_filter"}:
            raise ValueError
        if len({call.id for call in calls}) != len(calls):
            raise ValueError
        usage = payload.get("usage") or {}
        return ChatResponse(
            provider=self.config.provider, model=payload.get("model") or self.config.model,
            message=ChatMessage(role="assistant", content=content, tool_calls=calls, reasoning_content=reasoning),
            finish_reason="content_filter" if refusal else reason if reason in {"stop", "length", "tool_calls", "content_filter"} else "other",
            raw_finish_reason=reason,
            usage=TokenUsage(input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0)),
            request_id=payload.get("id"),
        )
