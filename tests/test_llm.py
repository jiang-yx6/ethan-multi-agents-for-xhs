import json

import httpx
import pytest

from xhs_agent.llm import (
    AnthropicProvider, ChatMessage, ChatRequest, LLMConfigurationError, LLMConfig,
    LLMConnectionError, LLMHTTPError, LLMProvider, LLMResponseError, LLMTimeoutError,
    OpenAIProvider, ProviderPreset, ToolCall, ToolDefinition, create_llm_provider, list_provider_presets,
    register_provider_preset,
)


def request(**kwargs):
    return ChatRequest(messages=[ChatMessage(role="system", content="中文助手"),
                                 ChatMessage(role="user", content="帮我选题")], **kwargs)


def openai_response(**changes):
    payload = {"id": "reply-1", "model": "test-model", "choices": [
        {"message": {"role": "assistant", "content": "选题结果"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    payload.update(changes)
    return payload


def anthropic_response(**changes):
    payload = {"id": "reply-2", "role": "assistant", "model": "test-model",
               "content": [{"type": "text", "text": "审核结果"}], "stop_reason": "end_turn",
               "usage": {"input_tokens": 20, "output_tokens": 6}}
    payload.update(changes)
    return payload


@pytest.mark.parametrize("name", list(list_provider_presets()))
def test_factory_vendor_presets(name):
    with create_llm_provider(name, model="explicit-model", api_key="test-secret", environ={}) as provider:
        preset = list_provider_presets()[name]
        assert isinstance(provider, LLMProvider)
        assert isinstance(provider, AnthropicProvider if preset.protocol == "anthropic" else OpenAIProvider)
        assert provider.config.base_url == preset.base_url
        assert provider.config.model == "explicit-model"
        assert "test-secret" not in repr(provider.config)
        assert "test-secret" not in provider.config.model_dump_json()


@pytest.mark.parametrize("alias,canonical", [("claude", "anthropic"), ("dashscope", "qwen"), ("glm", "zhipu")])
def test_factory_aliases(alias, canonical):
    with create_llm_provider(alias, model="test", api_key="key", environ={}) as provider:
        assert provider.config.provider == canonical


def test_openai_request_headers_and_normalized_response():
    def handle(incoming):
        body = json.loads(incoming.content)
        assert str(incoming.url) == "https://api.openai.com/v1/chat/completions"
        assert incoming.headers["authorization"] == "Bearer test-key"
        assert body["max_completion_tokens"] == 100
        assert "max_tokens" not in body
        assert body["temperature"] == 0
        assert body["messages"][0] == {"role": "system", "content": "中文助手"}
        assert body["stream"] is False
        return httpx.Response(200, json=openai_response())

    with create_llm_provider("openai", model="test", api_key="test-key", transport=httpx.MockTransport(handle)) as provider:
        response = provider.chat(request(max_tokens=100, temperature=0))
    assert response.text == "选题结果"
    assert response.usage.total_tokens == 15
    assert response.finish_reason == "stop"


def test_anthropic_request_and_system_extraction():
    def handle(incoming):
        body = json.loads(incoming.content)
        assert str(incoming.url) == "https://gateway.example/anthropic/v1/messages"
        assert incoming.headers["x-api-key"] == "test-key"
        assert incoming.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in incoming.headers
        assert body["system"] == "中文助手"
        assert body["max_tokens"] == 77
        assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "帮我选题"}]}]
        return httpx.Response(200, json=anthropic_response())

    with create_llm_provider("anthropic", model="test", api_key="test-key", base_url="https://gateway.example/anthropic/",
                             transport=httpx.MockTransport(handle)) as provider:
        response = provider.chat(request(max_tokens=77))
    assert response.finish_reason == "stop"
    assert response.text == "审核结果"
    assert response.usage.total_tokens == 26


def test_custom_vendor_and_environment_config():
    environment = {"XHS_LLM_PROVIDER": "my-vendor", "XHS_LLM_MODEL": "deployment-123",
                   "XHS_LLM_PROTOCOL": "anthropic", "XHS_LLM_BASE_URL": "https://example.org/v1/",
                   "XHS_LLM_API_KEY_ENV": "CUSTOM_MODEL_KEY", "CUSTOM_MODEL_KEY": "env-secret"}
    with create_llm_provider(environ=environment) as provider:
        assert isinstance(provider, AnthropicProvider)
        assert provider._endpoint() == "https://example.org/v1/messages"
        assert provider.config.api_key.get_secret_value() == "env-secret"
    config = LLMConfig(provider="unknown", protocol="openai", base_url="http://localhost:1234/v1", model="local",
                       auth_required=False)
    with create_llm_provider(config, environ={}) as provider:
        assert isinstance(provider, OpenAIProvider)
        assert "Authorization" not in provider._headers()
    with create_llm_provider("ollama", model="local-model", environ={}) as provider:
        assert not provider.config.auth_required


def test_explicit_api_key_wins_over_environment():
    with create_llm_provider("deepseek", model="test", api_key="explicit", api_key_env="MISSING",
                             environ={"XHS_LLM_API_KEY": "generic", "DEEPSEEK_API_KEY": "vendor"}) as provider:
        assert provider.config.api_key.get_secret_value() == "explicit"
        assert provider.config.token_parameter == "max_tokens"
    with create_llm_provider("deepseek", model="test", environ={"DEEPSEEK_API_KEY": "vendor"}) as provider:
        assert provider.config.api_key.get_secret_value() == "vendor"
    with pytest.raises(LLMConfigurationError, match="缺少"):
        create_llm_provider("deepseek", model="test", api_key_env="MISSING", environ={"DEEPSEEK_API_KEY": "vendor"})


@pytest.mark.parametrize("kwargs", [
    {"provider": "openai", "model": "test"},
    {"provider": "unknown", "model": "test", "api_key": "key"},
    {"provider": "openai", "api_key": "never-echo-this-secret"},
    {"provider": "deepseek", "model": "test", "protocol": "anthropic", "api_key": "key"},
    {"provider": "anthropic", "model": "test", "token_parameter": "max_completion_tokens", "api_key": "key"},
    {"provider": "openai", "model": "test", "base_url": "https://user:secret@example.org/v1", "api_key": "key"},
])
def test_invalid_factory_configuration_fails_before_network_and_masks_keys(kwargs):
    with pytest.raises(LLMConfigurationError) as error:
        create_llm_provider(environ={}, **kwargs)
    assert "never-echo-this-secret" not in str(error.value)


def test_openai_tool_roundtrip_and_reasoning_preservation():
    tools = [ToolDefinition(name="load_skill", input_schema={"type": "object", "properties": {"skill_id": {"type": "string"}}})]
    calls = 0

    def handle(incoming):
        nonlocal calls
        calls += 1
        body = json.loads(incoming.content)
        assert body["tools"][0]["function"]["name"] == "load_skill"
        assert body["max_tokens"] == 4096
        if calls == 1:
            return httpx.Response(200, json=openai_response(choices=[{
                "message": {"role": "assistant", "content": None, "reasoning_content": "分析过程",
                            "tool_calls": [{"id": "call-1", "type": "function", "function": {
                                "name": "load_skill", "arguments": '{"skill_id":"demo"}'}}]},
                "finish_reason": "tool_calls"}]))
        assert body["messages"][-2]["reasoning_content"] == "分析过程"
        assert body["messages"][-1] == {"role": "tool", "content": "技能正文", "tool_call_id": "call-1"}
        return httpx.Response(200, json=openai_response())

    with create_llm_provider("deepseek", model="test", api_key="key", transport=httpx.MockTransport(handle)) as provider:
        first = provider.chat(request(tools=tools))
        assert first.message.tool_calls[0].arguments == {"skill_id": "demo"}
        second = provider.chat(ChatRequest(messages=[ChatMessage(role="user", content="任务"), first.message,
                                 ChatMessage(role="tool", tool_call_id="call-1", content="技能正文")], tools=tools))
        assert second.text == "选题结果"


def test_anthropic_parallel_tool_results_and_signed_thinking_roundtrip():
    tools = [ToolDefinition(name="read_resource")]
    blocks = [{"type": "thinking", "thinking": "分析", "signature": "signed-content"},
              {"type": "tool_use", "id": "one", "name": "read_resource", "input": {"path": "a"}},
              {"type": "tool_use", "id": "two", "name": "read_resource", "input": {"path": "b"}}]
    calls = 0

    def handle(incoming):
        nonlocal calls
        calls += 1
        body = json.loads(incoming.content)
        if calls == 1:
            assert body["tool_choice"] == {"type": "any"}
            return httpx.Response(200, json=anthropic_response(content=blocks, stop_reason="tool_use"))
        assert body["messages"][-2]["content"] == blocks
        results = body["messages"][-1]
        assert results["role"] == "user"
        assert [r["tool_use_id"] for r in results["content"]] == ["one", "two"]
        assert results["content"][1]["is_error"] is True
        return httpx.Response(200, json=anthropic_response())

    with create_llm_provider("anthropic", model="test", api_key="key", transport=httpx.MockTransport(handle)) as provider:
        first = provider.chat(request(tools=tools, tool_choice="required"))
        assert first.finish_reason == "tool_calls"
        result = provider.chat(ChatRequest(messages=[ChatMessage(role="user", content="任务"), first.message,
            ChatMessage(role="tool", tool_call_id="one", content="参考资料"),
            ChatMessage(role="tool", tool_call_id="two", content="无权访问", is_error=True)], tools=tools))
        assert result.text == "审核结果"


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_http_errors_do_not_echo_upstream_body_or_retry_automatically(status):
    calls = []

    def handle(incoming):
        calls.append(incoming)
        return httpx.Response(status, headers={"location": "https://other.example"}, text="never-echo-this-secret")

    with create_llm_provider("openai", model="test", api_key="never-echo-this-secret", transport=httpx.MockTransport(handle)) as provider:
        with pytest.raises(LLMHTTPError) as error:
            provider.chat(request())
    assert error.value.status_code == status
    assert error.value.retryable == (status >= 500 or status == 429)
    assert "never-echo-this-secret" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("error_type,expected", [(httpx.ReadTimeout, LLMTimeoutError), (httpx.ConnectError, LLMConnectionError)])
def test_transport_errors_are_normalized(error_type, expected):
    def handle(incoming):
        raise error_type("secret-upstream-details", request=incoming)

    with create_llm_provider("openai", model="test", api_key="key", transport=httpx.MockTransport(handle)) as provider:
        with pytest.raises(expected) as error:
            provider.chat(request())
    assert "secret-upstream-details" not in str(error.value)


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"error": {"message": "secret"}},
    openai_response(choices=[{"finish_reason": "tool_calls", "message": {"content": ""}}]),
    openai_response(choices=[{"finish_reason": "tool_calls", "message": {"tool_calls": [
        {"id": "x", "type": "function", "function": {"name": "unauthorized", "arguments": "{}"}}]}}])])
def test_malformed_or_unoffered_tool_responses_fail(payload):
    with create_llm_provider("openai", model="test", api_key="key",
                             transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as provider:
        with pytest.raises(LLMResponseError):
            provider.chat(request())


def test_protected_options_cannot_override_protocol_fields():
    def no_network(incoming):
        raise AssertionError("不应发出请求")

    with create_llm_provider("openai", model="test", api_key="key", transport=httpx.MockTransport(no_network)) as provider:
        with pytest.raises(LLMConfigurationError):
            provider.chat(request(extra_body={"model": "other"}))
    with create_llm_provider("anthropic", model="test", api_key="key", transport=httpx.MockTransport(no_network)) as provider:
        with pytest.raises(LLMConfigurationError):
            provider.chat(request(temperature=1.5))


def test_request_rejects_missing_or_duplicate_tool_results():
    assistant = ChatMessage(role="assistant", tool_calls=[ToolCall(id="one", name="load_skill")])
    with pytest.raises(ValueError):
        ChatRequest(messages=[assistant])
    with pytest.raises(ValueError):
        ChatRequest(messages=[ChatMessage(role="tool", tool_call_id="unknown", content="结果")])
    with pytest.raises(ValueError):
        ChatRequest(messages=[assistant, ChatMessage(role="tool", tool_call_id="one"), ChatMessage(role="tool", tool_call_id="one")])


def test_factory_merges_overrides_before_validating_environment_or_mapping():
    environment = {"XHS_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "env-key"}
    with create_llm_provider(environ=environment, model="explicit-model", timeout=9) as provider:
        assert provider.config.model == "explicit-model"
        assert provider.config.timeout == 9
    with create_llm_provider({"provider": "deepseek"}, model="explicit-model", api_key="key") as provider:
        assert provider.config.model == "explicit-model"
        assert provider.config.provider == "deepseek"


def test_environment_options_support_local_servers_and_anthropic_gateways():
    environment = {
        "XHS_LLM_PROVIDER": "private", "XHS_LLM_MODEL": "deployment",
        "XHS_LLM_PROTOCOL": "anthropic", "XHS_LLM_BASE_URL": "http://localhost:8080/anthropic",
        "XHS_LLM_AUTH_REQUIRED": "false", "XHS_LLM_AUTH_MODE": "bearer",
        "XHS_LLM_ANTHROPIC_VERSION": "2023-06-01",
    }
    with create_llm_provider(environ=environment) as provider:
        assert not provider.config.auth_required
        assert "Authorization" not in provider._headers()
        assert provider._endpoint() == "http://localhost:8080/anthropic/v1/messages"
    environment["XHS_LLM_API_KEY"] = "gateway-key"
    with create_llm_provider(environ=environment) as provider:
        assert provider._headers()["Authorization"] == "Bearer gateway-key"
        assert "x-api-key" not in provider._headers()


@pytest.mark.parametrize("protocol,auth_mode,expected_header,expected_value", [
    ("openai", "bearer", "authorization", "Bearer key"),
    ("openai", "x-api-key", "x-api-key", "key"),
    ("anthropic", "bearer", "authorization", "Bearer key"),
    ("anthropic", "x-api-key", "x-api-key", "key"),
])
def test_authentication_can_be_configured_for_compatible_gateways(protocol, auth_mode, expected_header, expected_value):
    def handle(incoming):
        assert incoming.headers[expected_header] == expected_value
        other_header = "x-api-key" if expected_header == "authorization" else "authorization"
        assert other_header not in incoming.headers
        return httpx.Response(200, json=anthropic_response() if protocol == "anthropic" else openai_response())

    with create_llm_provider("gateway", protocol=protocol, base_url="https://gateway.example/v1",
                             model="deployment", api_key="key", auth_mode=auth_mode,
                             transport=httpx.MockTransport(handle)) as provider:
        assert provider.chat(request()).text


@pytest.fixture
def isolated_presets(monkeypatch):
    from xhs_agent.llm import factory
    monkeypatch.setattr(factory, "_PRESETS", dict(factory._PRESETS))
    monkeypatch.setattr(factory, "_ALIASES", dict(factory._ALIASES))


def test_register_custom_vendor_and_alias_without_modifying_adapters(isolated_presets):
    register_provider_preset("Acme_Cloud", ProviderPreset(
        protocol="anthropic", base_url="https://acme.example/anthropic/", api_key_env="ACME_KEY",
        auth_mode="bearer",
    ), aliases=("acme",))
    with create_llm_provider("acme", model="deployment", environ={"ACME_KEY": "key"}) as provider:
        assert isinstance(provider, AnthropicProvider)
        assert provider.config.provider == "acme-cloud"
        assert provider._endpoint() == "https://acme.example/anthropic/v1/messages"
        assert provider._headers()["Authorization"] == "Bearer key"
    assert "acme-cloud" in list_provider_presets()


def test_registration_validates_and_does_not_silently_replace_presets(isolated_presets):
    preset = ProviderPreset("openai", "https://example.org/v1", "ACME_KEY")
    with pytest.raises(LLMConfigurationError):
        register_provider_preset("openai", preset)
    with pytest.raises(LLMConfigurationError):
        register_provider_preset("acme", preset, aliases=("openai",))
    with pytest.raises(LLMConfigurationError):
        register_provider_preset("acme", ProviderPreset("anthropic", "https://example.org", "KEY", "max_completion_tokens"))
    with pytest.raises(LLMConfigurationError):
        register_provider_preset("acme", ProviderPreset("openai", "https://user:secret@example.org", "KEY"))
    assert "acme" not in list_provider_presets()
    register_provider_preset("openai", preset, replace=True)
    with create_llm_provider("openai", model="deployment", environ={"ACME_KEY": "key"}) as provider:
        assert provider.config.base_url == "https://example.org/v1"


def test_presets_return_an_independent_registry_snapshot():
    copy = list_provider_presets()
    copy.clear()
    assert "openai" in list_provider_presets()


def test_vendor_specific_options_are_passed_through_with_request_precedence():
    def handle(incoming):
        body = json.loads(incoming.content)
        assert body["thinking"] == {"type": "disabled"}
        assert body["top_p"] == 0.7
        return httpx.Response(200, json=openai_response())

    with create_llm_provider("deepseek", model="test", api_key="key",
                             extra_body={"thinking": {"type": "enabled"}, "top_p": 0.7},
                             transport=httpx.MockTransport(handle)) as provider:
        provider.chat(request(extra_body={"thinking": {"type": "disabled"}}))


def test_closing_context_manager_closes_transport():
    class RecordingTransport(httpx.MockTransport):
        closed = False

        def close(self):
            self.closed = True

    transport = RecordingTransport(lambda _: httpx.Response(200, json=openai_response()))
    with create_llm_provider("openai", model="test", api_key="key", transport=transport):
        assert not transport.closed
    assert transport.closed
