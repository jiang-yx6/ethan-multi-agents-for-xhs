"""LLM 配置；模型显式指定，密钥通过环境变量或 SecretStr 注入。"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .models import APIProtocol, AuthMode


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    protocol: APIProtocol | None = None
    base_url: str | None = None
    api_key: SecretStr | None = Field(default=None, repr=False)
    api_key_env: str | None = None
    auth_required: bool = True
    auth_mode: AuthMode | None = None
    timeout: float = Field(default=120, gt=0)
    max_tokens: int = Field(default=4096, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    token_parameter: Literal["max_tokens", "max_completion_tokens"] | None = None
    anthropic_version: str = "2023-06-01"
    trust_env: bool = False
    extra_headers: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", "model", "api_key_env", "anthropic_version")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip() or any(c in value for c in "\r\n"):
                raise ValueError("配置值不能为空或包含换行")
            return value.strip()
        return value

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            url = urlsplit(value)
            valid = (url.scheme in {"http", "https"} and url.hostname and url.port != 0
                     and not url.username and not url.password and not url.query and not url.fragment
                     and not any(c.isspace() for c in value) and "\\" not in value)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("base_url 必须是无凭证、查询参数和片段的 HTTP(S) 地址")
        return value.rstrip("/")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, prefix: str = "XHS_LLM_",
                 **overrides: Any) -> LLMConfig:
        environment = os.environ if environ is None else environ
        mapping = {
            "PROVIDER": "provider", "MODEL": "model", "PROTOCOL": "protocol", "BASE_URL": "base_url",
            "API_KEY_ENV": "api_key_env", "TIMEOUT": "timeout", "MAX_TOKENS": "max_tokens",
            "TEMPERATURE": "temperature", "TOKEN_PARAMETER": "token_parameter", "TRUST_ENV": "trust_env",
            "AUTH_REQUIRED": "auth_required", "AUTH_MODE": "auth_mode", "ANTHROPIC_VERSION": "anthropic_version",
        }
        values = {field: environment[prefix + name] for name, field in mapping.items() if prefix + name in environment}
        if prefix + "API_KEY" in environment:
            values["api_key"] = SecretStr(environment[prefix + "API_KEY"])
        # 显式参数优先，且在校验前合并，允许用参数补齐环境中缺少的 model。
        values.update(overrides)
        return cls.model_validate(values)
