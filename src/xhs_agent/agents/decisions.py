"""将模型选择的产物 ID/版本解析为可信的完整引用。"""
import json
from collections.abc import Mapping

from pydantic import TypeAdapter

from ..models.collaboration import ArtifactSelector, SupervisorContext, SupervisorDecision

SUPERVISOR_DECISION_ADAPTER = TypeAdapter(SupervisorDecision)


def supervisor_decision_schema() -> dict:
    # 内部决策和检查点继续保存完整引用；只有模型输出采用精简引用。
    schema = SUPERVISOR_DECISION_ADAPTER.json_schema()
    schema["$defs"]["ArtifactReference"] = ArtifactSelector.model_json_schema()
    return schema


def parse_supervisor_decision(value: str | dict | SupervisorDecision, context: SupervisorContext) -> SupervisorDecision:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return SUPERVISOR_DECISION_ADAPTER.validate_json(value)
    elif hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        return SUPERVISOR_DECISION_ADAPTER.validate_python(value)
    value = dict(value)
    available = {(ref.artifact_id, ref.version): ref for ref in context.artifacts}

    def resolve(refs, field):
        if not isinstance(refs, (list, tuple)):
            raise ValueError(f"{field}：产物引用必须是数组")
        resolved, seen = [], set()
        for index, raw in enumerate(refs):
            if hasattr(raw, "model_dump"):
                raw = raw.model_dump(mode="json")
            location = f"{field}[{index}]"
            if not isinstance(raw, Mapping):
                raise ValueError(f"{location}：产物引用必须是包含 artifact_id 和 version 的对象")
            # 兼容已有完整引用；摘要、类型均从可信目录获取，但不能声明其他任务。
            if "task_id" in raw and raw["task_id"] != context.task_id:
                raise ValueError(f"{location}：决策只能引用当前上下文已有的产物，任务归属不一致")
            selector = ArtifactSelector.model_validate({key: item for key, item in raw.items()
                                                        if key not in {"task_id", "kind", "summary"}})
            key = (selector.artifact_id, selector.version)
            if key in seen:
                raise ValueError(f"{location}：产物引用不能重复")
            seen.add(key)
            ref = available.get(key)
            if ref is None:
                problem = ("版本不在当前可用范围内" if any(item[0] == selector.artifact_id for item in available)
                           else "产物 ID 不存在或不在当前可用目录中")
                raise ValueError(f"{location}：决策只能引用当前上下文已有的产物；{problem}")
            if ref.task_id != context.task_id:
                raise ValueError(f"{location}：产物不属于当前任务")
            resolved.append(ref.model_dump(mode="json"))
        return resolved

    if value.get("action") == "delegate" and hasattr(value.get("task"), "model_dump"):
        value["task"] = value["task"].model_dump(mode="json")
    if value.get("action") == "delegate" and isinstance(value.get("task"), Mapping):
        task = dict(value["task"])
        task["input_artifacts"] = resolve(task.get("input_artifacts", []), "task.input_artifacts")
        value["task"] = task
    elif value.get("action") == "finish":
        value["artifacts"] = resolve(value.get("artifacts", []), "artifacts")
    return SUPERVISOR_DECISION_ADAPTER.validate_python(value)
