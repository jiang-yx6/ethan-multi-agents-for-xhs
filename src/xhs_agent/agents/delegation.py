"""将既有专业接口适配成统一 run；只执行明确委派，不替主 Agent 安排后续工作。"""
from ..artifacts import archive_task
from ..llm import LLMConnectionError, LLMHTTPError, LLMResponseError
from ..models import (
    ArtifactReference, ContentRequest, DelegationResult, DelegationTask, Draft, IdeaCandidate,
    ResearchBrief, ResearchRequest, ReviewReport, TrendItem,
)
from ..providers import EmptyTrendProvider
from ..team_store import TeamStore
from .base import SkillEnabledAgent


class MissingInput(ValueError):
    pass


class SpecialistDelegate(SkillEnabledAgent):
    def __init__(self, role, agent, store, trend_provider=None):
        self.role, self.agent, self.store = role, agent, store
        self.artifacts = TeamStore(store)
        self.trend_provider = trend_provider or EmptyTrendProvider()

    @property
    def available_tools(self):
        return self.agent.available_tools if hasattr(self.agent, "available_tools") else ("load_skill", "read_skill_resource")

    def bind_skills(self, skills):
        super().bind_skills(skills)
        if isinstance(self.agent, SkillEnabledAgent):
            self.agent.bind_skills(skills)

    def bind_tools(self, tool_names):
        super().bind_tools(tool_names)
        if isinstance(self.agent, SkillEnabledAgent):
            self.agent.bind_tools(tool_names)

    def run(self, task: DelegationTask) -> DelegationResult:
        if task.target_agent != self.role:
            raise ValueError("委派目标与执行器不匹配")
        base_version, cached = self.artifacts.begin(task)
        if cached:
            return cached
        current = self.store.get_task(task.task_id)
        if current.status in {"published", "submitted", "unknown", "cancelled"}:
            raise ValueError("已结束的任务不能继续执行内容委派")
        inputs = {ref.kind: self.artifacts.resolve(ref) for ref in task.input_artifacts}
        request = current.request.model_copy(deep=True)
        request.supplied_materials.extend(task.materials)
        if "material" in inputs:
            material = inputs["material"]
            values = [material] if isinstance(material, str) else material
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError("material 产物应为文本或文本数组")
            request.supplied_materials.extend(values)
        request.constraints.extend(["委派要求：" + task.instruction, *task.acceptance_criteria])
        # 已持久化的输出可以修复“业务提交成功，图检查点尚未提交”的间隙。
        outputs = [ref for ref in self.artifacts.artifacts(task.task_id)
                   if ref.artifact_id == "delegation:" + task.delegation_id]
        if outputs:
            ref = outputs[0]
        else:
            try:
                payload, kind, version, summary = self._execute(task, request, current.profile, inputs, base_version)
            except MissingInput as exc:
                result = DelegationResult(task_id=task.task_id, delegation_id=task.delegation_id,
                                          agent_id=self.role, status="needs_input", summary="委派缺少必要输入",
                                          missing_information=(str(exc),))
                self.artifacts.finish(task, result)
                return result
            except (LLMResponseError, LLMConnectionError, LLMHTTPError) as exc:
                # Provider 错误已脱敏；配置、存储与程序异常继续抛出，留在原节点重试。
                result = DelegationResult(task_id=task.task_id, delegation_id=task.delegation_id,
                                          agent_id=self.role, status="failed", summary=f"{self.role} Agent 执行失败",
                                          failure_reason=f"{type(exc).__name__}: {exc}")
                self.artifacts.finish(task, result)
                return result
            ref = ArtifactReference(task_id=task.task_id, artifact_id="delegation:" + task.delegation_id,
                                    kind=kind, version=version, summary=summary)
            self.artifacts.put_artifact(ref, payload)
        payload = self.artifacts.resolve(ref)
        if ref.kind == "ideas":
            self.store.save_stage(task.task_id, "ideas", payload)
        elif ref.kind == "brief":
            self.store.save_stage(task.task_id, "research", payload)
        elif ref.kind == "review":
            self.store.save_stage(task.task_id, "quality_review", payload, ref.version)
        if current.draft or ref.kind == "draft":
            archive_task(self.store.get_task(task.task_id), self.store.db_path.parent / "outputs")
        result = DelegationResult(task_id=task.task_id, delegation_id=task.delegation_id,
                                  agent_id=self.role, status="completed", summary=ref.summary, artifacts=(ref,))
        self.artifacts.finish(task, result)
        return result

    def _idea(self, task, request, profile, inputs):
        if "idea" in inputs:
            return IdeaCandidate.model_validate(inputs["idea"])
        if "ideas" in inputs:
            candidates = inputs["ideas"]
            if task.idea_index is None or task.idea_index >= len(candidates):
                raise MissingInput("引用候选选题时需要提供有效的 idea_index")
            return IdeaCandidate.model_validate(candidates[task.idea_index])
        return IdeaCandidate(title=request.topic or request.goal, why=request.goal,
                             audience=profile.audience, angle=request.content_type)

    def _brief(self, request, profile, idea, inputs):
        if "brief" in inputs:
            brief = ResearchBrief.model_validate(inputs["brief"])
            brief.supplied_materials = list(dict.fromkeys([*brief.supplied_materials, *request.supplied_materials]))
            return brief
        return ResearchBrief(topic=idea.title, target_audience=profile.audience,
                             pain_points=[request.goal], angles=[idea.angle], keywords=[idea.title], tags=[],
                             style=profile.style, objective=request.goal, content_type=request.content_type,
                             supplied_materials=request.supplied_materials,
                             pending_questions=["未执行外部调研，事实与经历以用户材料为准"])

    def _execute(self, task, request: ContentRequest, profile, inputs, base_version):
        if self.role == "ideation":
            trends = inputs.get("trends", self.store.get_stage(task.task_id, "trends"))
            if not request.use_trends:
                trends = []
            elif trends is None:
                trends = [t.model_dump(mode="json") for t in self.trend_provider.collect(request, profile)]
            self.store.save_stage(task.task_id, "trends", trends)
            ideas = self.agent.ideate(request, profile, [TrendItem.model_validate(t) for t in trends])
            if not isinstance(ideas, list) or not ideas or not all(isinstance(idea, IdeaCandidate) for idea in ideas):
                raise LLMResponseError("选题 Agent 必须返回非空的 IdeaCandidate 列表")
            return ([i.model_dump(mode="json") for i in ideas], "ideas", 1,
                    "候选选题：" + "；".join(f"{i}：{idea.title}（{idea.angle}）" for i, idea in enumerate(ideas)))
        idea = self._idea(task, request, profile, inputs)
        if self.role == "research":
            brief = self.agent.research(ResearchRequest(
                topic=idea.title, audience=idea.audience, objective=task.instruction, style=profile.style,
                constraints=[*request.constraints, *profile.boundaries], preferences=profile.preferences,
                supplied_materials=request.supplied_materials, content_request=request, profile=profile, idea=idea,
                trends=[TrendItem.model_validate(t) for t in inputs.get("trends", [])
                        if request.use_trends and (not idea.trend_urls or t["url"] in idea.trend_urls)],
            ))
            if not isinstance(brief, ResearchBrief) or not brief.topic.strip():
                raise LLMResponseError("调研 Agent 必须返回主题非空的 ResearchBrief")
            return brief.model_dump(mode="json"), "brief", 1, (
                f"简报：{brief.topic}；来源 {len(brief.sources)} 条；待核实：" + "；".join(brief.pending_questions))[:3000]
        brief = self._brief(request, profile, idea, inputs)
        if self.role == "creation":
            previous = Draft.model_validate(inputs["draft"]) if "draft" in inputs else None
            if base_version and (previous is None or previous.version != base_version):
                raise MissingInput("修改内容必须显式引用当前版本 draft")
            version = base_version + 1
            draft = self.store.get_draft(task.task_id, version)
            if draft is None:
                draft = self.agent.create(brief, feedback=task.instruction if previous else None,
                                          request=request, profile=profile, previous_draft=previous)
                if not isinstance(draft, Draft) or not draft.title.strip() or not draft.body.strip():
                    raise LLMResponseError("创作 Agent 必须返回标题和正文非空的 Draft")
                draft = draft.model_copy(update={"version": version, "approved": False, "approved_at": None})
                self.store.save_draft(task.task_id, draft)
            self.store.update_topic(task.task_id, idea.title)
            return draft.model_dump(mode="json"), "draft", version, f"草稿 v{version}：{draft.title}\n{draft.body[:1200]}"
        if "draft" not in inputs:
            raise MissingInput("审核需要显式提供 draft 产物引用")
        draft = Draft.model_validate(inputs["draft"])
        if draft.version != base_version:
            raise MissingInput("审核需要当前版本的 draft")
        report = self.agent.review(request, profile, brief, draft)
        if not isinstance(report, ReviewReport) or report.draft_version != draft.version:
            raise LLMResponseError("审核结果与草稿版本不一致")
        return report.model_dump(mode="json"), "review", draft.version, (
            f"审核 v{draft.version}：{'通过规则检查' if report.passed else '存在待解决问题'}；"
            + "；".join(f"{i.severity}/{i.target}：{i.message}" for i in report.issues))[:3000]
