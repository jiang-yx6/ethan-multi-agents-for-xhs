"""第二阶段：真实工具协议、模型自主工具循环、反馈与上下文边界；不访问外网。"""
import base64
import json
import threading
from pathlib import Path

import httpx
import pytest

from xhs_agent.agents.images import DashScopeImageProvider, OpenAIImageProvider
from xhs_agent.agents.review import RuleReviewAgent
from xhs_agent.agents.structured import LLMCreationAgent, LLMResearchAgent, LLMReviewAgent
from xhs_agent.agents.research.search import FetchArgs, ResearchTools, SearchArgs, TavilySearchProvider
from xhs_agent.agents.research.model import RetrievedDocument, SearchHit
from xhs_agent.config import AgentConfig
from xhs_agent.execution import ExecutionConfig, ImageSettings, RoleExecution, SearchSettings, load_execution_config
from xhs_agent.llm import ChatMessage, ChatResponse, LLMConfig, LLMResponseError, ToolCall, create_llm_provider
from xhs_agent.models import ContentRequest, Draft, ImageRef, ResearchBrief, ResearchRequest, ReviewIssue, UserProfile
from xhs_agent.service import TaskService
from xhs_agent.skills import SkillLibrary

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lWQAAAAASUVORK5CYII=")
URL = "https://example.org/original"


def tool(name, identifier, **arguments):
    return ChatMessage(role="assistant", tool_calls=[ToolCall(id=identifier, name=name, arguments=arguments)])


def tools(*calls):
    return ChatMessage(role="assistant", tool_calls=list(calls))


def output(value):
    return ChatMessage(role="assistant", content=json.dumps(value, ensure_ascii=False))


class ScriptedModel:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def chat(self, request):
        self.requests.append(request.model_copy(deep=True))
        result = self.responses.pop(0)
        message = result(request) if callable(result) else result
        return ChatResponse(provider="offline", model="test", message=message,
                            finish_reason="tool_calls" if message.tool_calls else "stop")


def bind(agent, role):
    agent.bind_skills(SkillLibrary().for_agent(role))
    return agent


def brief(**updates):
    return {"topic": "散步路线", "target_audience": "上班族", "pain_points": [], "angles": [],
            "keywords": [], "tags": [], **updates}


def test_research_autonomously_recovers_tool_error_and_repairs_fabricated_evidence():
    calls, events = [], []

    def handle(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        assert request.headers["Authorization"] == "Bearer test-search-key"
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [
                {"title": "不可读取的网页", "url": "https://example.org/failed", "content": "搜索摘要"},
                {"title": "原始路线公告", "url": URL, "content": "开放时段"}]})
        if payload["urls"] == ["https://example.org/failed"]:
            return httpx.Response(503)
        return httpx.Response(200, json={"results": [{"url": URL, "raw_content": "步道每日七点开放，雨天关闭。"}]})

    provider = TavilySearchProvider(SearchSettings(api_key="test-search-key"), transport=httpx.MockTransport(handle))
    valid = brief(sources=[{"title": "模型不能改写来源元信息", "url": URL, "retrieved_at": "invented"}],
                  evidence=[{"source_url": URL, "excerpt": "步道每日七点开放"}])
    invalid = {**valid, "evidence": [{"source_url": URL, "excerpt": "全天免费开放"}]}
    model = ScriptedModel(tool("web_search", "search", query="步道开放公告"),
                          tools(ToolCall(id="bad", name="web_fetch", arguments={"url": "https://example.org/failed"}),
                                ToolCall(id="good", name="web_fetch", arguments={"url": URL})),
                          output(invalid), output(valid))
    agent = bind(LLMResearchAgent(provider=model, search_provider=provider, on_event=events.append), "research")
    result = agent.research(ResearchRequest(topic="散步路线"))
    assert result.sources[0].title == "原始路线公告" and result.sources[0].retrieved_at != "invented"
    assert result.evidence[0].excerpt == "步道每日七点开放" and not result.evidence[0].is_mock
    bad_reply = next(message for message in model.requests[2].messages if message.tool_call_id == "bad")
    assert bad_reply.is_error
    assert "摘录必须存在" in model.requests[3].messages[-1].content
    assert [call[0] for call in calls] == ["/search", "/extract", "/extract"]
    assert any(event["kind"] == "output_validation" for event in events)
    assert all("test-search-key" not in json.dumps(event) for event in events)


def test_research_runs_one_parallel_search_batch_and_one_parallel_fetch_batch():
    search_barrier = threading.Barrier(2)
    fetch_barrier = threading.Barrier(2)
    urls = {"香山": "https://example.org/xiangshan", "坡峰岭": "https://example.org/pofengling"}

    class Search:
        def search(self, query, limit):
            search_barrier.wait(timeout=2)
            return [SearchHit(title=query, url=urls[query], snippet=query + "摘要")]

        def fetch(self, url):
            fetch_barrier.wait(timeout=2)
            name = next(name for name, value in urls.items() if value == url)
            return RetrievedDocument(title=name, url=url, text=name + "十月下旬进入观赏期。",
                                     retrieved_at="2026-11-16T00:00:00+00:00")

    final = brief(
        sources=[{"title": name, "url": url, "retrieved_at": "待校准"} for name, url in urls.items()],
        evidence=[{"source_url": url, "excerpt": name + "十月下旬进入观赏期。"}
                  for name, url in urls.items()],
    )
    model = ScriptedModel(
        tools(*(ToolCall(id="search-" + name, name="web_search", arguments={"query": name})
                for name in urls)),
        tools(*(ToolCall(id="fetch-" + name, name="web_fetch", arguments={"url": url, "query": name})
                for name, url in urls.items())),
        output(final),
    )
    agent = bind(LLMResearchAgent(provider=model, search_provider=Search()), "research")
    result = agent.research(ResearchRequest(topic="北京赏秋"))
    assert len(result.sources) == 2 and len(result.evidence) == 2
    assert "web_search" not in {item.name for item in model.requests[1].tools}
    assert not {"web_search", "web_fetch"} & {item.name for item in model.requests[2].tools}
    assert model.requests[2].tool_choice == "none" and not model.requests[2].tools


def test_research_blocks_a_second_network_round_without_calling_provider_again():
    class Search:
        calls = 0

        def search(self, query, limit):
            self.calls += 1
            return [SearchHit(title="来源", url=URL, snippet="摘要")]

        def fetch(self, url):
            raise AssertionError("本测试不应读取正文")

    provider = Search()
    model = ScriptedModel(
        tool("web_search", "search-1", query="第一次"),
        tool("web_search", "search-2", query="第二次"),
        output(brief(pending_questions=["尚未读取正文"])),
    )
    result = bind(LLMResearchAgent(provider=model, search_provider=provider), "research").research(
        ResearchRequest(topic="限制联网轮次"))
    assert result.pending_questions and provider.calls == 1
    blocked = next(message for message in model.requests[2].messages if message.tool_call_id == "search-2")
    assert blocked.is_error and "只允许 1 轮" in blocked.content


def test_research_stops_instead_of_looping_when_model_calls_tool_after_fetch():
    class Search:
        def search(self, query, limit):
            return [SearchHit(title="来源", url=URL, snippet="摘要")]

        def fetch(self, url):
            return RetrievedDocument(title="来源", url=url, text="可核验正文。",
                                     retrieved_at="2026-11-16T00:00:00+00:00")

    model = ScriptedModel(
        tool("web_search", "search", query="核验"),
        tool("web_fetch", "fetch", url=URL),
        tool("load_skill", "late-skill", skill_id="skill-news-intelligence"),
    )
    agent = bind(LLMResearchAgent(provider=model, search_provider=Search()), "research")
    with pytest.raises(LLMResponseError, match="避免继续循环和计费"):
        agent.research(ResearchRequest(topic="强制收尾"))
    assert len(model.requests) == 3 and model.requests[-1].tool_choice == "none"


def test_disabled_research_has_no_network_tools_or_fabricated_sources():
    class ForbiddenSearch:
        def search(self, *args):
            raise AssertionError("不应联网")

    model = ScriptedModel(output(brief()))
    agent = bind(LLMResearchAgent(provider=model, search_provider=ForbiddenSearch()), "research")
    result = agent.research(ResearchRequest(topic="素材", content_request=ContentRequest(goal="整理素材", needs_research=False)))
    assert result.sources == [] and not {"web_search", "web_fetch"} & {tool.name for tool in model.requests[0].tools}


def test_research_pages_cached_source_and_rejects_evidence_from_unread_ranges():
    class Search:
        calls = 0
        def search(self, query, limit):
            return [SearchHit(title="来源", url=URL, snippet="摘要")]
        def fetch(self, url):
            self.calls += 1
            return RetrievedDocument(title="来源", url=url, text="前段材料" * 2500 + "末尾可核验事实",
                                     retrieved_at="2026-11-16T00:00:00+00:00")
    provider = Search()
    tools = ResearchTools(provider)
    tools.search(SearchArgs(query="核实资料"))
    page = json.loads(tools.fetch(FetchArgs(url=URL, offset=0, max_chars=8000)))
    assert len(page["text"]) == 8000 and page["next_offset"] == 8000 and page["truncated"]
    result = ResearchBrief(**brief(sources=[{"title": "来源", "url": URL, "retrieved_at": "待校准"}],
                           evidence=[{"source_url": URL, "excerpt": "末尾可核验事实"}]))
    with pytest.raises(ValueError, match="实际读取"):
        tools.validate(result)
    page = json.loads(tools.fetch(FetchArgs(url=URL, offset=page["next_offset"])))
    assert page["next_offset"] is None and page["text"].endswith("末尾可核验事实")
    assert tools.validate(result).evidence[0].excerpt == "末尾可核验事实" and provider.calls == 1
    with pytest.raises(ValueError, match="offset"):
        tools.fetch(FetchArgs(url=URL, offset=page["total_chars"]))
    with pytest.raises(PermissionError):
        tools.fetch(FetchArgs(url="https://example.org/not-searched"))


def test_research_fetch_compresses_to_query_relevant_exact_passages():
    relevant = "香山红叶最佳观赏期为十月下旬，建议工作日上午错峰前往。"

    class Search:
        def search(self, query, limit):
            return [SearchHit(title="赏秋指南", url=URL, snippet="北京赏秋信息")]

        def fetch(self, url):
            return RetrievedDocument(title="赏秋指南", url=url,
                                     text=("无关的页面介绍。" * 400 + "\n\n" + relevant + "\n\n" + "其他信息。" * 400),
                                     retrieved_at="2026-11-16T00:00:00+00:00")

    tools = ResearchTools(Search())
    tools.search(SearchArgs(query="香山红叶最佳观赏期"))
    result = json.loads(tools.fetch(FetchArgs(url=URL)))
    assert result["context_compressed"] is True
    assert result["returned_chars"] <= 3000
    assert relevant in result["text"]
    assert result["returned_chars"] < result["total_chars"]
    assert all(Search().fetch(URL).text[item["start"]:item["end"]] == item["text"]
               for item in result["passages"])


def test_research_fetch_calls_are_limited_per_delegation():
    class Search:
        def search(self, query, limit):
            return [SearchHit(title="来源", url=URL, snippet="摘要")]

        def fetch(self, url):
            return RetrievedDocument(title="来源", url=url, text="可核验正文。",
                                     retrieved_at="2026-11-16T00:00:00+00:00")

    tools = ResearchTools(Search(), max_fetch_calls=2)
    tools.search(SearchArgs(query="核验"))
    first = json.loads(tools.fetch(FetchArgs(url=URL)))
    second = json.loads(tools.fetch(FetchArgs(url=URL)))
    assert first["fetch_calls_remaining"] == 1
    assert second["fetch_calls_remaining"] == 0
    with pytest.raises(LLMResponseError, match="最多允许 2 个并行 web_fetch"):
        tools.fetch(FetchArgs(url=URL))


def test_creation_edits_real_image_reads_it_and_rejects_invented_image_path(tmp_path):
    reference = tmp_path / "reference.png"
    reference.write_bytes(PNG)
    uploads = []

    def handle(request):
        uploads.append(request)
        assert request.url.path == "/v1/images/edits"
        assert b'name="image"' in request.content and PNG in request.content
        assert request.headers["Authorization"] == "Bearer test-image-key"
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    images = OpenAIImageProvider(ImageSettings(model="configured-image-model", api_key="test-image-key"),
                                 tmp_path / "generated", transport=httpx.MockTransport(handle))
    def final(request):
        produced = json.loads(next(message.content for message in request.messages if message.tool_call_id == "generate"))
        return output({"title": "步道路线", "body": "乘地铁抵达步道，封面已按要求调整。", "images": [
            {"description": "蓝色路线封面", "source": produced["source"]}]})

    model = ScriptedModel(tool("generate_image", "generate", prompt="将路线封面调整为蓝色", reference_image_id="image-0"),
                          tool("inspect_image", "inspect", image_id="generated-0"),
                          output({"title": "步道", "body": "新正文", "images": [{"description": "假图", "source": "C:/invented.png"}]}), final)
    agent = bind(LLMCreationAgent(provider=model, image_provider=images,
                                 execution=RoleExecution(supports_vision=True)), "creation")
    previous = Draft(title="步道路线", body="旧正文", images=[ImageRef(description="旧封面", source=str(reference))])
    result = agent.create(ResearchBrief(**brief()), "封面改蓝色，正文补充交通", request=ContentRequest(
        goal="图文分享", generate_images=True), profile=UserProfile(), previous_draft=previous)
    assert result.body != previous.body and Path(result.images[0].source).is_file()
    assert len(uploads) == 1 and model.requests[2].messages[-1].images[0].startswith("data:image/png;base64,")
    assert "不能虚构图片地址" in model.requests[3].messages[-1].content
    assert previous.body == "旧正文"


def test_creation_generates_one_parallel_image_batch_per_delegation():
    barrier = threading.Barrier(2)

    class Images:
        supports_edit = False

        def __init__(self):
            self.calls = []

        def generate(self, prompt, reference=None):
            self.calls.append(prompt)
            barrier.wait(timeout=2)
            return ImageRef(description=prompt, source="generated://" + prompt)

    def final(request):
        generated = [json.loads(message.content) for message in request.messages
                     if message.role == "tool" and message.tool_call_id in {"image-a", "image-b"}]
        return output({"title": "并行配图", "body": "两张图片已生成。",
                       "images": [{"description": item["description"], "source": item["source"]}
                                  for item in generated]})

    provider = Images()
    model = ScriptedModel(tools(
        ToolCall(id="image-a", name="generate_image", arguments={"prompt": "封面"}),
        ToolCall(id="image-b", name="generate_image", arguments={"prompt": "内页"}),
    ), final)
    agent = bind(LLMCreationAgent(provider=model, image_provider=provider), "creation")
    result = agent.create(ResearchBrief(**brief()), request=ContentRequest(
        goal="生成两张配图", generate_images=True), profile=UserProfile())
    assert set(provider.calls) == {"封面", "内页"} and len(result.images) == 2
    assert "generate_image" not in {item.name for item in model.requests[1].tools}
    assert "generate_image" in model.requests[1].messages[-1].content and "永久关闭" in model.requests[1].messages[-1].content


def test_creation_blocks_a_second_image_generation_round():
    class Images:
        supports_edit = False
        calls = 0

        def generate(self, prompt, reference=None):
            self.calls += 1
            return ImageRef(description=prompt, source="generated://first")

    provider = Images()

    def final(request):
        first = json.loads(next(message.content for message in request.messages
                                if message.tool_call_id == "image-first"))
        return output({"title": "单批次配图", "body": "保留首轮生成结果。",
                       "images": [{"description": first["description"], "source": first["source"]}]})

    model = ScriptedModel(
        tool("generate_image", "image-first", prompt="首轮"),
        tool("generate_image", "image-second", prompt="第二轮"),
        final,
    )
    agent = bind(LLMCreationAgent(provider=model, image_provider=provider), "creation")
    result = agent.create(ResearchBrief(**brief()), request=ContentRequest(
        goal="生成配图", generate_images=True), profile=UserProfile())
    blocked = next(message for message in model.requests[2].messages
                   if message.tool_call_id == "image-second")
    assert provider.calls == 1 and blocked.is_error and "只允许 1 轮" in blocked.content
    assert result.images[0].source == "generated://first"


def test_text_only_creation_exposes_capabilities_and_must_apply_feedback():
    previous = Draft(title="路线", body="旧的路线说明")
    model = ScriptedModel(output(previous.model_dump(include={"title", "body"})),
                          output({"title": "路线", "body": "新的路线说明，包含交通信息"}))
    agent = bind(LLMCreationAgent(provider=model), "creation")
    result = agent.create(ResearchBrief(**brief()), "补充交通信息", request=ContentRequest(goal="路线"),
                          profile=UserProfile(), previous_draft=previous)
    payload = json.loads(model.requests[0].messages[1].content)
    assert payload["capabilities"] == {"image_input": False, "image_generation": False, "image_editing": False}
    assert not {"generate_image", "inspect_image"} & {tool.name for tool in model.requests[0].tools}
    assert "不能原样返回" in model.requests[1].messages[-1].content and result.body != previous.body


def test_structured_agent_accepts_single_json_object_wrapped_in_explanation_and_fence():
    wrapped = ChatMessage(role="assistant", content=(
        "图片工具暂不可用，以下为结构化草稿：\n```json\n"
        + json.dumps({"title": "秋日路线", "body": "沿着胡同步行，记录真实秋色。"}, ensure_ascii=False)
        + "\n```"))
    agent = bind(LLMCreationAgent(provider=ScriptedModel(wrapped)), "creation")
    result = agent.create(ResearchBrief(**brief()), request=ContentRequest(goal="秋日路线"),
                          profile=UserProfile())
    assert result.title == "秋日路线"


def test_generation_is_cached_across_provider_restarts(tmp_path):
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    config = ImageSettings(model="configured-image-model", api_key="test-key")
    first = OpenAIImageProvider(config, tmp_path, transport=httpx.MockTransport(handle)).generate("步道封面")
    second = OpenAIImageProvider(config, tmp_path, transport=httpx.MockTransport(handle)).generate("步道封面")
    assert first == second and len(calls) == 1 and Path(first.source).read_bytes() == PNG
    assert calls[0]["output_format"] == "png" and "response_format" not in calls[0]


def test_dashscope_generation_downloads_temporary_url_and_saves_png(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["model"] == "qwen-image-3.0"
            assert body["size"] == "1024x1536"
            assert request.headers["Authorization"] == "Bearer test-key"
            return httpx.Response(200, json={"data": [{"url": "https://images.example.org/result.png"}]})
        assert request.url == "https://images.example.org/result.png"
        return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})

    config = ImageSettings(provider="dashscope",
                           base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                           api_key="test-key", model="qwen-image-3.0", size="1024x1536")
    result = DashScopeImageProvider(config, tmp_path, transport=httpx.MockTransport(handle)).generate("北京秋日封面")
    assert len(calls) == 2
    assert Path(result.source).read_bytes() == PNG


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_multimodal_messages_use_each_protocols_actual_content_blocks(protocol):
    captured = []
    def handle(request):
        captured.append(json.loads(request.content))
        if protocol == "openai":
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "图像说明"}, "finish_reason": "stop"}]})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "图像说明"}], "stop_reason": "end_turn"})
    provider = create_llm_provider(LLMConfig(provider="custom", protocol=protocol, model="configured-vision-model",
                                           base_url="https://example.org/v1", api_key="test-key"), transport=httpx.MockTransport(handle))
    from xhs_agent.llm import ChatRequest
    with provider:
        provider.chat(ChatRequest(messages=[ChatMessage(role="user", content="描述图片", images=[
            "data:image/png;base64," + base64.b64encode(PNG).decode()])]))
    block = captured[0]["messages"][0]["content"][1]
    if protocol == "openai":
        assert block["type"] == "image_url" and block["image_url"]["url"].startswith("data:image/png")
    else:
        assert block["type"] == "image" and block["source"]["media_type"] == "image/png"


def test_model_review_can_call_rules_but_cannot_remove_deterministic_issues():
    model = ScriptedModel(tool("run_rules", "rules"), output({"draft_version": 1, "issues": []}))
    agent = bind(LLMReviewAgent(provider=model), "review")
    result = agent.review(ContentRequest(goal="分享"), UserProfile(), ResearchBrief(**brief()),
                          Draft(version=1, title="路线", body="正文", images=[ImageRef(description="占位")]))
    assert any("占位" in issue.message for issue in result.issues)
    rules = next(message for message in model.requests[1].messages if message.tool_call_id == "rules")
    assert "占位" in rules.content


def test_model_review_suggestions_never_block_a_basically_complete_draft():
    model = ScriptedModel(output({"draft_version": 1, "issues": [{
        "message": "标题还可以更有吸引力", "severity": "error", "target": "create",
    }]}))
    agent = bind(LLMReviewAgent(provider=model), "review")
    result = agent.review(ContentRequest(goal="分享路线"), UserProfile(), ResearchBrief(**brief()),
                          Draft(version=1, title="周末路线", body="沿步道散步并记录沿途风景。"))
    assert result.passed
    assert [(issue.message, issue.severity) for issue in result.issues] == [
        ("标题还可以更有吸引力", "warning"),
    ]
    assert ReviewIssue(message="普通优化建议").severity == "warning"


def test_rule_review_uses_the_same_xiaohongshu_text_limits_as_publishing():
    reviewer = RuleReviewAgent()
    context = (ContentRequest(goal="分享路线"), UserProfile(), ResearchBrief(**brief()))
    boundary = reviewer.review(*context, Draft(
        version=1, title="题" * 20, body="正" * 996, tags=["话题"],
    ))
    assert boundary.passed

    over = reviewer.review(*context, Draft(
        version=1, title="题" * 21, body="正" * 997, tags=["话题"],
    ))
    errors = [issue.message for issue in over.issues if issue.severity == "error"]
    assert any("标题当前 21 个字符" in message for message in errors)
    assert any("正文与话题合计 1001 个字符" in message for message in errors)


def test_role_tool_and_skill_configuration_cannot_expand_permissions(tmp_path):
    config = AgentConfig(ideation_backend="mock", supervisor_backend="mock", execution=ExecutionConfig(
        roles={"ideation": RoleExecution(skill_ids=(), tools=())}))
    svc = TaskService(tmp_path, config=config)
    try:
        assert svc.agent_registry.tools_for("ideation") == () and not svc.ideation_agent.skills.catalog()
    finally:
        svc.close()
    with pytest.raises(ValueError):
        TaskService(tmp_path / "invalid", config=config.model_copy(update={"execution": ExecutionConfig(
            roles={"review": RoleExecution(tools=("generate_image",))})}))


def test_execution_configuration_is_separate_and_does_not_expose_keys(tmp_path):
    path = tmp_path / "execution.json"
    path.write_text(json.dumps({"execution": {"roles": {"creation": {"supports_vision": True, "prompt": "写作要求"}},
                                             "search": {"api_key": "private-test-key"}}}), encoding="utf8")
    config = load_execution_config(path)
    assert config.for_role("creation").supports_vision and config.search.key() == "private-test-key"
    assert "private-test-key" not in repr(config)


def test_cli_loads_separate_execution_settings_without_starting_server(tmp_path, monkeypatch):
    pytest.importorskip("uvicorn")
    from xhs_agent.cli import main
    path = tmp_path / "execution.json"
    path.write_text(json.dumps({"execution": {"roles": {"creation": {"supports_vision": True, "prompt": "实际执行配置"}}}}), encoding="utf8")
    configs = []
    monkeypatch.setattr("xhs_agent.web.create_app", lambda root, config, **kwargs: config)
    monkeypatch.setattr("uvicorn.run", lambda config, **kwargs: configs.append(config))
    assert main(["--data-dir", str(tmp_path / "data"), "--mock-supervisor", "--mock-ideation",
                 "--execution-config", str(path), "web"]) == 0
    assert configs[0].execution.for_role("creation").supports_vision
    assert configs[0].execution.for_role("creation").prompt == "实际执行配置"


def test_web_exposes_effective_tool_grants_without_credentials(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from xhs_agent.web import create_app
    execution = ExecutionConfig(search=SearchSettings(api_key="private-search-key"),
                                images=ImageSettings(model="image-model", api_key="private-image-key"),
                                roles={"creation": RoleExecution(supports_vision=True)})
    config = AgentConfig(ideation_backend="mock", supervisor_backend="mock", research_backend="llm",
                         creation_backend="llm", review_backend="llm", execution=execution)
    with TestClient(create_app(tmp_path, config=config)) as client:
        response = client.get("/api/agents")
        assert response.status_code == 200
        agents = {item["agent_id"]: item for item in response.json()["agents"]}
        assert {"web_search", "web_fetch"}.issubset(agents["research"]["tool_names"])
        assert {"generate_image", "inspect_image"}.issubset(agents["creation"]["tool_names"])
        assert "generate_image" not in agents["review"]["tool_names"]
        assert "private-search-key" not in response.text and "private-image-key" not in response.text


def test_once_delegated_agents_use_private_context_and_parent_gets_references(tmp_path):
    research = ScriptedModel(output(brief(pending_questions=["需补充资料"])))
    creation = ScriptedModel(output({"title": "路线分享", "body": "根据用户素材整理路线"}))
    review = ScriptedModel(tool("run_rules", "rules"), output({"draft_version": 1, "issues": []}))
    svc = TaskService(tmp_path, config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"),
                      research_agent=LLMResearchAgent(provider=research), creation_agent=LLMCreationAgent(provider=creation),
                      review_agent=LLMReviewAgent(provider=review))
    try:
        task = svc.create_task(ContentRequest(goal="整理散步路线", topic="散步路线", supplied_materials=["走过步道"]))
        assert task.status == "pending_review" and not task.draft.approved
        collaboration = svc.collaboration(task.task_id)
        assert [record["task"]["target_agent"] for record in collaboration["history"]] == ["research", "creation", "review"]
        assert all(len(model.requests[0].messages) == 2 for model in (research, creation, review))
        assert "tool_calls" not in json.dumps(collaboration["history"])
        assert all(record["result"]["artifacts"] for record in collaboration["history"])
        assert svc.store.publish_count(task.task_id) == 0
    finally:
        svc.close()
