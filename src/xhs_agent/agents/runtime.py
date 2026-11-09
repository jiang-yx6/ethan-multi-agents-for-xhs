"""使用项目 LLMProvider 的 LangGraph 模型/技能循环；每次运行使用独立消息状态。"""
from __future__ import annotations

import json
import operator
from collections.abc import Callable
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..llm import ChatMessage, ChatRequest, LLMProvider, LLMResponseError, ToolDefinition
from ..skills import AgentSkills
from .base import SKILL_READ_TOOLS


class _LoadSkillArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    skill_id: str = Field(min_length=1)


class _ReadResourceArgs(_LoadSkillArgs):
    relative: str = Field(min_length=1)


class _ModelState(TypedDict, total=False):
    messages: Annotated[list[ChatMessage], operator.add]
    model_calls: int
    tool_calls: int
    output_retries: int
    result: Any


class SkillModelRuntime:
    """只提供角色绑定的技能读取工具；实际联网、命令和发布需独立提供能力。"""

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
            allowed_tools: tuple[str, ...] = SKILL_READ_TOOLS):
        if not set(allowed_tools).issubset(SKILL_READ_TOOLS):
            raise ValueError("当前运行时未实现所配置的工具")
        def emit(event: dict) -> None:
            if on_event is not None:
                on_event(event)
        tools = [
            ToolDefinition(name="load_skill", description="加载当前角色已授权技能的正文和可读资源目录",
                           input_schema=_LoadSkillArgs.model_json_schema()),
            ToolDefinition(name="read_skill_resource", description="读取当前角色已授权技能内的参考资料或显式共享资源",
                           input_schema=_ReadResourceArgs.model_json_schema()),
        ]
        tools = [tool for tool in tools if tool.name in allowed_tools]

        def model(state: _ModelState) -> dict:
            if state["model_calls"] >= self.max_model_calls:
                raise LLMResponseError("模型调用已达上限，请检查输出要求或工具调用情况")
            emit({"kind": "model_call", "message": "正在生成候选方向", "call": state["model_calls"] + 1})
            response = self.provider.chat(ChatRequest(messages=state["messages"], tools=tools,
                                                       tool_choice="auto" if tools else "none"))
            message = response.message
            if message.role != "assistant":
                raise LLMResponseError("模型接口未返回 assistant 消息")
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
                raise LLMResponseError("技能工具调用已达上限")
            replies = []
            for call in calls:
                error = False
                try:
                    if call.name not in allowed_tools:
                        raise PermissionError("当前 Agent 未获此工具授权")
                    if call.name == "load_skill":
                        args = _LoadSkillArgs.model_validate(call.arguments)
                        content = skills.load(args.skill_id).model_dump_json()
                    elif call.name == "read_skill_resource":
                        args = _ReadResourceArgs.model_validate(call.arguments)
                        content = skills.read_resource(args.skill_id, args.relative)
                    else:
                        raise ValueError("未知工具；当前仅提供 load_skill 和 read_skill_resource")
                    if len(content) > 64000:
                        raise ValueError("该资源过大，请选择更小的参考文件")
                except ValidationError:
                    content, error = "工具参数无效，请按工具 schema 提供参数；不能指定角色或额外字段", True
                except PermissionError:
                    content, error = "当前角色无权访问该技能或资源，请从已授权目录中选择", True
                except (ValueError, OSError):
                    content, error = "技能或资源读取失败，请核对授权目录、资源名称和技能完整性；当前仅支持两个技能读取工具", True
                replies.append(ChatMessage(role="tool", tool_call_id=call.id, content=content, is_error=error))
                emit({"kind": "skill_tool", "message": "技能读取失败" if error else "已读取授权技能资料",
                      "tool": call.name, "skill_id": args.skill_id if not error else None, "success": not error})
            return {"messages": replies, "tool_calls": count}

        def validate(state: _ModelState) -> dict:
            try:
                return {"result": validate_output(state["messages"][-1].content)}
            except ValidationError as exc:
                details = json.dumps(exc.errors(include_input=False, include_context=False, include_url=False),
                                     ensure_ascii=False)
            except ValueError as exc:
                details = str(exc)
            if state["output_retries"] >= self.max_output_retries:
                raise LLMResponseError("模型输出未通过结构或来源校验，已用完修正次数")
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
                                     + "\n当前授权工具：" + ", ".join(allowed_tools)
                                     + "。只能调用本次请求提供的工具。"),
                         ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False))],
            "model_calls": 0, "tool_calls": 0, "output_retries": 0,
        }, {"recursion_limit": self.max_model_calls * 3 + 6})
        return final["result"]
