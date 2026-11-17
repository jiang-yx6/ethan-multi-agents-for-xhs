"""主图的人工交互节点；选题、修改、批准和发布确认均通过 interrupt 恢复。"""
from langgraph.types import Command, interrupt

from .artifacts import archive_task
from .conversation import conversation_messages
from .models import ArtifactReference, AskUserDecision, Draft, DraftContent, IdeaCandidate, IdeaSelection


class HumanNodes:
    def __init__(self, store, artifacts, context, emit):
        self.store, self.artifacts, self.context, self.emit = store, artifacts, context, emit

    def _continue(self, state, text, *, message_id, kind="message", context_text=None, **updates):
        return Command(goto="supervisor", update={
            "rounds": 0, "user_messages": [*state.get("user_messages", []), context_text or text],
            "conversation": [*conversation_messages(state), {"id": message_id, "role": "user",
                              "kind": kind, "content": text}], **updates,
        })

    def select_idea(self, state):
        ref = next(ref for ref in self.context(state).artifacts if ref.kind == "ideas")
        candidates = [IdeaCandidate.model_validate(item) for item in self.artifacts.resolve(ref)]
        if not candidates:
            raise ValueError("选题候选不能为空")
        answer = interrupt({"kind": "select_idea", "task_id": state["task_id"],
                            "ideas": ref.model_dump(mode="json"), "question": "请选择一个选题，或说明要求后重新生成。",
                            "hint": "select_idea / regenerate_ideas / cancel"})
        if isinstance(answer, dict) and answer.get("action") == "cancel":
            return Command(goto="cancelled")
        if not isinstance(answer, dict) or (answer.get("ideas_artifact_id"), answer.get("ideas_version")) != (ref.artifact_id, ref.version):
            raise ValueError("候选选题已变化，请刷新后重新操作")
        if answer.get("action") == "regenerate_ideas":
            feedback = answer.get("feedback")
            if not isinstance(feedback, str) or not feedback.strip():
                raise ValueError("请填写重新生成选题的要求")
            text = "请重新生成选题：" + feedback.strip()
            return self._continue(state, text, message_id=f"regenerate:{ref.artifact_id}:{ref.version}",
                                  regeneration_source=ref.model_dump(mode="json"), idea_feedback=feedback.strip(),
                                  selection_required=False)
        index = answer.get("idea_index")
        if answer.get("action") != "select_idea" or type(index) is not int or not 0 <= index < len(candidates):
            raise ValueError("请选择有效的候选选题")
        candidate = candidates[index]
        selected = ArtifactReference(task_id=state["task_id"], artifact_id="selection:" + ref.artifact_id,
                                     kind="idea", version=ref.version, summary=f"用户选定：{candidate.title}")
        self.artifacts.put_artifact(selected, candidate.model_dump(mode="json"))
        selection = IdeaSelection(candidates=ref, idea_index=index, idea=selected,
                                  draft_version_at_selection=self.store.get_task(state["task_id"]).current_version)
        text = f"已确认选题 {index + 1}：{candidate.title}"
        self.emit({"kind": "idea_selected", "message": text, "idea_index": index,
              "artifacts": [ref.model_dump(mode="json"), selected.model_dump(mode="json")]})
        return self._continue(state, text, message_id=f"selection:{ref.artifact_id}:{ref.version}", kind="answer",
                              idea_selection=selection.model_dump(mode="json"), regeneration_source={},
                              idea_feedback="", selection_required=False)

    def ask(self, state):
        decision = AskUserDecision.model_validate(state["decision"])
        answer = interrupt({"kind": "ask_user", "task_id": state["task_id"],
                            "question": decision.question, "missing_information": list(decision.missing_information),
                            "hint": "answer / cancel"})
        if isinstance(answer, dict) and answer.get("action") == "cancel":
            return Command(goto="cancelled")
        if not isinstance(answer, dict) or not isinstance(answer.get("answer"), str) or not answer["answer"].strip():
            raise ValueError("用户回复不能为空")
        message = f"问题：{decision.question}\n用户回复：{answer['answer'].strip()}"
        return self._continue(state, answer["answer"].strip(), message_id=f"user:{state['turn']}:answer",
                              kind="answer", context_text=message)

    def human_gate(self, kind):
        def node(state):
            version = state["draft"]["version"]
            answer = interrupt({"kind": kind, "task_id": state["task_id"], "version": version,
                                "hint": "approve / request_changes / replace_draft / cancel" if kind == "review"
                                else "publish / request_changes / replace_draft / cancel"})
            if not isinstance(answer, dict):
                raise ValueError("人工操作必须是结构化对象")
            action = answer.get("action")
            if action == "cancel":
                return Command(goto="cancelled")
            if action == "approve" and kind == "review":
                return Command(goto="mark_approved")
            if action == "publish" and kind == "publish_gate":
                return Command(goto="publish")
            if action == "message":
                text = answer.get("text", "").strip()
                if not text:
                    raise ValueError("消息不能为空")
                self.store.revoke_approval(state["task_id"])
                draft = self.store.get_task(state["task_id"]).draft
                return self._continue(state, text, message_id=f"user:{state['turn']}:message",
                                      context_text=f"对话 v{version}：{text}", draft=draft.model_dump(mode="json"))
            if action == "request_changes":
                text = answer.get("comment", "").strip()
                if not text:
                    raise ValueError("修改意见不能为空")
                return self._continue(state, text, message_id=f"user:{state['turn']}:changes", kind="changes",
                                      context_text=f"修改 v{version}：{text}")
            if action == "replace_draft":
                return Command(goto="replace_draft", update={"replacement": answer["draft"], "rounds": 0})
            raise ValueError("未知人工操作")
        return node

    def replace_draft(self, state):
        version = int(state["draft"]["version"]) + 1
        draft = self.store.get_draft(state["task_id"], version)
        if draft is None:
            content = DraftContent.model_validate(state["replacement"])
            draft = Draft(**content.model_dump(), version=version)
            self.store.save_draft(state["task_id"], draft)
        archive_task(self.store.get_task(state["task_id"]), self.store.db_path.parent / "outputs")
        self.artifacts.put_artifact(ArtifactReference(task_id=state["task_id"], artifact_id=f"manual:{draft.version}",
                                                 kind="draft", version=draft.version,
                                                 summary=f"用户替换的草稿 v{draft.version}：{draft.title}\n{draft.body[:1200]}"),
                               draft.model_dump(mode="json"))
        return {"draft": draft.model_dump(mode="json"), "replacement": {}}

    def mark_approved(self, state):
        self.store.approve_draft(state["task_id"], int(state["draft"]["version"]))
        return {}

    def cancelled(self, state):
        self.store.update_status(state["task_id"], "cancelled")
        return {}
