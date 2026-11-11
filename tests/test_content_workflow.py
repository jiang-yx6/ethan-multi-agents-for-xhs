import json
import sqlite3

import pytest

from xhs_agent.agents.creation import MockCreationAgent
from xhs_agent.agents.ideation import MockIdeationAgent
from xhs_agent.agents.research.agent import MockResearchAgent
from xhs_agent.agents.research.model import RetrievedDocument, SearchHit
from xhs_agent.config import AgentConfig
from xhs_agent.models import (
    ContentRequest, Draft, PublishResult, TrendItem, UserProfile,
)
from xhs_agent.service import TaskService
from xhs_agent.storage import Store


def test_cli_create_review_export_and_modify(tmp_path, capsys):
    from xhs_agent.cli import main

    profile_path = tmp_path / "profile.json"
    profile_path.write_text(UserProfile(name="路线账号", style="轻松").model_dump_json(), encoding="utf-8")
    base = ["--data-dir", str(tmp_path / "data"), "--mock-ideation", "--mock-supervisor"]
    assert main([*base, "create", "城市漫步", "--goal", "分享路线", "--profile", str(profile_path),
                 "--content-type", "攻略", "--material", "路过江边", "--no-research", "--no-trends"]) == 0
    output = capsys.readouterr().out
    import re
    task_id = re.search(r"xhs-[0-9a-f]+", output).group()
    assert main([*base, "approve", task_id]) == 0
    assert main([*base, "export", task_id]) == 0
    svc = TaskService(tmp_path / "data", ideation_agent=MockIdeationAgent(), config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    assert not svc.status(task_id).published
    svc.close()
    assert main([*base, "modify", task_id, "-c", "增加交通信息"]) == 0
    assert main([*base, "export", task_id]) == 1


class RecordingCreation(MockCreationAgent):
    def __init__(self):
        self.previous = []

    def create(self, brief, feedback=None, *, request, profile, previous_draft=None):
        self.previous.append(previous_draft)
        assert profile.name == "测试账号"
        assert "不写价格" in request.constraints
        return super().create(brief, feedback, request=request, profile=profile,
                              previous_draft=previous_draft)


def test_creation_receives_previous_draft_and_full_context(data_dir):
    creator = RecordingCreation()
    svc = TaskService(data_dir, ideation_agent=MockIdeationAgent(), creation_agent=creator, config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    task = svc.create_task(ContentRequest(goal="写一篇攻略", topic="攻略", constraints=["不写价格"]),
                           profile=UserProfile(name="测试账号"))
    first = svc.status(task.task_id)
    svc.request_changes(task.task_id, "补充路线")
    assert creator.previous[0] is None
    assert creator.previous[1].body == first.draft.body
    assert creator.previous[1].version == 1
    svc.close()


def test_context_snapshot_and_version_archive_survive_restart(data_dir):
    profile = UserProfile(name="周末玩家", positioning="城市旅行", audience="上班族", style="轻松口语")
    request = ContentRequest(goal="分享周末路线", topic="城市漫步", content_type="攻略",
                             supplied_materials=["周六走过江边步道"], needs_research=False)
    svc = TaskService(data_dir, ideation_agent=MockIdeationAgent(), config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    task = svc.create_task(request, profile)
    profile.style = "修改后的风格"
    request.supplied_materials.clear()
    svc.close()

    svc = TaskService(data_dir, ideation_agent=MockIdeationAgent(), config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    task = svc.status(task.task_id)
    assert task.profile.style == "轻松口语"
    assert task.request.supplied_materials == ["周六走过江边步道"]
    assert task.draft.content_type == "攻略"
    assert "周六走过江边步道" in task.draft.body
    assert task.brief is None
    svc.approve(task.task_id)
    destination = svc.export(task.task_id)
    assert json.loads((destination / "profile.json").read_text(encoding="utf-8"))["style"] == "轻松口语"
    assert (destination / "note.md").is_file()
    assert not svc.status(task.task_id).published
    revised = svc.request_changes(task.task_id, "保留路线，补充交通方式")
    assert revised.current_version == 2
    assert task.draft.body in revised.draft.body
    assert revised.draft.revision_notes[-1].endswith("保留路线，补充交通方式")
    assert not any(h["approved"] for h in revised.history)
    assert (data_dir / "outputs" / task.task_id / "v001" / "note.md").read_text(encoding="utf-8") == task.draft.body
    assert (data_dir / "outputs" / task.task_id / "v002" / "note.md").is_file()
    with pytest.raises(ValueError):
        svc.export(task.task_id)
    svc.close()


class CountingTrends:
    def __init__(self):
        self.calls = 0

    def collect(self, request, profile):
        self.calls += 1
        return [TrendItem(title="城市漫步话题", url="https://example.org/walk", keywords=["城市漫步"]),
                TrendItem(title="无关体育事件", url="https://example.org/sport")]


def test_trends_are_optional_and_related(data_dir):
    provider = CountingTrends()
    svc = TaskService(data_dir, ideation_agent=MockIdeationAgent(), trend_provider=provider, config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    task = svc.create_task(ContentRequest(goal="城市漫步"))
    ideas = svc.candidate_ideas(task.task_id)
    assert provider.calls == 1
    assert ideas[0].trend_urls == ["https://example.org/walk"]
    assert all("https://example.org/sport" not in idea.trend_urls for idea in ideas)
    no_trends = svc.create_task(ContentRequest(goal="记录生活", use_trends=False))
    assert provider.calls == 1
    assert svc.status(no_trends.task_id).trends == []
    assert not any(i.trend_urls for i in svc.candidate_ideas(no_trends.task_id))
    svc.close()


def test_explicit_trends_do_not_call_provider(data_dir):
    provider = CountingTrends()
    svc = TaskService(data_dir, ideation_agent=MockIdeationAgent(), trend_provider=provider, config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    task = svc.create_task(ContentRequest(goal="咖啡"), trends=[TrendItem(title="咖啡话题", url="https://example.org/coffee")])
    assert provider.calls == 0
    assert svc.candidate_ideas(task.task_id)[0].trend_urls == ["https://example.org/coffee"]
    svc.close()


class Search:
    def __init__(self):
        self.calls = 0

    def search(self, query, limit=5):
        self.calls += 1
        return [SearchHit(title="资料", url="https://example.org/source", snippet="摘要")]

    def fetch(self, url):
        return RetrievedDocument(title="原始材料", url=url, text="这段正文必须传给创作环节。",
                                 retrieved_at="2026-11-03T10:00:00+08:00")


def test_research_retains_source_text_and_can_skip_search(data_dir):
    search = Search()
    svc = TaskService(data_dir, ideation_agent=MockIdeationAgent(), research_agent=MockResearchAgent(search), config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    task = svc.create_task("路线")
    task = svc.status(task.task_id)
    assert task.brief.evidence[0].excerpt == "这段正文必须传给创作环节。"
    assert not task.brief.evidence[0].is_mock
    assert len(task.brief.sources) == 1
    assert "这段正文必须传给创作环节。" in task.draft.body
    calls = search.calls
    other = svc.create_task(ContentRequest(goal="整理照片", needs_research=False))
    svc.answer(other.task_id, "0")
    assert search.calls == calls
    svc.close()


class FlakyPublisher:
    def __init__(self):
        self.calls = 0

    def publish(self, task_id, draft):
        self.calls += 1
        return PublishResult(task_id=task_id, draft_version=draft.version,
                             published=self.calls > 1, reason="temporary_failure")


def test_failed_publish_is_not_success_and_can_retry_after_restart(data_dir):
    publisher = FlakyPublisher()
    svc = TaskService(data_dir, ideation_agent=MockIdeationAgent(), publisher=publisher, config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    task = svc.create_task("旅行")
    svc.status(task.task_id)
    svc.approve(task.task_id)
    with pytest.raises(RuntimeError, match="发布失败"):
        svc.publish(task.task_id)
    failed = svc.status(task.task_id)
    assert failed.status == "failed"
    assert not failed.published
    assert failed.last_error
    svc.close()
    svc = TaskService(data_dir, ideation_agent=MockIdeationAgent(), publisher=publisher, config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    task = svc.retry(task.task_id)
    assert task.published
    assert task.status == "published"
    assert task.last_error is None
    assert svc.store.publish_count(task.task_id) == 1
    svc.close()


class FailOnceResearch(MockResearchAgent):
    def __init__(self):
        super().__init__()
        self.failed = False

    def research(self, request):
        if not self.failed:
            self.failed = True
            raise RuntimeError("暂时不可用")
        return super().research(request)


def test_failed_research_delegation_resumes(data_dir):
    agent = FailOnceResearch()
    svc = TaskService(data_dir, ideation_agent=MockIdeationAgent(), research_agent=agent, config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    task = svc.register_task("早餐")
    with pytest.raises(RuntimeError):
        svc.start_task(task.task_id)
    svc.close()
    svc = TaskService(data_dir, ideation_agent=MockIdeationAgent(), research_agent=agent, config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    task = svc.retry(task.task_id)
    assert task.status == "pending_review"
    assert task.current_version == 1
    assert task.topic == "早餐"
    svc.close()


def test_draft_commit_before_checkpoint_is_replayed_without_new_version(data_dir, monkeypatch):
    svc = TaskService(data_dir, ideation_agent=MockIdeationAgent(), config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    save = svc.store.save_draft
    raised = False

    def crash_after_save(task_id, draft):
        nonlocal raised
        save(task_id, draft)
        if not raised:
            raised = True
            raise RuntimeError("模拟业务提交后中断")

    monkeypatch.setattr(svc.store, "save_draft", crash_after_save)
    task = svc.register_task("日常记录")
    with pytest.raises(RuntimeError):
        svc.start_task(task.task_id)
    task = svc.retry(task.task_id)
    assert task.current_version == 1
    assert len(task.history) == 1
    assert task.status == "pending_review"
    svc.close()


def test_legacy_database_migrates_without_losing_approval(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tasks (task_id TEXT PRIMARY KEY, topic TEXT NOT NULL, status TEXT NOT NULL,
            current_version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE drafts (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, version INTEGER NOT NULL,
            title TEXT NOT NULL, body TEXT NOT NULL, images TEXT NOT NULL, approved INTEGER NOT NULL,
            approved_at TEXT, created_at TEXT NOT NULL, UNIQUE(task_id, version));
        INSERT INTO tasks VALUES ('legacy', '旧选题', 'approved', 1, '2026-01-01', '2026-01-01');
        INSERT INTO drafts VALUES (1, 'legacy', 1, '旧标题', '旧正文', '[]', 1,
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
    """)
    conn.close()
    for _ in range(2):
        store = Store(path)
        task = store.get_task("legacy")
        assert task.draft.body == "旧正文"
        assert task.draft.approved
        assert task.request.topic == "旧选题"
        store.close()


def test_draft_collision_does_not_invalidate_existing_approval(data_dir):
    svc = TaskService(data_dir, ideation_agent=MockIdeationAgent(), config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))
    task = svc.create_task("分享")
    task = svc.status(task.task_id)
    svc.approve(task.task_id)
    with pytest.raises(ValueError):
        svc.store.save_draft(task.task_id, Draft(version=1, title="冲突标题", body="不同正文"))
    assert svc.status(task.task_id).draft.approved
    svc.close()


@pytest.mark.parametrize("goal", ["", "  "])
def test_empty_request_is_rejected(goal):
    with pytest.raises(ValueError):
        ContentRequest(goal=goal)
