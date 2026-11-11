"""使用项目 LLMProvider 的 LangGraph 模型/技能循环；每次运行使用独立消息状态。"""
from __future__ import annotations

import json
import operator
import re
from collections.abc import Callable
from typing import Annotated, Any, TypedDict
from time import perf_counter

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from ..llm import ChatMessage, ChatRequest, LLMError, LLMProvider, LLMResponseError
from ..skills import AgentSkills
from .base import SKILL_READ_TOOLS
from .tools import AgentTool, skill_tools
from ..flow_log import error_details, record_event


class _ModelState(TypedDict, total=False):
    messages: Annotated[list[ChatMessage], operator.add]
    model_calls: int
    tool_calls: int
    output_retries: int
    result: Any


def _validation_details(exc: ValidationError) -> str:
    """只公开字段位置和错误类型；不回显模型原文或未知字段名称。"""
    problems = []
    for error in exc.errors(include_input=False, include_context=False, include_url=False)[:5]:
        location = error["loc"][:-1] if error["type"] == "extra_forbidden" else error["loc"]
        path = ".".join(str(part) for part in location
                        if isinstance(part, int) or re.fullmatch(r"[a-z_][a-z_0-9]{0,39}", str(part)))
        problems.append(f"{path or '$'}: {error['type']}")
    return "; ".join(problems)[:1000]


class ModelToolRuntime:
    """通用模型/工具/校验循环；能力由应用注入，消息只属于本次运行。"""

    def __init__(self, provider: LLMProvider, *, max_model_calls: int = 6,
                 max_tool_calls: int = 12, max_output_retries: int = 1):
        if not 1 <= max_model_calls <= 20 or not 0 <= max_tool_calls <= 40 or not 0 <= max_output_retries <= 3:
            raise ValueError("模型调用、工具调用或输出修正次数配置越界")
        self.provider = provider
        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls
        self.max_output_retries = max_output_retries

    def run(self, *, skills: AgentSkills, system_prompt: str, payload: dict,
            validate_output: Callable[[str], Any], on_event: Callable[[dict], None] | None = None,
            allowed_tools: tuple[str, ...] = SKILL_READ_TOOLS,
            tools: tuple[AgentTool, ...] = (), model_message: str = "Agent 正在处理委派"):
        def emit(event: dict) -> None:
            record_event({"agent_id": skills.role, **event})
            if on_event is not None:
                on_event(event)
        registered = (*skill_tools(skills), *tools)
        if len({tool.name for tool in registered}) != len(registered):
            raise ValueError("工具名称不能重复")
        handlers = {tool.name: tool for tool in registered if tool.name in allowed_tools}
        definitions = [tool.definition() for tool in handlers.values()]

        def model(state: _ModelState) -> dict:
            if state["model_calls"] >= self.max_model_calls:
                raise LLMResponseError("模型调用已达上限，请检查输出要求或工具调用情况")
            emit({"kind": "model_call", "message": model_message, "call": state["model_calls"] + 1})
            start = perf_counter()
            try:
                response = self.provider.chat(ChatRequest(messages=state["messages"], tools=definitions,
                                                           tool_choice="auto" if definitions else "none"))
            except Exception as exc:
                emit({"kind": "model_error", "message": "模型调用失败", **error_details(exc),
                      "call": state["model_calls"] + 1, "elapsed_ms": round((perf_counter() - start) * 1000)})
                raise
            emit({"kind": "model_result", "message": "模型调用返回", "provider": response.provider,
                  "model": response.model, "finish_reason": response.finish_reason,
                  "input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens,
                  "call": state["model_calls"] + 1, "elapsed_ms": round((perf_counter() - start) * 1000)})
            message = response.message
            if message.role != "assistant":
                raise LLMResponseError("模型接口未返回 assistant 消息")
            if response.finish_reason == "length":
                limit = getattr(getattr(self.provider, "config", None), "max_tokens", None)
                detail = f"（当前 max_tokens={limit}）" if limit is not None else ""
                raise LLMResponseError(f"{skills.role} Agent 的模型输出达到长度限制{detail}，结果可能不完整；"
                                       "请提高该 Agent 当前生效配置的最大输出 Token 后重试")
            if response.finish_reason not in {"stop", "tool_calls"}:
                raise LLMResponseError(f"模型未正常完成输出：{response.finish_reason}")
            if response.finish_reason == "tool_calls" and not message.tool_calls:
                raise LLMResponseError("模型要求调用工具，但没有提供工具调用")
            used = {call.id for m in state["messages"] for call in m.tool_calls}
            for call in message.tool_calls:
                if call.id in used:
                    raise LLMResponseError("模型返回了重复的工具调用 ID")
                used.add(call.id)
            # 保留 reasoning_content、Anthropic thinking 等原始续传信息。
            return {"messages": [message], "model_calls": state["model_calls"] + 1}

        def execute_tools(state: _ModelState) -> dict:
            calls = state["messages"][-1].tool_calls
            count = state["tool_calls"] + len(calls)
            if count > self.max_tool_calls:
                raise LLMResponseError("工具调用已达上限")
            replies, images = [], []
            for call in calls:
                start = perf_counter()
                emit({"kind": "tool_started", "tool": call.name, "tool_call_id": call.id, "message": "开始工具调用"})
                error = False
                try:
                    if call.name not in handlers:
                        raise PermissionError("当前 Agent 未获此工具授权")
                    result = handlers[call.name].invoke(call.arguments)
                    content = result.content
                    if len(content) > 64000:
                        raise ValueError("该资源过大，请选择更小的参考文件")
                    images.extend(result.images)
                except ValidationError:
                    content, error = "工具参数无效，请按工具 schema 提供参数；不能指定角色或额外字段", True
                except PermissionError:
                    content, error = "当前角色无权访问该工具或资源，请从已授权目录中选择", True
                except LLMError as exc:
                    content, error = str(exc), True
                except (ValueError, OSError):
                    content, error = "工具执行失败，请核对参数、资源、授权及工具配置；不得编造工具结果", True
                replies.append(ChatMessage(role="tool", tool_call_id=call.id, content=content, is_error=error))
                skill = call.name in SKILL_READ_TOOLS
                emit({"kind": "skill_tool" if skill else "agent_tool", "agent_id": skills.role,
                      "message": "工具执行失败" if error else "工具执行完成", "tool": call.name,
                      "tool_call_id": call.id, "elapsed_ms": round((perf_counter() - start) * 1000),
                      "error": content if error else None,
                      "skill_id": call.arguments.get("skill_id") if skill and not error else None, "success": not error})
            if images:
                replies.append(ChatMessage(role="user", content="本轮工具返回的授权图片，请结合图像继续处理。", images=images))
            return {"messages": replies, "tool_calls": count}

        def validate(state: _ModelState) -> dict:
            try:
                return {"result": validate_output(state["messages"][-1].content)}
            except ValidationError as exc:
                details = _validation_details(exc)
            except ValueError as exc:
                # 语义校验器只应抛出不含输入内容的业务规则说明。
                details = str(exc)[:1000]
            retry = (state["output_retries"] < self.max_output_retries
                     and state["model_calls"] < self.max_model_calls)
            emit({"kind": "output_validation", "agent_id": skills.role,
                  "message": f"{skills.role} Agent 输出校验失败：{details}",
                  "attempt": state["output_retries"] + 1, "will_retry": retry})
            if not retry:
                raise LLMResponseError(f"{skills.role} Agent 的模型输出未通过结构或来源校验，"
                                       f"已用完修正次数或模型调用预算；最后校验问题：{details}") from None
            return {"output_retries": state["output_retries"] + 1, "messages": [ChatMessage(
                role="user", content="上次输出未通过校验，请按系统要求修正并只返回 JSON。校验问题：" + details,
            )]}

        builder = StateGraph(_ModelState)
        builder.add_node("model", model)
        builder.add_node("tools", execute_tools)
        builder.add_node("validate", validate)
        builder.add_edge(START, "model")
        builder.add_conditional_edges("model", lambda state: (
            "tools" if state["messages"][-1].tool_calls else "validate"),
            {"tools": "tools", "validate": "validate"})
        builder.add_edge("tools", "model")
        builder.add_conditional_edges("validate", lambda state: "done" if "result" in state else "retry",
                                      {"done": END, "retry": "model"})
        graph = builder.compile()
        final = graph.invoke({
            "messages": [ChatMessage(role="system", content=system_prompt + "\n" + skills.catalog_prompt()
                                     + "\n当前实际可用工具：" + ", ".join(handlers)
                                     + "。只能调用本次请求提供的工具。"),
                         ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False))],
            "model_calls": 0, "tool_calls": 0, "output_retries": 0,
        }, {"recursion_limit": self.max_model_calls * 3 + 6})
        return final["result"]
