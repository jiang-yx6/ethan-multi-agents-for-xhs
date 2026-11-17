"""Web 模型配置：启动配置、角色覆盖与凭据复制共用一个入口。"""
import hashlib
import json
import os
import re

from fastapi import APIRouter
from pydantic import SecretStr

from ..config import AgentConfig, MODEL_ROLES, ModelRole
from ..llm import list_provider_presets
from .models import CopyModelSettings, ModelSettings, ModelSettingsUpdate
from .repository import WebRepository


def credential_scope(values: dict, role: str = "ideation") -> str:
    """凭据绑定角色、服务商、协议和实际接口地址；模型 ID 变化可复用同一接口凭据。"""
    provider = values["provider"].strip().lower().replace("_", "-")
    preset = list_provider_presets().get(provider)
    protocol = values.get("protocol") or (preset.protocol if preset else None)
    url = values.get("base_url") or (preset.base_url if preset else "")
    return hashlib.sha256(json.dumps([role, provider, protocol, url.rstrip("/")]).encode()).hexdigest()


class ModelSettingsService:
    def __init__(self, base_config: AgentConfig, repository: WebRepository):
        self.base_config, self.repo = base_config, repository

    def router(self) -> APIRouter:
        router = APIRouter(prefix="/api/settings", tags=["模型配置"])
        router.add_api_route("", self.all, methods=["GET"])
        router.add_api_route("/{role}", self.read, methods=["GET"])
        router.add_api_route("/{role}", self.save, methods=["PUT"])
        router.add_api_route("/{role}", self.reset, methods=["DELETE"])
        router.add_api_route("/{role}/copy", self.copy, methods=["POST"])
        return router

    def effective_config(self) -> AgentConfig:
        updates = {}
        for role in MODEL_ROLES:
            saved = self.repo.setting(role)
            if saved is None:
                continue
            values = ModelSettings.model_validate(saved)
            # 每个角色单独查找加密密钥；显式空密钥阻止环境变量回退。
            api_key = ((self.repo.api_key(credential_scope(saved, role)) or SecretStr(""))
                       if values.credential_source == "api_key" else None)
            llm = values.llm_config(api_key) if values.backend == "llm" else None
            updates.update({f"{role}_backend": values.backend, f"{role}_llm": llm})
        return self.base_config.model_copy(update=updates)

    def _startup(self, role: ModelRole) -> dict:
        llm = getattr(self.base_config, f"{role}_llm")
        prefix = "XHS_LLM_" if role == "ideation" else f"XHS_{role.upper()}_"
        provider = llm.provider if llm else os.environ.get(prefix + "PROVIDER", "deepseek")
        preset = list_provider_presets().get(provider)
        default_key_env = (prefix + "API_KEY" if role != "ideation" else
                           "XHS_LLM_API_KEY" if os.environ.get("XHS_LLM_API_KEY") else
                           preset.api_key_env if preset else "XHS_LLM_API_KEY")
        key_env = (llm.api_key_env if llm else os.environ.get(prefix + "API_KEY_ENV")) or default_key_env
        # 即使启动文件误把密钥正文填进 api_key_env，也不将该值返回给浏览器。
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
            key_env = default_key_env
        public = {
            "backend": getattr(self.base_config, f"{role}_backend"), "provider": provider,
            "model": llm.model if llm else os.environ.get(prefix + "MODEL", ""), "api_key_env": key_env,
            "credential_source": "api_key" if (llm and llm.api_key) or not os.environ.get(key_env) else "environment",
            "protocol": llm.protocol if llm else os.environ.get(prefix + "PROTOCOL"),
            "base_url": llm.base_url if llm else os.environ.get(prefix + "BASE_URL"),
            "trust_env": llm.trust_env if llm else os.environ.get(prefix + "TRUST_ENV", "false").lower() in {"true", "1", "yes"},
            "max_tokens": llm.max_tokens if llm else int(os.environ.get(prefix + "MAX_TOKENS", 4096)),
            "reasoning_effort": llm.reasoning_effort if llm else os.environ.get(prefix + "REASONING_EFFORT"),
            "timeout": llm.timeout if llm else float(os.environ.get(prefix + "TIMEOUT", 120))}
        return self._public(public, bool(llm and llm.api_key))

    def _public(self, public: dict, stored: bool) -> dict:
        preset = list_provider_presets().get(public["provider"])
        configured = stored if public["credential_source"] == "api_key" else bool(os.environ.get(public["api_key_env"]))
        return {**public, "api_key_configured": stored,
                "credential_configured": configured or bool(preset and not preset.auth_required),
                "model_configured": bool(public["model"])}

    def read(self, role: ModelRole):
        saved = self.repo.setting(role)
        initial = self._startup(role)
        llm = getattr(self.base_config, f"{role}_llm")
        prefix = "XHS_LLM_" if role == "ideation" else f"XHS_{role.upper()}_"
        env_fields = ("PROVIDER", "MODEL", "PROTOCOL", "BASE_URL", "API_KEY", "API_KEY_ENV", "MAX_TOKENS",
                      "REASONING_EFFORT", "TIMEOUT", "TRUST_ENV", "TEMPERATURE", "TOKEN_PARAMETER",
                      "AUTH_MODE", "AUTH_REQUIRED")
        origin = ("file" if llm is not None and self.base_config.llm_config_path else "provided" if llm is not None else
                  "environment" if any(prefix + name in os.environ for name in env_fields) else "defaults")
        public = (self._public(ModelSettings.model_validate(saved).model_dump(),
                                  self.repo.has_api_key(credential_scope(saved, role))) if saved is not None else initial)
        return {**public, "config_source": "web" if saved is not None else "startup",
                "startup": {"origin": origin, "path": self.base_config.llm_config_path if origin == "file" else None,
                            "values": initial}}

    def reset(self, role: ModelRole):
        self.repo.delete_setting(role)
        return self.read(role)

    def save(self, role: ModelRole, body: ModelSettingsUpdate):
        if body.backend == "rules" and role != "review":
            raise ValueError("只有审核 Agent 可以使用规则审核模式")
        metadata = body.model_dump(exclude={"api_key", "clear_api_key"})
        if role != "ideation" and "api_key_env" not in body.model_fields_set:
            metadata["api_key_env"] = f"XHS_{role.upper()}_API_KEY"
        scope = credential_scope(metadata, role)
        key = body.api_key
        # 首次在 Web 保存来自启动文件的配置时，保留同一接口已有的显式密钥。
        initial = getattr(self.base_config, f"{role}_llm")
        if (key is None and not body.clear_api_key and self.repo.setting(role) is None and initial and initial.api_key
                and scope == credential_scope(initial.model_dump(), role)):
            key = initial.api_key
        self.repo.save_setting(metadata, role=role, scope=scope, api_key=key, clear_api_key=body.clear_api_key)
        return self.read(role)

    def all(self):
        return [{"role": role, **self.read(role)} for role in MODEL_ROLES]

    def copy(self, role: ModelRole, body: CopyModelSettings):
        if role == body.source_role:
            raise ValueError("请选择其他 Agent 的已配置模型")
        source = self.read(body.source_role)
        if source["backend"] != "llm" or not source["model_configured"] or not source["credential_configured"]:
            raise ValueError("来源 Agent 需要先保存完整的真实模型配置和凭据")
        metadata = ModelSettings.model_validate({key: source[key] for key in ModelSettings.model_fields}).model_dump()
        key = None
        if metadata["credential_source"] == "api_key":
            source_scope = credential_scope(metadata, body.source_role)
            key = self.repo.api_key(source_scope) if source["config_source"] == "web" else None
            initial = getattr(self.base_config, f"{body.source_role}_llm")
            if key is None and self.repo.setting(body.source_role) is None and initial:
                key = initial.api_key
            preset = list_provider_presets().get(metadata["provider"])
            if key is None and not (preset and not preset.auth_required):
                raise ValueError("来源模型没有可复制的密钥，请重新保存来源配置")
        # 目标角色使用独立凭据副本；之后修改/清除任何一方均不会影响另一方。
        self.repo.save_setting(metadata, role=role, scope=credential_scope(metadata, role), api_key=key,
                          clear_api_key=key is None)
        return self.read(role)
