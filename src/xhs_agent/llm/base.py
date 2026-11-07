"""Provider 契约与可安全展示的错误类型。"""
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from .models import ChatRequest, ChatResponse


class LLMError(RuntimeError):
    """不包含请求凭证、上游响应正文或原始连接错误。"""


class LLMConfigurationError(LLMError):
    pass


class LLMResponseError(LLMError):
    pass


class LLMConnectionError(LLMError):
    pass


class LLMTimeoutError(LLMConnectionError):
    pass


class LLMHTTPError(LLMError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.retryable = status_code == 429 or status_code >= 500
        super().__init__(f"LLM 接口返回 HTTP {status_code}，请检查模型、凭证、配额与地址")


@runtime_checkable
class LLMProvider(Protocol):
    def chat(self, request: ChatRequest) -> ChatResponse: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
                 traceback: TracebackType | None) -> None: ...
