"""工作流发布适配器：绑定账号/审核版本，并持久保存每一次提交作业。"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from ..agents.base import SkillEnabledAgent
from ..models import Draft, PublishResult
from ..storage import Store
from .accounts import AccountService
from .content import adapt_draft, draft_fingerprint, file_hash
from .models import AccountRef, PublishJob, PublishReceipt, PublishRequest, ValidationReport


class PlatformPublishingService:
    def __init__(self, tasks: Store, accounts: AccountService):
        self.tasks = tasks
        self.accounts = accounts
        self.store = accounts.store

    def prepare(self, task_id: str, ref: AccountRef, *, require_approved: bool = True) -> PublishRequest:
        task = self.tasks.get_task(task_id)
        if not task or not task.draft:
            raise ValueError("任务尚无草稿")
        draft = task.draft
        if require_approved and not draft.approved:
            raise ValueError("真实发布需要人工批准当前草稿")
        account = self.store.account(ref)
        if not account.user_id:
            raise ValueError("账号尚未绑定登录身份，请先执行 accounts login 或 whoami")
        content = adapt_draft(draft)
        connector = self.accounts.connector(ref)
        if not connector.capabilities().image_publish:
            raise ValueError("连接器不支持图文发布")
        connector.validate(content).require_valid()
        fingerprint = draft_fingerprint(draft)
        if require_approved and self.tasks.get_stage(task_id, "approval_fingerprint", draft.version) != fingerprint:
            raise ValueError("图片或内容与批准时不同，请替换草稿并重新审核")
        return PublishRequest(task_id=task_id, draft_version=draft.version, account=ref,
                              expected_user_id=account.user_id, content=content, content_hash=fingerprint)

    def preflight(self, task_id: str, ref: AccountRef) -> ValidationReport:
        request = self.prepare(task_id, ref, require_approved=False)
        return self.accounts.connector(ref).validate(request.content)

    def submit(self, request: PublishRequest) -> PublishJob:
        existing = self.store.find_job(request.task_id, request.draft_version, request.account)
        if existing:
            if existing.request != request:
                raise ValueError("发布作业已绑定其他内容或账号身份，不能覆盖")
            if existing.state not in {"ready", "failed"}:
                return existing
        # 直接调用连接层也必须绑定当前批准内容，不能绕过工作流的审核边界。
        if self.prepare(request.task_id, request.account) != request:
            raise ValueError("发布请求与当前批准内容不一致")
        job = self.store.ensure_job(request)
        if not self.store.claim(job.job_id):
            # 包含崩溃遗留的 submitting。绝不自动把它重置为可提交状态。
            return self.store.job(job.job_id)
        try:
            # 文件在 preflight 和点击之间发生变化时阻止上传。已提交作业重放不读媒体。
            if any(file_hash(Path(a.path)) != a.sha256 for a in request.content.images):
                receipt = PublishReceipt(state="failed", message="图片在提交前发生变化，请重新审核")
            else:
                receipt = self.accounts.connector(request.account).submit(
                    request, self.store.profile_dir(request.account))
            if receipt.state == "published" and (receipt.verification != "platform" or not receipt.platform_post_id):
                receipt = PublishReceipt(state="unknown", message="连接器未提供可核实的作品回执")
        except Exception as exc:
            # 连接器逸出异常无法保证尚未点击；保守记录，留待人工核实。
            receipt = PublishReceipt(state="unknown", message="连接器异常，提交结果待核实：" + str(exc))
        self.store.finish(job.job_id, receipt)
        return self.store.job(job.job_id)

    def confirm_published(self, task_id: str, note_url: str) -> PublishReceipt:
        """调用者明确人工确认；该操作不被描述为平台自动验证。"""
        task = self.tasks.get_task(task_id)
        jobs = self.store.jobs(task_id)
        if not task or not task.draft or not task.draft.approved:
            raise ValueError("没有已批准的待确认草稿")
        jobs = [job for job in jobs if job.request.draft_version == task.current_version]
        if len(jobs) != 1:
            raise ValueError("没有唯一的当前版本平台发布作业")
        job = jobs[0]
        if job.request.account.platform != "xiaohongshu":
            raise ValueError("该平台暂不支持人工回执登记")
        parsed = urlsplit(note_url)
        match = re.fullmatch(r"/(?:explore|discovery/item)/([a-fA-F0-9]{24})/?", parsed.path)
        if (parsed.scheme != "https" or parsed.netloc != "www.xiaohongshu.com" or not match
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError("请提供真实笔记链接：https://www.xiaohongshu.com/explore/<24位笔记ID>")
        canonical_url = f"https://www.xiaohongshu.com/explore/{match[1]}"
        if job.state == "published":
            if not job.receipt or job.receipt.url != canonical_url:
                raise ValueError("作业已绑定其他发布链接")
            receipt = job.receipt
        else:
            if task.status not in {"submitted", "unknown", "failed"} or job.state not in {"submitted", "unknown", "submitting"}:
                raise ValueError("该作业尚不需要人工确认发布结果")
            receipt = PublishReceipt(state="published", message="用户已在平台后台人工核实发布结果",
                                     platform_post_id=match[1], url=canonical_url, verification="manual")
            self.store.finish(job.job_id, receipt)
            persisted = self.store.job(job.job_id).receipt
            if not persisted or persisted.url != canonical_url:
                raise ValueError("作业已由其他操作绑定不同发布回执")
            receipt = persisted
        self.tasks.record_publish(task_id, task.draft)
        self.tasks.save_stage(task_id, "platform_receipt", receipt.model_dump(mode="json"), task.current_version)
        result = PublishResult(task_id=task_id, draft_version=task.current_version,
                               published=True, status="published", reason=receipt.message,
                               platform=job.request.account.platform, account_id=job.request.account.account_id,
                               job_id=job.job_id, platform_post_id=receipt.platform_post_id,
                               url=receipt.url, verification=receipt.verification)
        self.tasks.save_stage(task_id, "publish", result.model_dump(mode="json"), task.current_version)
        self.tasks.clear_error(task_id)
        return receipt


class PlatformPublisher(SkillEnabledAgent):
    def __init__(self, service: PlatformPublishingService, account: AccountRef):
        self.service = service
        self.account = account

    def publish(self, task_id: str, draft: Draft) -> PublishResult:
        job = self.service.store.find_job(task_id, draft.version, self.account)
        if job is None or job.state in {"ready", "failed"}:
            request = self.service.prepare(task_id, self.account)
            if draft.version != request.draft_version:
                raise ValueError("待发布版本已变化")
            job = self.service.submit(request)
        receipt = job.receipt or PublishReceipt(state="unknown", message="上次提交尚未得到回执，请先核实平台后台")
        return PublishResult(task_id=task_id, draft_version=draft.version,
                             published=receipt.state == "published", reason=receipt.message,
                             status=receipt.state, platform=self.account.platform,
                             account_id=self.account.account_id, job_id=job.job_id,
                             platform_post_id=receipt.platform_post_id, url=receipt.url,
                             verification=receipt.verification)
