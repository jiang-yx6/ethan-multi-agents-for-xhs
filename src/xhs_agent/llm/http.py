"""共享 HTTP 传输与厂商扩展参数处理。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import ValidationError

from .base import LLMConfigurationError, LLMConnectionError, LLMHTTPError, LLMResponseError, LLMTimeoutError
from .config import LLMConfig
from .models import ChatRequest, ChatResponse

PROTECTED_FIELDS = {"model", "messages", "system", "stream", "tools", "tool_choice", "temperature",
                    "max_tokens", "max_completion_tokens", "n"}


class HTTPProvider(ABC):
    def __init__(self, config: LLMConfig, *, transport: httpx.BaseTransport | None = None):
        if config.base_url is None:
            raise LLMConfigurationError("Provider 需要已解析的 base_url，请通过工厂函数创建")
        if config.auth_required and (not config.api_key or not config.api_key.get_secret_value()):
            raise LLMConfigurationError("Provider 缺少 API Key")
        self.config = config
        self._client = httpx.Client(timeout=config.timeout, trust_env=config.trust_env,
                                    follow_redirects=False, transport=transport)

    @abstractmethod
    def _endpoint(self) -> str: ...

    @abstractmethod
    def _headers(self) -> dict[str, str]: ...

    @abstractmethod
    def _encode(self, request: ChatRequest) -> dict[str, Any]: ...

    @abstractmethod
    def _decode(self, payload: dict[str, Any]) -> ChatResponse: ...

    def _options(self, request: ChatRequest) -> dict[str, Any]:
        options = {**self.config.extra_body, **request.extra_body}
        if PROTECTED_FIELDS.intersection(options):
            raise LLMConfigurationError("extra_body 不能覆盖协议字段，请使用对应配置或请求字段")
        temperature = request.temperature if request.temperature is not None else self.config.temperature
        if temperature is not None:
            options["temperature"] = temperature
        return options

    def _extra_headers(self, headers: dict[str, str]) -> dict[str, str]:
        forbidden = {"authorization", "x-api-key", "anthropic-version", "host", "content-length", "content-type"}
        if any(name.lower() in forbidden for name in self.config.extra_headers):
            raise LLMConfigurationError("extra_headers 不能覆盖鉴权、版本和传输头")
        return {**headers, **{name: value.get_secret_value() for name, value in self.config.extra_headers.items()}}

    def _auth_headers(self, default_mode: str) -> dict[str, str]:
        key = self.config.api_key.get_secret_value() if self.config.api_key else ""
        if not key:
            return {}
        mode = self.config.auth_mode or default_mode
        return {"Authorization": f"Bearer {key}"} if mode == "bearer" else {"x-api-key": key}

    def chat(self, request: ChatRequest) -> ChatResponse:
        if not isinstance(request, ChatRequest):
            raise LLMConfigurationError("chat() 需要 ChatRequest 对象")
        try:
            body = self._encode(request)
            headers = self._headers()
        except (ValueError, TypeError, KeyError):
            raise LLMConfigurationError("LLM 请求参数或消息内容无法编码") from None
        try:
            response = self._client.post(self._endpoint(), json=body, headers=headers)
        except httpx.TimeoutException:
            raise LLMTimeoutError("LLM 请求超时") from None
        except httpx.HTTPError:
            raise LLMConnectionError("LLM 连接失败，请检查服务地址和网络") from None
        except (ValueError, TypeError):
            raise LLMConfigurationError("LLM 请求参数无法编码") from None
        if not 200 <= response.status_code < 300:
            raise LLMHTTPError(response.status_code)
        try:
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("error"):
                raise ValueError
            result = self._decode(payload)
            allowed = {tool.name for tool in request.tools}
            if any(call.name not in allowed for call in result.message.tool_calls):
                raise ValueError
            if result.message.tool_calls and (request.tool_choice == "none" or result.finish_reason != "tool_calls"):
                raise ValueError
            if result.finish_reason == "tool_calls" and not result.message.tool_calls:
                raise ValueError
            return result
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, ValidationError):
            raise LLMResponseError("LLM 响应格式无效或包含不支持的内容") from None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()
