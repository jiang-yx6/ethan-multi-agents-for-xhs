"""模型 Agent 共用的延迟初始化、角色凭据隔离与 Provider 生命周期。"""
from pydantic import ValidationError

from ..llm import LLMConfig, LLMConfigurationError, LLMProvider, create_llm_provider
from .base import SkillEnabledAgent


class ModelAgent(SkillEnabledAgent):
    role: str

    def __init__(self, provider: LLMProvider | None = None, *, config: LLMConfig | None = None, on_event=None):
        if provider is not None and config is not None:
            raise ValueError("provider 与 config 只能指定一个")
        self._provider, self._config = provider, config
        self._owns_provider = provider is None
        self._on_event = on_event

    def _get_provider(self) -> LLMProvider:
        if self._provider is None:
            try:
                config = self._config
                # 选题保留通用 XHS_LLM_ 入口；其他角色必须使用独立配置或角色环境变量。
                if self.role != "ideation":
                    config = config or LLMConfig.from_env(prefix=f"XHS_{self.role.upper()}_")
                    if config.api_key is None and config.api_key_env is None:
                        config = config.model_copy(update={"api_key_env": f"XHS_{self.role.upper()}_API_KEY"})
                self._provider = create_llm_provider(config)
            except (ValidationError, LLMConfigurationError) as exc:
                detail = str(exc) if isinstance(exc, LLMConfigurationError) else "请检查模型与连接参数"
                hint = ("选题模型配置未就绪：请配置 XHS_LLM_PROVIDER、XHS_LLM_MODEL 和厂商密钥，"
                        "或通过 --llm-config 指定 ideation；离线可用 --mock-ideation。" if self.role == "ideation" else
                        "父 Agent 需要独立 supervisor 配置；请配置模型和 XHS_SUPERVISOR_API_KEY。" if self.role == "supervisor" else
                        f"{self.role} 模型配置未就绪，请为该 Agent 独立配置接口和密钥。")
                raise LLMConfigurationError(hint + detail) from None
        return self._provider

    def close(self) -> None:
        if self._owns_provider and self._provider is not None:
            self._provider.close()
            self._provider = None
