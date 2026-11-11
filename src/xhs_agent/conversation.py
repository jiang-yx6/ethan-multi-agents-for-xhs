"""对外展示的协作对话；不包含模型原始消息或推理内容。"""


def decision_message(decision: dict, turn: int) -> dict:
    action = decision["action"]
    task = decision.get("task", {})
    return {"id": f"assistant:{turn}", "role": "assistant", "kind": action,
            "content": (decision["question"] if action == "ask_user" else decision["summary"]
                        if action == "finish" else task["instruction"]),
            "reason": decision["reason"], "agent_id": task.get("target_agent"),
            "delegation_id": task.get("delegation_id")}


def conversation_messages(state: dict) -> list[dict]:
    if "conversation" in state:
        return list(state["conversation"])
    # 旧检查点的问答以字符串保存；按原顺序还原，不改写原数据。
    messages = []
    for index, raw in enumerate(state.get("user_messages", [])):
        question, marker, answer = raw.partition("\n用户回复：")
        if marker:
            messages.append({"id": f"legacy-question:{index}", "role": "assistant", "kind": "ask_user",
                             "content": question.removeprefix("问题：")})
        messages.append({"id": f"legacy-user:{index}", "role": "user", "kind": "message",
                         "content": answer if marker else raw})
    decision = state.get("decision")
    if decision and not (decision["action"] == "ask_user" and any(
            item["role"] == "assistant" and item["content"] == decision["question"] for item in messages)):
        messages.append(decision_message(decision, state.get("turn", 0) - 1))
    return messages
