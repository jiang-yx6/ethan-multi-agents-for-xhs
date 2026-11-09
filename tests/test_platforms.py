"""平台集成与浏览器协议均离线验证，不登录、不上传、不发帖。"""
import base64
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from xhs_agent.cli import main
from xhs_agent.agents.ideation import MockIdeationAgent
from xhs_agent.models import DraftContent, ImageRef
from xhs_agent.platforms import AccountIdentity, AccountRef, PublishReceipt
from xhs_agent.platforms.content import adapt_draft
from xhs_agent.platforms.xiaohongshu import SELECTORS, XiaohongshuConnector, identity_from_payload
from xhs_agent.service import TaskService


NOTE_URL = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"
REF = AccountRef(platform="xiaohongshu", account_id="primary")


class FakeConnector(XiaohongshuConnector):
    def __init__(self, state="submitted"):
        self.calls = 0
        self.state = state
        self.identity = AccountIdentity(state="authenticated", user_id="real-user-1", nickname="路线分享")

    def login(self, profile_dir, *, timeout=180):
        return self.identity

    def whoami(self, profile_dir):
        return self.identity

    def submit(self, request, profile_dir):
        self.calls += 1
        assert request.expected_user_id == "real-user-1"
        return PublishReceipt(state=self.state, message="离线模拟回执")


@pytest.fixture
def setup(tmp_path):
    image = tmp_path / "photo.png"
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRncAAAAASUVORK5CYII="))
    connector = FakeConnector()
    services = []

    def make():
        service = TaskService(tmp_path / "data", ideation_agent=MockIdeationAgent(), connector_factory=lambda name: connector)
        services.append(service)
        return service

    yield SimpleNamespace(image=image, connector=connector, make=make)
    for service in services:
        service.close()


def approved(setup):
    svc = setup.make()
    svc.accounts.add(REF)
    assert svc.accounts.login(REF).state == "authenticated"
    task = svc.create_task("周末路线")
    svc.select_idea(task.task_id, 0)
    task = svc.replace_draft(task.task_id, DraftContent(
        title="周末路线", body="分享这次散步的体验。", images=[ImageRef(description="路线照片", source=str(setup.image))]))
    assert task.current_version == 2
    assert not task.draft.approved
    return svc, svc.approve(task.task_id)


def test_accounts_are_isolated_and_identity_binding_survives_restart(setup):
    svc, task = approved(setup)
    other = AccountRef(platform="xiaohongshu", account_id="second")
    svc.accounts.add(other)
    assert svc.accounts.store.profile_dir(REF) != svc.accounts.store.profile_dir(other)
    restarted = setup.make()
    assert restarted.accounts.store.account(REF).user_id == "real-user-1"
    setup.connector.identity = AccountIdentity(state="unknown", message="模拟网络中断")
    assert restarted.accounts.whoami(REF).state == "unknown"
    assert restarted.accounts.store.account(REF).user_id == "real-user-1"
    setup.connector.identity = AccountIdentity(state="authenticated", user_id="another-person")
    with pytest.raises(ValueError, match="已绑定身份不同"):
        restarted.accounts.whoami(REF)
    assert restarted.accounts.store.account(REF).user_id == "real-user-1"


@pytest.mark.parametrize("account_id", ["../escape", "a/b", "a\\b", ".", "..", "CON", "lpt1"])
def test_account_paths_reject_traversal(account_id):
    with pytest.raises(ValueError):
        AccountRef(platform="xiaohongshu", account_id=account_id)


def test_windows_case_aliases_cannot_create_two_accounts(setup):
    svc = setup.make()
    svc.accounts.add(REF)
    upper = AccountRef(platform="xiaohongshu", account_id="PRIMARY")
    assert upper == REF
    with pytest.raises(ValueError, match="已登记"):
        svc.accounts.add(upper)


def test_target_binding_cannot_be_overwritten_by_other_process(setup):
    svc, task = approved(setup)
    other = setup.make()
    svc.store.bind_publish_target(task.task_id, REF.model_dump())
    with pytest.raises(ValueError, match="其他发布账号"):
        other.store.bind_publish_target(task.task_id, {"platform": "xiaohongshu", "account_id": "second"})
    assert svc.store.get_stage(task.task_id, "publish_target") == REF.model_dump()


def test_real_preflight_rejects_placeholder_and_keeps_approval(setup):
    svc = setup.make()
    svc.accounts.add(REF)
    svc.accounts.login(REF)
    task = svc.create_task("日常")
    svc.select_idea(task.task_id, 0)
    svc.approve(task.task_id)
    with pytest.raises(ValueError, match="真实发布需要本地图片"):
        svc.publish(task.task_id, platform=REF.platform, account_id=REF.account_id)
    assert svc.status(task.task_id).status == "approved"
    assert svc.store.get_stage(task.task_id, "publish_target") is None
    assert setup.connector.calls == 0


def test_changed_media_cannot_be_published_under_old_approval(setup):
    svc, task = approved(setup)
    setup.image.write_bytes(setup.image.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="批准时不同"):
        svc.publish(task.task_id, platform=REF.platform, account_id=REF.account_id)
    assert svc.status(task.task_id).status == "approved"
    assert setup.connector.calls == 0
    task = svc.replace_draft(task.task_id, DraftContent(
        title="新的路线", body="照片已重新检查。", images=task.draft.images))
    assert task.current_version == 3
    assert task.history[-2]["approved"] is False
    assert task.status == "pending_review"
    svc.approve(task.task_id)
    assert svc.publish(task.task_id, platform=REF.platform, account_id=REF.account_id).status == "submitted"


@pytest.mark.parametrize("state", ["submitted", "unknown"])
def test_unverified_submission_is_not_published_and_never_resubmits(setup, state):
    setup.connector.state = state
    svc, task = approved(setup)
    task = svc.publish(task.task_id, platform=REF.platform, account_id=REF.account_id)
    assert task.status == state
    assert not task.published
    assert task.publish_result.job_id
    assert svc.store.publish_count(task.task_id) == 0
    restarted = setup.make()
    job = restarted.accounts.store.jobs(task.task_id)[0]
    assert restarted.platform_publishing.submit(job.request).state == state
    with pytest.raises(ValueError):
        restarted.retry(task.task_id)
    with pytest.raises(ValueError):
        restarted.publish(task.task_id)
    assert setup.connector.calls == 1
    task = restarted.confirm_published(task.task_id, NOTE_URL)
    assert task.status == "published" and task.published
    assert task.publish_result.verification == "manual"
    assert restarted.confirm_published(task.task_id, NOTE_URL).published
    assert restarted.store.publish_count(task.task_id) == 1
    assert setup.connector.calls == 1


def test_crash_after_click_before_receipt_save_does_not_fall_back_to_mock(setup, monkeypatch):
    svc, task = approved(setup)

    def crash(*args):
        raise RuntimeError("模拟回执写入前崩溃")

    monkeypatch.setattr(svc.accounts.store, "finish", crash)
    with pytest.raises(RuntimeError, match="模拟回执"):
        svc.publish(task.task_id, platform=REF.platform, account_id=REF.account_id)
    assert setup.connector.calls == 1
    assert svc.status(task.task_id).status == "failed"
    restarted = setup.make()
    task = restarted.retry(task.task_id)
    assert task.status == "unknown" and not task.published
    assert setup.connector.calls == 1
    assert restarted.store.publish_count(task.task_id) == 0
    task = restarted.confirm_published(task.task_id, NOTE_URL)
    assert task.publish_result.verification == "manual"
    assert task.publish_result.url == NOTE_URL


def test_known_precommit_failure_returns_to_publish_gate_and_reuses_target(setup):
    setup.connector.state = "failed"
    svc, task = approved(setup)
    task = svc.publish(task.task_id, platform=REF.platform, account_id=REF.account_id)
    assert task.status == "approved"
    assert task.publish_result.status == "failed"
    with pytest.raises(ValueError, match="其他发布账号"):
        svc.publish(task.task_id, platform=REF.platform, account_id="another")
    with pytest.raises(ValueError):
        svc.confirm_published(task.task_id, NOTE_URL)
    setup.connector.state = "submitted"
    restarted = setup.make()
    task = restarted.publish(task.task_id)  # 重启后沿用绑定账号，不能回到 Mock。
    assert task.status == "submitted" and not task.published
    assert setup.connector.calls == 2
    assert len(restarted.accounts.store.jobs(task.task_id)) == 1


def test_atomic_job_claim_allows_one_owner(setup):
    svc, task = approved(setup)
    request = svc.platform_publishing.prepare(task.task_id, REF)
    job = svc.accounts.store.ensure_job(request)
    other = setup.make()
    assert svc.accounts.store.claim(job.job_id)
    assert not other.accounts.store.claim(job.job_id)
    assert other.platform_publishing.submit(request).state == "submitting"
    assert setup.connector.calls == 0


def test_direct_submit_cannot_bypass_approval_or_change_content(setup):
    svc, task = approved(setup)
    request = svc.platform_publishing.prepare(task.task_id, REF)
    altered = request.model_copy(deep=True)
    altered.content.body = "未经审核的正文"
    with pytest.raises(ValueError, match="批准内容不一致"):
        svc.platform_publishing.submit(altered)
    assert not svc.accounts.store.jobs(task.task_id)
    svc.replace_draft(task.task_id, DraftContent(title="下一版", body="尚未批准", images=task.draft.images))
    with pytest.raises(ValueError, match="人工批准"):
        svc.platform_publishing.submit(request)
    assert setup.connector.calls == 0


def test_recovery_of_unverified_job_does_not_require_original_media(setup, monkeypatch):
    svc, task = approved(setup)

    def crash(*args):
        raise RuntimeError("回执落盘失败")

    monkeypatch.setattr(svc.accounts.store, "finish", crash)
    with pytest.raises(RuntimeError):
        svc.publish(task.task_id, platform=REF.platform, account_id=REF.account_id)
    setup.image.unlink()
    restarted = setup.make()
    assert restarted.retry(task.task_id).status == "unknown"
    assert restarted.confirm_published(task.task_id, NOTE_URL).published
    assert setup.connector.calls == 1


def test_hard_exit_leaves_submitting_and_recovery_does_not_submit_twice(setup, monkeypatch):
    svc, task = approved(setup)

    def stop(*args):
        raise SystemExit("模拟进程退出")

    monkeypatch.setattr(svc.accounts.store, "finish", stop)
    with pytest.raises(SystemExit):
        svc.publish(task.task_id, platform=REF.platform, account_id=REF.account_id)
    assert svc.status(task.task_id).status == "submitting"
    with pytest.raises(ValueError):
        svc.replace_draft(task.task_id, DraftContent(title="不能修改", body="提交还未核实"))
    restarted = setup.make()
    assert restarted.retry(task.task_id).status == "unknown"
    assert setup.connector.calls == 1


def test_platform_cannot_claim_publication_without_verification(setup):
    setup.connector.state = "published"
    svc, task = approved(setup)
    task = svc.publish(task.task_id, platform=REF.platform, account_id=REF.account_id)
    assert task.status == "unknown" and not task.published


def test_cli_replace_draft_uses_json_relative_images_and_needs_reapproval(setup, tmp_path):
    svc, task = approved(setup)
    replacement = tmp_path / "note.json"
    replacement.write_text('{"title":"新的标题","body":"手动写好的正文","images":[{"description":"实拍","source":"photo.png"}]}', encoding="utf-8")
    data_dir = str(svc.data_dir)
    assert main(["--data-dir", data_dir, "replace-draft", task.task_id, "--file", str(replacement)]) == 0
    updated = svc.status(task.task_id)
    assert updated.status == "pending_review"
    assert updated.draft.images[0].source == str(setup.image.resolve())
    assert updated.current_version == 3
    assert main(["--data-dir", data_dir, "accounts", "list"]) == 0
    assert main(["--data-dir", data_dir, "platforms"]) == 0
    assert main(["--data-dir", data_dir, "preflight", task.task_id,
                 "--platform", REF.platform, "--account", REF.account_id]) == 0


@pytest.mark.parametrize("url", ["https://example.com/explore/0123456789abcdef01234567",
                                "https://www.xiaohongshu.com/", "http://www.xiaohongshu.com/explore/0123456789abcdef01234567"])
def test_confirmation_requires_a_note_url(setup, url):
    svc, task = approved(setup)
    svc.publish(task.task_id, platform=REF.platform, account_id=REF.account_id)
    with pytest.raises(ValueError):
        svc.confirm_published(task.task_id, url)
    assert not svc.status(task.task_id).published


class FakeElement:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    def filter(self, **kwargs):
        return self

    def nth(self, index):
        return self

    def wait_for(self, **kwargs):
        pass

    def all(self):
        return [self] if self.selector in {SELECTORS["tab"], SELECTORS["button"], SELECTORS["toast"]} else []

    def is_visible(self):
        return True

    def is_enabled(self, **kwargs):
        return True

    def get_attribute(self, name, **kwargs):
        return None

    def scroll_into_view_if_needed(self, **kwargs):
        pass

    def bounding_box(self, **kwargs):
        return {"x": 80, "y": 80, "width": 100, "height": 40}

    def inner_text(self):
        return "发布成功"

    def click(self, *, trial=False, **kwargs):
        if trial:
            return
        if self.selector == SELECTORS["button"]:
            self.page.clicks += 1
            if self.page.click_error:
                raise TimeoutError("按钮点击超时，是否生效未知")

    def fill(self, value):
        self.page.fills[self.selector] = value

    def set_input_files(self, payload):
        self.page.uploads.append(payload)


class FakePage:
    viewport_size = {"width": 1440, "height": 1000}
    def __init__(self):
        self.url = ""
        self.clicks = 0
        self.click_error = False
        self.fills = {}
        self.uploads = []

    def goto(self, url, **kwargs):
        self.url = url

    def locator(self, selector):
        return FakeElement(self, selector)

    def wait_for_timeout(self, duration):
        pass


def browser_connector(page, *, user_id="real-user-1", close_error=False):
    @contextmanager
    def session(profile_dir):
        def get(url, **kwargs):
            return SimpleNamespace(status=200, json=lambda: {"success": True, "data": {"userId": user_id}})
        yield SimpleNamespace(request=SimpleNamespace(get=get)), page
        if close_error:
            raise RuntimeError("关闭浏览器时失败")
    return XiaohongshuConnector(session_factory=session)


def test_browser_fills_reviewed_content_uploads_immutable_bytes_and_only_submits(setup):
    svc, task = approved(setup)
    page = FakePage()
    connector = browser_connector(page, close_error=True)
    receipt = connector.submit(svc.platform_publishing.prepare(task.task_id, REF), Path("unused"))
    assert receipt.state == "submitted"
    assert receipt.verification == "none"
    assert page.clicks == 1
    assert page.fills[SELECTORS["body"]] == task.draft.body
    assert page.uploads[0]["buffer"] == setup.image.read_bytes()


def test_click_timeout_is_unknown_and_does_not_click_again(setup):
    svc, task = approved(setup)
    page = FakePage()
    page.click_error = True
    receipt = browser_connector(page).submit(svc.platform_publishing.prepare(task.task_id, REF), Path("unused"))
    assert receipt.state == "unknown"
    assert page.clicks == 1


def test_browser_checks_account_and_media_before_click(setup):
    svc, task = approved(setup)
    request = svc.platform_publishing.prepare(task.task_id, REF)
    page = FakePage()
    receipt = browser_connector(page, user_id="wrong-user").submit(request, Path("unused"))
    assert receipt.state == "failed" and page.clicks == 0
    setup.image.write_bytes(setup.image.read_bytes() + b"new bytes")
    receipt = browser_connector(page).submit(request, Path("unused"))
    assert receipt.state == "failed" and page.clicks == 0
    assert not page.uploads


def test_identity_and_capability_failures_are_explicit(setup):
    assert identity_from_payload({"success": True, "data": {"nickname": "匿名"}}).state == "unknown"
    assert identity_from_payload(None, 403).state == "unknown"
    assert identity_from_payload(None, 401).state == "logged_out"
    connector = XiaohongshuConnector()
    svc, task = approved(setup)
    content = adapt_draft(task.draft)
    assert connector.validate(content).valid
    content.title = "超长标题" * 10
    content.tags = ["有 空格"]
    assert not connector.validate(content).valid
    assert not connector.capabilities().automatic_verification
    with pytest.raises(ValueError, match="草稿箱"):
        connector.create_draft(None, Path("unused"))
    with pytest.raises(ValueError, match="自动回查"):
        connector.reconcile(None, Path("unused"))
