"""根据角色配置组装 Agent；模型与外部工具仅在执行时连接。"""
from ..execution import ExecutionConfig
from .creation import MockCreationAgent
from .ideation import LLMIdeationAgent, MockIdeationAgent
from .images import DashScopeImageProvider, OpenAIImageProvider
from .publishing import MockPublisher
from .research import MockResearchAgent
from .research.search import TavilySearchProvider
from .review import RuleReviewAgent
from .structured import LLMCreationAgent, LLMResearchAgent, LLMReviewAgent
from .supervisor import LLMSupervisorAgent, MockSupervisorAgent


def create_agents(config, store, *, overrides=None, on_event=None, search_provider=None, image_provider=None):
    """显式注入优先于配置；返回角色到独立实例的映射。"""
    execution: ExecutionConfig = config.execution
    search_provider = search_provider if search_provider is not None else (
        TavilySearchProvider(execution.search) if execution.search else None)
    image_providers = {"openai": OpenAIImageProvider, "dashscope": DashScopeImageProvider}
    image_provider = image_provider if image_provider is not None else (
        image_providers[execution.images.provider](execution.images, store.db_path.parent / "generated-images")
        if execution.images else None)
    implementations = {
        "supervisor": (LLMSupervisorAgent, MockSupervisorAgent),
        "ideation": (LLMIdeationAgent, MockIdeationAgent),
        "research": (LLMResearchAgent, MockResearchAgent),
        "creation": (LLMCreationAgent, MockCreationAgent),
        "review": (LLMReviewAgent, RuleReviewAgent),
    }
    tools = {"research": {"search_provider": search_provider}, "creation": {"image_provider": image_provider}}
    overrides = overrides or {}
    agents = {}
    for role, (model_class, local_class) in implementations.items():
        if overrides.get(role) is not None:
            agents[role] = overrides[role]
        elif getattr(config, f"{role}_backend") == "llm":
            options = {} if role == "supervisor" else {"execution": execution.for_role(role), **tools.get(role, {})}
            agents[role] = model_class(config=getattr(config, f"{role}_llm"), on_event=on_event, **options)
        else:
            agents[role] = local_class()
    agents["publishing"] = overrides.get("publishing") if overrides.get("publishing") is not None else MockPublisher(store)
    return agents
