"""协议与厂商分离：预设提供默认连接信息，工厂选择协议适配器。"""
from __future__ import annotations

import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import SecretStr, ValidationError

from .anthropic import AnthropicProvider
from .base import LLMConfigurationError, LLMProvider
from .config import LLMConfig
from .models import APIProtocol, AuthMode
from .openai import OpenAIProvider


@dataclass(frozen=True)
class ProviderPreset:
    protocol: APIProtocol
    base_url: str
    api_key_env: str
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    auth_required: bool = True
    auth_mode: AuthMode | None = None


_PRESETS = {
    "openai": ProviderPreset("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", "max_completion_tokens"),
    "anthropic": ProviderPreset("anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY"),
    "deepseek": ProviderPreset("openai", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "qwen": ProviderPreset("openai", "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "zhipu": ProviderPreset("openai", "https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY"),
    "ollama": ProviderPreset("openai", "http://localhost:11434/v1", "OLLAMA_API_KEY", auth_required=False),
    "minimax": ProviderPreset("anthropic", "https://api.minimax.io/anthropic", "MINIMAX_API_KEY"),
}
_ALIASES = {"dashscope": "qwen", "aliyun": "qwen", "glm": "zhipu", "claude": "anthropic"}
_REGISTRY_LOCK = threading.RLock()


def list_provider_presets() -> dict[str, ProviderPreset]:
    with _REGISTRY_LOCK:
        return dict(_PRESETS)


def _provider_name(value: str) -> str:
    name = value.strip().lower().replace("_", "-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise LLMConfigurationError("厂商名称只能包含字母、数字、连字符和下划线")
    return name


def register_provider_preset(name: str, preset: ProviderPreset, *,
                             aliases: tuple[str, ...] = (), replace: bool = False) -> None:
    """在本进程注册兼容厂商；不持有凭证、不发请求，默认拒绝覆盖。"""
    name = _provider_name(name)
    normalized_aliases = tuple(_provider_name(alias) for alias in aliases)
    if not isinstance(preset, ProviderPreset):
        raise LLMConfigurationError("preset 需要 ProviderPreset 对象")
    try:
        validated = LLMConfig(provider=name, model="preset-validation", protocol=preset.protocol,
                              base_url=preset.base_url, api_key_env=preset.api_key_env,
                              token_parameter=preset.token_parameter, auth_required=preset.auth_required,
                              auth_mode=preset.auth_mode)
    except ValidationError:
        raise LLMConfigurationError("厂商预设无效，请检查协议、地址、鉴权与 token 参数") from None
    if preset.protocol == "anthropic" and preset.token_parameter != "max_tokens":
        raise LLMConfigurationError("Anthropic 协议使用 max_tokens")
    if name in normalized_aliases or len(set(normalized_aliases)) != len(normalized_aliases):
        raise LLMConfigurationError("别名不能重复或与厂商名称相同")
    with _REGISTRY_LOCK:
        if name in _ALIASES or (name in _PRESETS and not replace):
            raise LLMConfigurationError("厂商名称已存在，更新预设需显式 replace=True")
        if any(alias in _PRESETS or (alias in _ALIASES and _ALIASES[alias] != name and not replace)
               for alias in normalized_aliases):
            raise LLMConfigurationError("厂商别名与已有名称冲突")
        _PRESETS[name] = ProviderPreset(
            validated.protocol, validated.base_url, validated.api_key_env,
            validated.token_parameter, validated.auth_required, validated.auth_mode,
        )
        _ALIASES.update({alias: name for alias in normalized_aliases})


def create_llm_provider(provider: str | LLMConfig | Mapping[str, Any] | None = None, *,
                        environ: Mapping[str, str] | None = None,
                        transport: httpx.BaseTransport | None = None, **overrides: Any) -> LLMProvider:
    """创建 Provider，不发起模型请求。未知厂商显式提供 protocol 和 base_url。"""
    environment = os.environ if environ is None else environ
    try:
        if provider is None:
            config = LLMConfig.from_env(environment, **overrides)
            overrides = {}
        else:
            values = ({"provider": provider} if isinstance(provider, str)
                      else provider.model_dump(exclude_unset=True) if isinstance(provider, LLMConfig)
                      else dict(provider))
            config = LLMConfig.model_validate({**values, **overrides})
        provided = config.model_fields_set
        name = _provider_name(config.provider)
        with _REGISTRY_LOCK:
            name = _ALIASES.get(name, name)
            preset = _PRESETS.get(name)
        protocol = config.protocol or (preset.protocol if preset else None)
        base_url = config.base_url or (preset.base_url if preset else None)
        if protocol is None or base_url is None:
            raise LLMConfigurationError("自定义厂商必须明确配置 protocol 和 base_url")
        if preset and protocol != preset.protocol and config.base_url is None:
            raise LLMConfigurationError("切换厂商预设协议时必须同时指定对应的 base_url")
        if config.api_key is not None:
            key = config.api_key.get_secret_value()
        elif config.api_key_env is not None:
            key = environment.get(config.api_key_env, "")
        else:
            key = environment.get("XHS_LLM_API_KEY") or (environment.get(preset.api_key_env, "") if preset else "")
        if key and any(c.isspace() for c in key):
            raise LLMConfigurationError("API Key 不能包含空白字符")
        auth_required = config.auth_required if "auth_required" in provided else preset.auth_required if preset else True
        if auth_required and not key:
            raise LLMConfigurationError("缺少 API Key，请通过 api_key 或指定环境变量提供")
        token_parameter = config.token_parameter or (
            preset.token_parameter if preset and protocol == preset.protocol else "max_tokens")
        if protocol == "anthropic" and token_parameter != "max_tokens":
            raise LLMConfigurationError("Anthropic 协议使用 max_tokens")
        resolved = LLMConfig.model_validate({
            **config.model_dump(), "provider": name, "protocol": protocol, "base_url": base_url,
            "api_key": SecretStr(key), "auth_required": auth_required, "token_parameter": token_parameter,
            "auth_mode": config.auth_mode or (preset.auth_mode if preset and protocol == preset.protocol else None)
                or ("bearer" if protocol == "openai" else "x-api-key"),
        })
    except (ValidationError, TypeError, ValueError):
        # 不把包含原始凭证的 Pydantic input_value 回显给上层。
        raise LLMConfigurationError("LLM 配置无效，请检查 provider、model、protocol 和连接参数") from None
    adapter = OpenAIProvider if protocol == "openai" else AnthropicProvider
    return adapter(resolved, transport=transport)
