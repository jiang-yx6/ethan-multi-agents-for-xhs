"""本地单用户 FastAPI 工作台，复用 TaskService 并提供持久化后台作业。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr
from dotenv import load_dotenv

from fastapi import FastAPI, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..config import AgentConfig, MODEL_ROLES, ModelRole
from ..llm import LLMConfig, list_provider_presets
from ..platforms import AccountRef, create_platform_connector
from ..platforms.content import local_image_path
from ..service import TaskService
from ..skills import SkillLibrary
from ..workflow import TERMINAL, WorkflowConflictError, allowed_actions, gate_status
from .jobs import JobRunner, public_job
from .models import (AccountInput, AnswerAction, ChangeAction, ConfirmAction, CopyModelSettings, CreateTask, DraftAction,
                     LoginAction, MessageAction, ModelSettings, ModelSettingsUpdate, ProfileInput, PublishAction,
                     ResumeAction, TargetInput, TaskAction)
from .repository import ConflictError, WebRepository


def fingerprint(kind: str, resource: str, payload: dict) -> str:
    body = {key: value for key, value in payload.items() if key != "request_id"}
    return hashlib.sha256(json.dumps([kind, resource, body], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def credential_scope(values: dict, role: str = "ideation") -> str:
    """凭据绑定角色、服务商、协议和实际接口地址；模型 ID 变化可复用同一接口凭据。"""
    provider = values["provider"].strip().lower().replace("_", "-")
    preset = list_provider_presets().get(provider)
    protocol = values.get("protocol") or (preset.protocol if preset else None)
    url = values.get("base_url") or (preset.base_url if preset else "")
    return hashlib.sha256(json.dumps([role, provider, protocol, url.rstrip("/")]).encode()).hexdigest()


def create_app(data_dir: str | Path = "data", *, config: AgentConfig | None = None,
               skill_library: SkillLibrary | None = None, service_factory=None,
               dist_dir: str | Path | None = None) -> FastAPI:
    load_dotenv(override=False, encoding="utf-8-sig")
    root = Path(data_dir).resolve()
    base_config = config or AgentConfig()
    library = skill_library or SkillLibrary()
    repo = WebRepository(root)

    def effective_config() -> AgentConfig:
        updates = {}
        for role in MODEL_ROLES:
            saved = repo.setting(role)
            if saved is None:
                continue
            values = ModelSettings.model_validate(saved)
            # 每个角色单独查找加密密钥；显式空密钥阻止环境变量回退。
            api_key = ((repo.api_key(credential_scope(saved, role)) or SecretStr(""))
                       if values.credential_source == "api_key" else None)
            llm = LLMConfig(provider=values.provider, model=values.model, api_key_env=values.api_key_env,
                            api_key=api_key, protocol=values.protocol, base_url=values.base_url,
                            trust_env=values.trust_env, max_tokens=values.max_tokens,
                            timeout=values.timeout) if values.backend == "llm" else None
            updates.update({f"{role}_backend": values.backend, f"{role}_llm": llm})
        return base_config.model_copy(update=updates)

    def make_service(on_event=None):
        current = effective_config()
        return (service_factory(current, on_event) if service_factory else
                TaskService(root, config=current, skill_library=library, on_event=on_event))

    @contextmanager
    def service():
        instance = make_service()
        try:
            yield instance
        finally:
            instance.close()

    runner = JobRunner(repo, make_service)

    @asynccontextmanager
    async def lifespan(app):
        with repo.execution_lock():
            runner.recover()
            try:
                yield
            finally:
                await asyncio.to_thread(runner.close)

    app = FastAPI(title="创作间 API", version="0.1.0", lifespan=lifespan)
    app.state.repository, app.state.runner = repo, runner
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_origins(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            allowed = {str(request.base_url).rstrip("/"), "http://localhost:5173", "http://127.0.0.1:5173"}
            if origin and origin not in allowed:
                return JSONResponse({"detail": "此本地工作区不接受该来源的写操作"}, status_code=403)
        response = await call_next(request)
        if request.url.path == "/api/settings" or request.url.path.startswith("/api/settings/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "请求字段或格式无效", "errors": [
            {"loc": error["loc"], "type": error["type"], "msg": error["msg"]} for error in exc.errors()
        ]}, status_code=422)

    @app.exception_handler(WorkflowConflictError)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc).strip("'")}, status_code=404)

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(RuntimeError)
    async def runtime_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def workflow_state(svc, task, job):
        workflow = svc.workflow(task.task_id)
        active = bool(job and job["status"] in {"queued", "running"})
        actions = allowed_actions(task, workflow, active=active)
        if active:
            workflow = workflow.model_copy(update={"status": job["status"]})
        elif (task.status not in TERMINAL and workflow.status == "waiting_input"
              and gate_status(task, workflow) != task.status):
            workflow = workflow.model_copy(update={"status": "inconsistent", "error": "任务与图暂停状态不一致，请恢复任务后再操作"})
        return {"workflow": workflow.model_dump(mode="json"), "allowed_actions": actions}

    def check_version(svc, task_id, expected):
        task = svc.status(task_id)
        if task.current_version != expected:
            raise ConflictError("内容版本已经变化，请刷新后再操作")
        return task

    def queue_action(task_id: str, kind: str, body: TaskAction):
        payload = body.model_dump(mode="json")
        digest = fingerprint(kind, task_id, payload)
        existing = repo.request_job(str(body.request_id), digest)
        if existing:
            return public_job(existing)
        with service() as svc:
            task = check_version(svc, task_id, body.expected_version)
            if kind not in workflow_state(svc, task, repo.latest(task_id=task_id))["allowed_actions"]:
                raise ConflictError("当前任务状态不支持该操作，请刷新状态")
            if isinstance(body, ResumeAction):
                svc.validate_resume(task_id, kind, body.interrupt_id)
            if kind == "publish":
                svc.platform_publishing.prepare(task_id, body.account)
            if kind == "draft":
                for image in body.images:
                    if image.media_id:
                        repo.media(str(image.media_id))
                    elif not task.draft or image.existing_index >= len(task.draft.images):
                        raise ValueError("图片引用不属于当前草稿")
        return runner.submit(request_id=str(body.request_id), fingerprint=digest, resource="task:" + task_id,
                             task_id=task_id, kind=kind, params=payload)

    @app.get("/api/health")
    def health():
        current = effective_config()
        return {"ok": True, "mode": "local", "ideation_backend": current.ideation_backend,
                "orchestration": "team",
                "supervisor_backend": current.supervisor_backend,
                "stages": {role: getattr(current, f"{role}_backend") for role in ("research", "creation", "review")}}

    @app.get("/api/tasks")
    def tasks(query: str = "", status: str = "", limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0)):
        with service() as svc:
            items, total = svc.store.list_tasks(query=query, status=status, limit=limit, offset=offset)
            return {"items": [{"task_id": t.task_id, "topic": t.topic, "status": t.status,
                    "current_version": t.current_version, "title": t.draft.title if t.draft else t.topic,
                    "profile_name": t.profile.name, "published": t.published,
                    "is_mock_publish": bool(t.published and (not t.publish_result or not t.publish_result.platform)),
                    "job": public_job(repo.latest(task_id=t.task_id))} for t in items], "total": total}

    @app.post("/api/tasks", status_code=202)
    def create(body: CreateTask):
        payload = body.model_dump(mode="json")
        digest = fingerprint("create", "new", payload)
        existing = repo.request_job(str(body.request_id), digest)
        if existing:
            return public_job(existing)
        for trend in body.trends:
            if urlsplit(trend.url).scheme not in {"http", "https"}:
                raise ValueError("热点来源须为 HTTP 或 HTTPS 链接")
        task_id = "xhs-" + uuid.uuid5(uuid.NAMESPACE_URL, "xhs-web:" + str(body.request_id)).hex
        with service() as svc:
            svc.register_task(body.request, repo.profile(body.profile_id), body.trends, task_id=task_id)
        return runner.submit(request_id=str(body.request_id), fingerprint=digest, resource="task:" + task_id,
                             task_id=task_id, kind="create", params={})

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str):
        with service() as svc:
            task = svc.status(task_id)
            job = repo.latest(task_id=task_id)
            images = [{"index": i, "description": image.description,
                       "url": f"/api/tasks/{task_id}/drafts/{task.current_version}/images/{i}",
                       "available": bool((path := local_image_path(image.source)) and path.is_file())}
                      for i, image in enumerate(task.draft.images if task.draft else [])]
            return {"task": task, "ideas": svc.candidate_ideas(task_id), "images": images,
                    "job": public_job(job), "events": repo.events(job["job_id"]) if job else [],
                    "collaboration": svc.collaboration(task_id), **workflow_state(svc, task, job)}

    @app.get("/api/tasks/{task_id}/workflow")
    def workflow(task_id: str):
        with service() as svc:
            return workflow_state(svc, svc.status(task_id), repo.latest(task_id=task_id))

    @app.get("/api/tasks/{task_id}/collaboration")
    def collaboration(task_id: str):
        with service() as svc:
            return svc.collaboration(task_id)

    @app.get("/api/tasks/{task_id}/artifacts")
    def artifact(task_id: str, artifact_id: str, version: int = Query(ge=1)):
        with service() as svc:
            return svc.artifact(task_id, artifact_id, version)

    @app.post("/api/tasks/{task_id}/message", status_code=202)
    def message(body: MessageAction, task_id: str):
        return queue_action(task_id, "message", body)

    @app.post("/api/tasks/{task_id}/answer", status_code=202)
    def answer(task_id: str, body: AnswerAction):
        return queue_action(task_id, "answer", body)

    @app.get("/api/tasks/{task_id}/drafts/{version}")
    def get_draft(task_id: str, version: int):
        with service() as svc:
            svc.status(task_id)
            draft = svc.store.get_draft(task_id, version)
            if draft is None:
                raise KeyError("草稿版本不存在")
            return draft

    @app.get("/api/tasks/{task_id}/ideas")
    def get_ideas(task_id: str):
        with service() as svc:
            return svc.candidate_ideas(task_id)

    @app.get("/api/tasks/{task_id}/drafts/{version}/images/{index}")
    def draft_image(task_id: str, version: int, index: int):
        with service() as svc:
            svc.status(task_id)
            draft = svc.store.get_draft(task_id, version)
            if not draft or not 0 <= index < len(draft.images):
                raise KeyError("图片不存在")
            path = local_image_path(draft.images[index].source)
            if path is None or not path.is_file():
                raise KeyError("此图片仍为方案或文件已不存在")
            return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    @app.post("/api/tasks/{task_id}/drafts", status_code=202)
    def replace(task_id: str, body: DraftAction): return queue_action(task_id, "draft", body)

    @app.post("/api/tasks/{task_id}/changes", status_code=202)
    def changes(task_id: str, body: ChangeAction): return queue_action(task_id, "changes", body)

    @app.post("/api/tasks/{task_id}/approve", status_code=202)
    def approve(task_id: str, body: ResumeAction): return queue_action(task_id, "approve", body)

    @app.post("/api/tasks/{task_id}/retry", status_code=202)
    def retry(task_id: str, body: TaskAction): return queue_action(task_id, "retry", body)

    @app.post("/api/tasks/{task_id}/reconcile", status_code=202)
    def reconcile(task_id: str, body: TaskAction): return queue_action(task_id, "reconcile", body)

    @app.post("/api/tasks/{task_id}/cancel", status_code=202)
    def cancel(task_id: str, body: ResumeAction): return queue_action(task_id, "cancel", body)

    @app.post("/api/tasks/{task_id}/publish", status_code=202)
    def publish(task_id: str, body: PublishAction): return queue_action(task_id, "publish", body)

    @app.post("/api/tasks/{task_id}/confirm-published", status_code=202)
    def confirm(task_id: str, body: ConfirmAction): return queue_action(task_id, "confirm", body)

    @app.post("/api/tasks/{task_id}/preflight")
    def preflight(task_id: str, body: TargetInput):
        with service() as svc:
            task = check_version(svc, task_id, body.expected_version)
            if "preflight" not in workflow_state(svc, task, repo.latest(task_id=task_id))["allowed_actions"]:
                raise ConflictError("当前图状态不支持发布检查")
            report = svc.platform_publishing.preflight(task_id, body.account)
            return {"valid": report.valid, **report.model_dump()}

    @app.get("/api/tasks/{task_id}/export")
    def export(task_id: str):
        with service() as svc:
            path = svc.export(task_id)
            return FileResponse(path / "note.md", filename=task_id + ".md", media_type="text/markdown; charset=utf-8")

    @app.get("/api/profiles")
    def profiles(): return repo.profiles()

    @app.post("/api/profiles")
    def add_profile(body: ProfileInput):
        identifier = repo.save_profile(body.profile)
        return {"profile_id": identifier, "profile": body.profile}

    @app.put("/api/profiles/{profile_id}")
    def update_profile(profile_id: str, body: ProfileInput):
        repo.profile(profile_id)
        repo.save_profile(body.profile, profile_id)
        return {"profile_id": profile_id, "profile": body.profile}

    def startup_settings(role: ModelRole) -> dict:
        llm = getattr(base_config, f"{role}_llm")
        prefix = "XHS_LLM_" if role == "ideation" else f"XHS_{role.upper()}_"
        provider = llm.provider if llm else os.environ.get(prefix + "PROVIDER", "deepseek")
        preset = list_provider_presets().get(provider)
        default_key_env = (prefix + "API_KEY" if role != "ideation" else
                           "XHS_LLM_API_KEY" if os.environ.get("XHS_LLM_API_KEY") else
                           preset.api_key_env if preset else "XHS_LLM_API_KEY")
        key_env = (llm.api_key_env if llm else os.environ.get(prefix + "API_KEY_ENV")) or default_key_env
        # 即使启动文件误把密钥正文填进 api_key_env，也不将该值返回给浏览器。
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
            key_env = default_key_env
        public = {
            "backend": getattr(base_config, f"{role}_backend"), "provider": provider,
            "model": llm.model if llm else os.environ.get(prefix + "MODEL", ""), "api_key_env": key_env,
            "credential_source": "api_key" if (llm and llm.api_key) or not os.environ.get(key_env) else "environment",
            "protocol": llm.protocol if llm else os.environ.get(prefix + "PROTOCOL"),
            "base_url": llm.base_url if llm else os.environ.get(prefix + "BASE_URL"),
            "trust_env": llm.trust_env if llm else os.environ.get(prefix + "TRUST_ENV", "false").lower() in {"true", "1", "yes"},
            "max_tokens": llm.max_tokens if llm else int(os.environ.get(prefix + "MAX_TOKENS", 4096)),
            "timeout": llm.timeout if llm else float(os.environ.get(prefix + "TIMEOUT", 120))}
        return public_settings(public, bool(llm and llm.api_key))

    def public_settings(public: dict, stored: bool) -> dict:
        preset = list_provider_presets().get(public["provider"])
        configured = stored if public["credential_source"] == "api_key" else bool(os.environ.get(public["api_key_env"]))
        return {**public, "api_key_configured": stored,
                "credential_configured": configured or bool(preset and not preset.auth_required),
                "model_configured": bool(public["model"])}

    @app.get("/api/settings/{role}")
    def settings(role: ModelRole):
        saved = repo.setting(role)
        initial = startup_settings(role)
        llm = getattr(base_config, f"{role}_llm")
        prefix = "XHS_LLM_" if role == "ideation" else f"XHS_{role.upper()}_"
        env_fields = ("PROVIDER", "MODEL", "PROTOCOL", "BASE_URL", "API_KEY", "API_KEY_ENV", "MAX_TOKENS",
                      "TIMEOUT", "TRUST_ENV", "TEMPERATURE", "TOKEN_PARAMETER", "AUTH_MODE", "AUTH_REQUIRED")
        origin = ("file" if llm is not None and base_config.llm_config_path else "provided" if llm is not None else
                  "environment" if any(prefix + name in os.environ for name in env_fields) else "defaults")
        public = (public_settings(ModelSettings.model_validate(saved).model_dump(),
                                  repo.has_api_key(credential_scope(saved, role))) if saved is not None else initial)
        return {**public, "config_source": "web" if saved is not None else "startup",
                "startup": {"origin": origin, "path": base_config.llm_config_path if origin == "file" else None,
                            "values": initial}}

    @app.delete("/api/settings/{role}")
    def follow_startup_settings(role: ModelRole):
        repo.delete_setting(role)
        return settings(role)

    @app.put("/api/settings/{role}")
    def save_settings(role: ModelRole, body: ModelSettingsUpdate):
        if body.backend == "rules" and role != "review":
            raise ValueError("只有审核 Agent 可以使用规则审核模式")
        metadata = body.model_dump(exclude={"api_key", "clear_api_key"})
        if role != "ideation" and "api_key_env" not in body.model_fields_set:
            metadata["api_key_env"] = f"XHS_{role.upper()}_API_KEY"
        scope = credential_scope(metadata, role)
        key = body.api_key
        # 首次在 Web 保存来自启动文件的配置时，保留同一接口已有的显式密钥。
        initial = getattr(base_config, f"{role}_llm")
        if (key is None and not body.clear_api_key and repo.setting(role) is None and initial and initial.api_key
                and scope == credential_scope(initial.model_dump(), role)):
            key = initial.api_key
        repo.save_setting(metadata, role=role, scope=scope, api_key=key, clear_api_key=body.clear_api_key)
        return settings(role)

    @app.get("/api/settings")
    def all_model_settings():
        return [{"role": role, **settings(role)} for role in MODEL_ROLES]

    @app.post("/api/settings/{role}/copy")
    def copy_model_settings(role: ModelRole, body: CopyModelSettings):
        if role == body.source_role:
            raise ValueError("请选择其他 Agent 的已配置模型")
        source = settings(body.source_role)
        if source["backend"] != "llm" or not source["model_configured"] or not source["credential_configured"]:
            raise ValueError("来源 Agent 需要先保存完整的真实模型配置和凭据")
        metadata = ModelSettings.model_validate({key: source[key] for key in ModelSettings.model_fields}).model_dump()
        key = None
        if metadata["credential_source"] == "api_key":
            source_scope = credential_scope(metadata, body.source_role)
            key = repo.api_key(source_scope) if source["config_source"] == "web" else None
            initial = getattr(base_config, f"{body.source_role}_llm")
            if key is None and repo.setting(body.source_role) is None and initial:
                key = initial.api_key
            preset = list_provider_presets().get(metadata["provider"])
            if key is None and not (preset and not preset.auth_required):
                raise ValueError("来源模型没有可复制的密钥，请重新保存来源配置")
        # 目标角色使用独立凭据副本；之后修改/清除任何一方均不会影响另一方。
        repo.save_setting(metadata, role=role, scope=credential_scope(metadata, role), api_key=key,
                          clear_api_key=key is None)
        return settings(role)

    @app.get("/api/agents")
    def agents():
        with service() as svc:
            return {"agents": [definition.model_dump(mode="json")
                               for definition in svc.agent_registry.catalog(delegatable_only=False)]}

    @app.get("/api/skills")
    def skills(role: str = "ideation"):
        return [item.model_dump() for item in library.for_agent(role).catalog()]

    @app.get("/api/accounts")
    def accounts():
        with service() as svc:
            return [{**account.model_dump(), "job": public_job(repo.latest(
                resource=f"account:{account.platform}:{account.account_id}"))} for account in svc.accounts.store.accounts()]

    @app.post("/api/accounts")
    def add_account(body: AccountInput):
        with service() as svc:
            return svc.accounts.add(body.account, body.label)

    def queue_account(platform, account_id, body, kind):
        ref = AccountRef(platform=platform, account_id=account_id)
        resource = f"account:{ref.platform}:{ref.account_id}"
        payload = {**body.model_dump(mode="json"), "account": ref.model_dump()}
        digest = fingerprint(kind, resource, payload)
        existing = repo.request_job(str(body.request_id), digest)
        if existing:
            return public_job(existing)
        with service() as svc:
            svc.accounts.store.account(ref)
        return runner.submit(request_id=str(body.request_id), fingerprint=digest, resource=resource,
                             task_id=None, kind=kind, params=payload)

    @app.post("/api/accounts/{platform}/{account_id}/login", status_code=202)
    def login(platform: str, account_id: str, body: LoginAction): return queue_account(platform, account_id, body, "login")

    @app.post("/api/accounts/{platform}/{account_id}/whoami", status_code=202)
    def whoami(platform: str, account_id: str, body: LoginAction): return queue_account(platform, account_id, body, "whoami")

    @app.get("/api/platforms")
    def platforms(): return [create_platform_connector("xiaohongshu").capabilities()]

    @app.post("/api/media")
    async def upload(file: UploadFile):
        try:
            data = await file.read(20 * 1024 * 1024 + 1)
        finally:
            await file.close()
        if not data or len(data) > 20 * 1024 * 1024:
            raise ValueError("图片大小须在 20 MB 以内")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            extension, content_type = ".png", "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            extension, content_type = ".jpg", "image/jpeg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            extension, content_type = ".webp", "image/webp"
        else:
            raise ValueError("只支持 JPG、PNG 和 WebP 图片，请检查文件内容")
        identifier = str(uuid.uuid4())
        directory = root / "media"
        directory.mkdir(exist_ok=True)
        path = (directory / (identifier + extension)).resolve()
        if not path.is_relative_to(root):
            raise ValueError("媒体目录无效")
        path.write_bytes(data)
        return repo.add_media(identifier, Path(file.filename or "image").name, path, content_type)

    @app.get("/api/media/{media_id}")
    def get_media(media_id: str):
        item = repo.media(media_id)
        return FileResponse(item["path"], media_type=item["content_type"])

    def job_snapshot(job_id: str):
        current = repo.job(job_id)
        result = {"job": public_job(current), "events": repo.events(job_id)}
        if current["task_id"]:
            with service() as svc:
                task = svc.status(current["task_id"])
                result.update(workflow_state(svc, task, repo.latest(task_id=task.task_id)))
        return result

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return job_snapshot(job_id)

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request):
        repo.job(job_id)

        async def stream():
            last = None
            while not await request.is_disconnected():
                current = await asyncio.to_thread(job_snapshot, job_id)
                snapshot = json.dumps(current, ensure_ascii=False)
                if snapshot != last:
                    yield "data: " + snapshot + "\n\n"
                    last = snapshot
                if current["job"]["status"] not in {"queued", "running"}:
                    break
                yield ": keepalive\n\n"
                await asyncio.sleep(0.7)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    frontend = Path(dist_dir) if dist_dir else Path(__file__).resolve().parents[3] / "web" / "dist"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    else:
        @app.get("/")
        def unbuilt():
            return JSONResponse({"message": "请先在 web 目录执行 npm install 和 npm run build", "api": "/docs"})
    return app
