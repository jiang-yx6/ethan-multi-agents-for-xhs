"""按天追加的任务 JSONL 日志；ContextVar 隔离并发任务和嵌套模型节点。"""
import json
import os
import re
import sys
import threading
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from langgraph.errors import GraphInterrupt
from pydantic import BaseModel, SecretStr, ValidationError

_context = ContextVar("agent_flow_context", default={})
_sink = ContextVar("agent_flow_sink", default=None)
_write_lock = threading.Lock()
_fields = {
    "kind", "message", "task_id", "run_id", "node", "agent_id", "delegation_id", "turn", "rounds",
    "action", "status", "version", "cached", "instruction", "acceptance_criteria", "idea_index",
    "input_artifacts", "artifacts", "summary", "question", "missing_information", "failure_reason",
    "nodes", "interrupt_ids", "tool", "tool_call_id", "skill_id", "success", "call", "attempt",
    "will_retry", "elapsed_ms", "provider", "model", "finish_reason", "input_tokens", "output_tokens",
    "error_type", "error", "frames", "mode",
}


@contextmanager
def event_scope(**values):
    token = _context.set({**_context.get(), **values})
    try:
        yield
    finally:
        _context.reset(token)


def record_event(event):
    sink = _sink.get()
    if sink is not None:
        sink.write(event)


def error_details(exc):
    if isinstance(exc, ValidationError):
        message = "; ".join(error["type"] for error in exc.errors(include_input=False, include_context=False)[:5])
    else:
        message = str(exc)
    return {"error_type": type(exc).__name__, "error": message[:2000],
            "frames": [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                       for frame in traceback.extract_tb(exc.__traceback__)[-10:]]}


def _secrets(value):
    if isinstance(value, SecretStr):
        yield value.get_secret_value()
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _secrets(getattr(value, name))
    elif isinstance(value, dict):
        for item in value.values():
            yield from _secrets(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _secrets(item)


class FlowLog:
    def __init__(self, data_dir, config):
        self.directory = Path(data_dir) / "logs"
        self.secrets = tuple(sorted({value for value in _secrets(config) if value}, key=len, reverse=True))

    @contextmanager
    def run(self, task_id, run_id):
        token = _sink.set(self)
        try:
            with event_scope(task_id=task_id, run_id=run_id):
                yield
        finally:
            _sink.reset(token)

    def _redact(self, text):
        secrets = (*self.secrets, *(value for name, value in os.environ.items()
                                   if value and re.search(r"(?:API_KEY|ACCESS_TOKEN|SECRET|PASSWORD)$", name)))
        for secret in sorted(set(secrets), key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"\b(?:sk-|tvly-)[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
        text = re.sub(r"(?i)(bearer\s+)[^\s\"',;]+", r"\1[REDACTED]", text)
        return re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|password|authorization)\s*[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}]+",
                      r"\1[REDACTED]", text)

    def write(self, event):
        now = datetime.now(timezone.utc)
        values = {key: value for key, value in {**_context.get(), **event}.items() if key in _fields}
        # 产物只记录逻辑引用，不记录正文、图片 base64 或任意扩展字段。
        for key in ("artifacts", "input_artifacts"):
            if key in values:
                values[key] = [{name: ref[name] for name in ("task_id", "artifact_id", "kind", "version") if name in ref}
                               for ref in values[key]]
        def safe(value):
            if isinstance(value, str):
                return self._redact(value)[:4000]
            if isinstance(value, (list, tuple)):
                return [safe(item) for item in value]
            if isinstance(value, dict):
                return {key: safe(item) for key, item in value.items()}
            return value
        row = {"timestamp": now.isoformat(timespec="milliseconds"),
               "level": "ERROR" if values.get("kind", "").endswith(("failed", "error")) else
                        "WARNING" if values.get("status") == "failed" or values.get("success") is False else "INFO",
               **safe(values)}
        line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf8")
        try:
            with _write_lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                with (self.directory / f"agents-{now:%Y-%m-%d}.jsonl").open("ab") as stream:
                    stream.write(line)
        except OSError:
            # 日志故障不能令已提交的业务操作（尤其发布）再次执行。
            print("Agent flow log could not be written; check data/logs permissions or disk space.", file=sys.stderr)


def trace_node(name, handler, emit, *, agent_id=None):
    def traced(state):
        fields = {"task_id": state["task_id"], "node": name}
        if agent_id:
            fields["agent_id"] = agent_id
        if name == "execute_agent":
            task = state["decision"]["task"]
            fields.update(agent_id=task["target_agent"], delegation_id=task["delegation_id"])
        with event_scope(**fields):
            start = perf_counter()
            emit({"kind": "node_started", **fields, "message": f"开始节点：{name}"})
            try:
                result = handler(state)
            except GraphInterrupt:
                emit({"kind": "node_paused", **fields, "elapsed_ms": round((perf_counter() - start) * 1000),
                      "message": "节点等待人工操作"})
                raise
            except Exception as exc:
                emit({"kind": "node_failed", **fields, **error_details(exc),
                      "elapsed_ms": round((perf_counter() - start) * 1000), "message": f"节点失败：{name}"})
                raise
            emit({"kind": "node_completed", **fields, "elapsed_ms": round((perf_counter() - start) * 1000),
                  "message": f"完成节点：{name}"})
            return result
    return traced
