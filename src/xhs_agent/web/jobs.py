"""持久化作业 + 有界执行器；每个任务同时只允许一个写作业。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from ..models import DraftContent, ImageRef
from ..platforms import AccountRef
from ..workflow import TERMINAL, allowed_actions
from .models import DraftAction
from .repository import ConflictError, WebRepository


def public_job(job: dict | None) -> dict | None:
    if job is None:
        return None
    return {key: job[key] for key in ("job_id", "task_id", "kind", "status", "result", "error", "created_at", "updated_at")}


class JobRunner:
    def __init__(self, repository: WebRepository, service_factory: Callable):
        self.repo = repository
        self.service_factory = service_factory
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="xhs-web")
        self.closed = False

    def recover(self):
        interrupted = self.repo.recover()
        if not interrupted:
            return
        service = self.service_factory(None)
        try:
            for job in interrupted:
                if job["task_id"]:
                    task = service.store.get_task(job["task_id"])
                    if task and service.reconcile_task(task.task_id):
                        self.repo.event(job["job_id"], {"kind": "reconciled", "message": "已从检查点恢复人工待办，未自动执行原操作"})
                    elif task and task.workflow_version == 2 and task.status not in TERMINAL:
                        service.store.mark_failed(task.task_id, "Web 服务中断，请手动重试恢复检查点")
                self.repo.event(job["job_id"], {"kind": "interrupted", "message": "服务中断，未自动重新执行"})
        finally:
            service.close()

    def close(self):
        self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=True)

    def submit(self, **kwargs) -> dict:
        if self.closed:
            raise ConflictError("服务正在关闭，请稍后重试")
        job, created = self.repo.enqueue(**kwargs)
        if created:
            self.repo.event(job["job_id"], {"kind": "queued", "message": "任务已加入执行队列"})
            self.executor.submit(self._run, job["job_id"])
        return public_job(job)

    def _run(self, job_id: str):
        if not self.repo.start(job_id):
            return
        service = None
        try:
            job = self.repo.job(job_id)
            self.repo.event(job_id, {"kind": "started", "message": "开始执行"})
            service = self.service_factory(lambda event: self.repo.event(job_id, event))
            params, kind, task_id = job["params"], job["kind"], job["task_id"]
            if task_id and "expected_version" in params:
                task = service.status(task_id)
                if task.current_version != params["expected_version"]:
                    raise ConflictError("内容版本已经变化，请刷新后再操作")
                if kind not in allowed_actions(task, service.workflow(task_id)):
                    raise ConflictError("图状态已经变化，请刷新后再操作")
            resume = {"interrupt_id": params["interrupt_id"]} if "interrupt_id" in params else {}
            if kind == "create":
                result = service.start_task(task_id)
            elif kind == "draft":
                data = DraftAction.model_validate(params)
                current = service.status(task_id)
                images = []
                for ref in data.images:
                    if ref.media_id is not None:
                        source = self.repo.media(str(ref.media_id))["path"]
                    elif current.draft and ref.existing_index < len(current.draft.images):
                        source = current.draft.images[ref.existing_index].source
                    else:
                        raise ValueError("原版本图片引用已失效")
                    images.append(ImageRef(description=ref.description, source=source))
                result = service.replace_draft(task_id, DraftContent(title=data.title, body=data.body,
                    tags=data.tags, images=images, content_type=data.content_type), **resume)
            elif kind == "changes":
                result = service.request_changes(task_id, params["comment"], **resume)
            elif kind == "answer":
                result = service.answer(task_id, params["answer"], **resume)
            elif kind == "message":
                result = service.message(task_id, params["text"], **resume)
            elif kind == "publish":
                ref = AccountRef.model_validate(params["account"])
                result = service.publish(task_id, platform=ref.platform, account_id=ref.account_id, **resume)
            elif kind == "confirm":
                result = service.confirm_published(task_id, params["note_url"])
            elif kind in {"approve", "cancel"}:
                result = getattr(service, kind)(task_id, **resume)
            elif kind == "retry":
                result = service.retry(task_id)
            elif kind == "reconcile":
                if not service.reconcile_task(task_id):
                    raise ConflictError("当前检查点无法恢复人工待办，请检查任务状态")
                self.repo.event(job_id, {"kind": "reconciled", "message": "人工待办已恢复，未执行图节点"})
                result = service.status(task_id)
            elif kind in {"login", "whoami"}:
                ref = AccountRef.model_validate(params["account"])
                identity = (service.accounts.login(ref, timeout=params.get("timeout", 180)) if kind == "login"
                            else service.accounts.whoami(ref))
                if identity.state != "authenticated":
                    raise ValueError(identity.message or "登录身份尚未确认")
                self.repo.event(job_id, {"kind": "finished", "message": "账号身份校验完成"})
                self.repo.finish(job_id, result=identity.model_dump(mode="json"))
                return
            else:
                raise ValueError("未知后台作业")
            self.repo.event(job_id, {"kind": "finished", "message": "本次操作已完成", "status": result.status})
            self.repo.finish(job_id, result={"task_id": result.task_id, "status": result.status,
                                             "current_version": result.current_version,
                                             "workflow": service.workflow(task_id).model_dump(mode="json")})
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ValueError, RuntimeError, KeyError, OSError)) else "执行失败，请查看服务日志并重试"
            self.repo.event(job_id, {"kind": "failed", "message": message})
            self.repo.finish(job_id, error=message)
        finally:
            if service is not None:
                service.close()
