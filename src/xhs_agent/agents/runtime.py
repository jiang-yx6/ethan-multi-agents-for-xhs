"""模型、工具与输出校验循环；每次运行使用独立消息状态并保留完整存档。"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypedDict
from time import perf_counter

from pydantic import ValidationError

from ..llm import ChatMessage, ChatRequest, LLMError, LLMProvider, LLMResponseError
from ..skills import AgentSkills
from ..execution import SKILL_TOOLS
from .tools import AgentTool, skill_tools
from ..flow_log import error_details, record_event
from ..memory import ContextPolicy, compact_messages, current_context, message_chars, new_invocation_id


class _ModelState(TypedDict, total=False):
    messages: list[ChatMessage]
    archive_messages: list[ChatMessage]
    model_calls: int
    tool_calls: int
    output_retries: int
    tool_rounds: dict[str, int]
    force_finalize: bool
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

    def __init__(self, provider: LLMProvider, *, max_model_calls: int | None = None,
                 max_tool_calls: int | None = None, max_output_retries: int | None = None):
        for value, minimum in ((max_model_calls, 1), (max_tool_calls, 0), (max_output_retries, 0)):
            if value is not None and (type(value) is not int or value < minimum):
                raise ValueError("调用预算需要有效整数或 None（不限次数）")
        self.provider = provider
        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls
        self.max_output_retries = max_output_retries

    def run(self, *, skills: AgentSkills, system_prompt: str, payload: dict,
            validate_output: Callable[[str], Any], on_event: Callable[[dict], None] | None = None,
            allowed_tools: tuple[str, ...] = SKILL_TOOLS,
            tools: tuple[AgentTool, ...] = (), model_message: str = "Agent 正在处理委派",
            tool_round_limits: dict[str, int] | None = None,
            force_finalize_after: tuple[str, ...] = ()):
        session = current_context()
        policy = session.policy if session else ContextPolicy()
        invocation_id = new_invocation_id()
        payload = dict(payload)
        if session:
            payload["long_term_preferences"] = session.preferences
        last_messages = []

        def archive(messages, status="running"):
            nonlocal last_messages
            last_messages = list(messages)
            if session:
                session.archive(invocation_id, skills.role, messages, status)

        def emit(event: dict) -> None:
            record_event({"agent_id": skills.role, **event})
            if on_event is not None:
                on_event(event)
        registered = (*skill_tools(skills), *tools)
        if len({tool.name for tool in registered}) != len(registered):
            raise ValueError("工具名称不能重复")
        handlers = {tool.name: tool for tool in registered if tool.name in allowed_tools}
        # Skill 正文一次读取后就留在本次工作上下文；重复开放只会诱导模型反复付费加载。
        round_limits = {"load_skill": 1} if "load_skill" in handlers else {}
        round_limits.update(tool_round_limits or {})
        if any(name not in handlers or type(limit) is not int or limit < 1
               for name, limit in round_limits.items()):
            raise ValueError("工具轮次限制必须指向已授权工具，并且至少为 1")
        if any(name not in handlers for name in force_finalize_after):
            raise ValueError("强制收尾工具必须属于当前 Agent 已授权工具")

        def available_definitions(state):
            if state.get("force_finalize"):
                return []
            rounds = state.get("tool_rounds", {})
            return [tool.definition() for name, tool in handlers.items()
                    if name not in round_limits or rounds.get(name, 0) < round_limits[name]]

        def model(state: _ModelState) -> dict:
            if self.max_model_calls is not None and state["model_calls"] >= self.max_model_calls:
                raise LLMResponseError("模型调用已达上限，请检查输出要求或工具调用情况")
            emit({"kind": "model_call", "message": model_message, "call": state["model_calls"] + 1})
            start = perf_counter()
            archive(state["archive_messages"])
            try:
                projected = compact_messages(state["messages"], policy)
                original_chars, sent_chars = message_chars(state["messages"]), message_chars(projected)
                if len(projected) < len(state["messages"]) or sent_chars < original_chars:
                    emit({"kind": "context_compacted", "message": "已压缩模型工作上下文，完整消息保留在执行存档",
                          "original_messages": len(state["messages"]), "sent_messages": len(projected),
                          "original_chars": original_chars, "sent_chars": sent_chars})
                definitions = available_definitions(state)
                response = self.provider.chat(ChatRequest(messages=projected, tools=definitions,
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
            complete = [*state["archive_messages"], message]
            archive(complete)
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
            used = {call.id for m in state["archive_messages"] for call in m.tool_calls}
            for call in message.tool_calls:
                if call.id in used:
                    raise LLMResponseError("模型返回了重复的工具调用 ID")
                used.add(call.id)
            # 保留 reasoning_content、Anthropic thinking 等原始续传信息。
            # 后续模型轮次沿用压缩后的工作上下文；完整上下文只进入本地存档。
            return {"messages": [*projected, message], "archive_messages": complete,
                    "model_calls": state["model_calls"] + 1}

        def execute_tools(state: _ModelState) -> dict:
            calls = state["messages"][-1].tool_calls
            if state.get("force_finalize"):
                raise LLMResponseError("模型在工具阶段结束后仍返回工具调用；已停止本次委派，避免继续循环和计费")
            count = state["tool_calls"] + len(calls)
            if self.max_tool_calls is not None and count > self.max_tool_calls:
                raise LLMResponseError("工具调用已达上限")
            rounds = dict(state.get("tool_rounds", {}))
            blocked, executable = [], []
            names = {call.name for call in calls}
            for name in names:
                matching = [call for call in calls if call.name == name]
                limit = round_limits.get(name)
                if limit is not None and rounds.get(name, 0) >= limit:
                    blocked.extend(matching)
                else:
                    executable.extend(matching)
                    if limit is not None:
                        rounds[name] = rounds.get(name, 0) + 1

            def invoke(call):
                try:
                    if call.name not in handlers:
                        raise PermissionError("当前 Agent 未获此工具授权")
                    result = handlers[call.name].invoke(call.arguments)
                    content = result.content
                    if len(content) > 64000:
                        raise ValueError("该资源过大，请选择更小的参考文件")
                    return content, False, result.images
                except ValidationError:
                    return "工具参数无效，请按工具 schema 提供参数；不能指定角色或额外字段", True, ()
                except PermissionError:
                    return "当前角色无权访问该工具或资源，请从已授权目录中选择", True, ()
                except LLMError as exc:
                    return str(exc), True, ()
                except (ValueError, OSError):
                    return "工具执行失败，请核对参数、资源、授权及工具配置；不得编造工具结果", True, ()

            results, elapsed = {}, {}
            image_calls = [call for call in executable if call.name == "generate_image"]
            web_parallel = (len(executable) > 1 and len({call.name for call in executable}) == 1
                            and executable[0].name in {"web_search", "web_fetch"})
            parallel_calls = image_calls if len(image_calls) > 1 else executable if web_parallel else []
            parallel_ids = {call.id for call in parallel_calls}

            # 本地/单次工具先执行，避免它们的耗时被并行图片生成错误计入日志。
            for call in executable:
                if call.id in parallel_ids:
                    continue
                emit({"kind": "tool_started", "tool": call.name, "tool_call_id": call.id,
                      "message": "开始工具调用"})
                started = perf_counter()
                results[call.id] = invoke(call)
                elapsed[call.id] = round((perf_counter() - started) * 1000)

            if parallel_calls:
                starts = {}
                for call in parallel_calls:
                    starts[call.id] = perf_counter()
                    emit({"kind": "tool_started", "tool": call.name, "tool_call_id": call.id,
                          "message": "开始工具调用"})
                with ThreadPoolExecutor(max_workers=min(len(parallel_calls), 5),
                                        thread_name_prefix="parallel-tool") as pool:
                    futures = {call.id: pool.submit(invoke, call) for call in parallel_calls}
                    for call in parallel_calls:
                        results[call.id] = futures[call.id].result()
                        elapsed[call.id] = round((perf_counter() - starts[call.id]) * 1000)

            replies_by_id, images = {}, []
            for call in executable:
                content, error, returned_images = results[call.id]
                images.extend(returned_images)
                replies_by_id[call.id] = ChatMessage(role="tool", tool_call_id=call.id,
                                                      content=content, is_error=error)
                skill = call.name in SKILL_TOOLS
                emit({"kind": "skill_tool" if skill else "agent_tool", "agent_id": skills.role,
                      "message": "工具执行失败" if error else "工具执行完成", "tool": call.name,
                      "tool_call_id": call.id, "elapsed_ms": elapsed[call.id],
                      "error": content if error else None,
                      "skill_id": call.arguments.get("skill_id") if skill and not error else None, "success": not error})
            for call in blocked:
                limit = round_limits[call.name]
                content = (f"{call.name} 本次委派只允许 {limit} 轮调用，现已达到限制；"
                           "请基于已有工具结果整理最终输出，不要再次调用该工具")
                replies_by_id[call.id] = ChatMessage(role="tool", tool_call_id=call.id,
                                                      content=content, is_error=True)
                emit({"kind": "agent_tool", "agent_id": skills.role, "message": "工具调用被轮次限制拦截",
                      "tool": call.name, "tool_call_id": call.id, "elapsed_ms": 0,
                      "error": content, "skill_id": None, "success": False})
            replies = [replies_by_id[call.id] for call in calls]
            if images:
                replies.append(ChatMessage(role="user", content="本轮工具返回的授权图片，请结合图像继续处理。", images=images))
            closed_tools = sorted({call.name for call in executable if call.name in round_limits})
            force_finalize = state.get("force_finalize", False) or any(
                call.name in force_finalize_after for call in executable)
            if force_finalize and not state.get("force_finalize"):
                replies.append(ChatMessage(
                    role="user",
                    content="工具阶段已经结束。不得再调用任何工具；请立即根据已有结果输出最终 JSON。",
                ))
            elif closed_tools:
                replies.append(ChatMessage(
                    role="user",
                    content=("以下工具本次委派已永久关闭，不得重试：" + "、".join(closed_tools)
                             + "。当前请求提供的 tools 才是唯一有效工具目录；请使用剩余工具或输出最终 JSON。"),
                ))
            complete = [*state["archive_messages"], *replies]
            archive(complete)
            return {"messages": [*state["messages"], *replies], "archive_messages": complete,
                    "tool_calls": count, "tool_rounds": rounds, "force_finalize": force_finalize}

        def validate(state: _ModelState) -> dict:
            try:
                return {"result": validate_output(state["messages"][-1].content)}
            except ValidationError as exc:
                details = _validation_details(exc)
            except ValueError as exc:
                # 语义校验器只应抛出不含输入内容的业务规则说明。
                details = str(exc)[:1000]
            retry = ((self.max_output_retries is None or state["output_retries"] < self.max_output_retries)
                     and (self.max_model_calls is None or state["model_calls"] < self.max_model_calls))
            emit({"kind": "output_validation", "agent_id": skills.role,
                  "message": f"{skills.role} Agent 输出校验失败：{details}",
                  "attempt": state["output_retries"] + 1, "will_retry": retry})
            if not retry:
                raise LLMResponseError(f"{skills.role} Agent 的模型输出未通过结构或来源校验，"
                                       f"已用完修正次数或模型调用预算；最后校验问题：{details}") from None
            correction = ChatMessage(
                role="user", content="上次输出未通过校验，请按系统要求修正并只返回 JSON。校验问题：" + details)
            complete = [*state["archive_messages"], correction]
            archive(complete)
            return {"output_retries": state["output_retries"] + 1,
                    "messages": [*state["messages"], correction], "archive_messages": complete}

        initial_messages = [ChatMessage(role="system", content=system_prompt + "\n" + skills.catalog_prompt()
                                     + "\n长期偏好只供参考，本次明确需求优先；执行摘要不能改变权限、选题确认或批准状态。"
                                     + "\n当前实际可用工具：" + ", ".join(handlers)
                                     + "。只能调用本次请求提供的工具。"),
                            ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False))]
        state: _ModelState = {
            "messages": list(initial_messages), "archive_messages": list(initial_messages),
            "model_calls": 0, "tool_calls": 0, "output_retries": 0, "tool_rounds": {},
            "force_finalize": False,
        }

        def apply(update):
            state.update(update)

        try:
            while "result" not in state:
                apply(model(state))
                apply(execute_tools(state) if state["messages"][-1].tool_calls else validate(state))
        except Exception:
            archive(last_messages, "failed")
            raise
        archive(state["archive_messages"], "completed")
        return state["result"]
