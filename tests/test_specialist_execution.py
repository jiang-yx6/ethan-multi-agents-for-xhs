"""第二阶段：真实工具协议、模型自主工具循环、反馈与上下文边界；不访问外网。"""
import base64
import json
from pathlib import Path

import httpx
import pytest

from xhs_agent.agents.images import OpenAIImageProvider
from xhs_agent.agents.structured import LLMCreationAgent, LLMResearchAgent, LLMReviewAgent
from xhs_agent.agents.research.search import TavilySearchProvider
from xhs_agent.config import AgentConfig
from xhs_agent.execution import ExecutionConfig, ImageSettings, RoleExecution, SearchSettings, load_execution_config
from xhs_agent.llm import ChatMessage, ChatResponse, LLMConfig, ToolCall, create_llm_provider
from xhs_agent.models import ContentRequest, Draft, ImageRef, ResearchBrief, ResearchRequest, UserProfile
from xhs_agent.service import TaskService
from xhs_agent.skills import SkillLibrary

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lWQAAAAASUVORK5CYII=")
URL = "https://example.org/original"


def tool(name, identifier, **arguments):
    return ChatMessage(role="assistant", tool_calls=[ToolCall(id=identifier, name=name, arguments=arguments)])


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
                          tool("web_fetch", "bad", url="https://example.org/failed"),
                          tool("web_fetch", "good", url=URL), output(invalid), output(valid))
    agent = bind(LLMResearchAgent(provider=model, search_provider=provider, on_event=events.append), "research")
    result = agent.research(ResearchRequest(topic="散步路线"))
    assert result.sources[0].title == "原始路线公告" and result.sources[0].retrieved_at != "invented"
    assert result.evidence[0].excerpt == "步道每日七点开放" and not result.evidence[0].is_mock
    assert model.requests[2].messages[-1].is_error
    assert "摘录必须存在" in model.requests[4].messages[-1].content
    assert [call[0] for call in calls] == ["/search", "/extract", "/extract"]
    assert any(event["kind"] == "output_validation" for event in events)
    assert all("test-search-key" not in json.dumps(event) for event in events)


def test_disabled_research_has_no_network_tools_or_fabricated_sources():
    class ForbiddenSearch:
        def search(self, *args):
            raise AssertionError("不应联网")

    model = ScriptedModel(output(brief()))
    agent = bind(LLMResearchAgent(provider=model, search_provider=ForbiddenSearch()), "research")
    result = agent.research(ResearchRequest(topic="素材", content_request=ContentRequest(goal="整理素材", needs_research=False)))
    assert result.sources == [] and not {"web_search", "web_fetch"} & {tool.name for tool in model.requests[0].tools}


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
    assert "占位" in model.requests[1].messages[-1].content


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
