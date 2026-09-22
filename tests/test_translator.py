"""
LLM Translator 测试

测试点：
- 工厂函数 create_translator
- OpenAICompatibleTranslator 实例化与属性
- 响应解析 _parse_response
- 无 API key 时正确报错
- 接口一致性（返回 TranslateResult 列表）
- Prompt Builder 集成
"""
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.translate import llm as llm_module

from pipeline.translate.llm import (
    _COMPATIBILITY_CACHE, BaseTranslator, LLMAuthenticationError,
    LLMConfigurationError, LLMQuotaError, LLMTransientError,
    PARAMETER_DEGRADATION_ORDER, OpenAICompatibleTranslator,
    TranslateResult, create_translator,
)
from pipeline.parser.srt_parser import Subtitle


TEST_BASE_URL = "https://relay.example/v1"


def test_factory_create():
    """工厂函数创建翻译器"""
    t = create_translator(api_key="sk-test")
    assert isinstance(t, OpenAICompatibleTranslator)


def test_factory_with_base_url():
    """工厂函数支持 base_url"""
    t = create_translator(api_key="sk-test", model="gpt-4o",
                          base_url="https://api.deepseek.com/v1")
    assert t.base_url == "https://api.deepseek.com/v1"
    assert t.model == "gpt-4o"


def test_model_is_empty_without_explicit_configuration(monkeypatch):
    """首次使用不能暗中选择任何供应商模型。"""
    monkeypatch.delenv("LLM_MODEL", raising=False)
    t = OpenAICompatibleTranslator(api_key="sk-test")
    assert t.default_model == ""
    assert t.model == ""


def test_name():
    """后端名称"""
    t = OpenAICompatibleTranslator(api_key="sk-test")
    assert t.name == "openai-compatible"


def test_parse_response_normal():
    """正常响应解析"""

    class DummyTranslator(OpenAICompatibleTranslator):
        def _call_api(self, prompt):
            return ("", 0, 0)

    t = DummyTranslator(api_key="sk-test")
    batch = [
        Subtitle(id=1, start="00:00:01,000", end="00:00:03,000", text="Hello"),
        Subtitle(id=2, start="00:00:04,000", end="00:00:06,000", text="World"),
    ]

    response = "[1] 你好\n[2] 世界"
    results = t._parse_response(response, batch)

    assert len(results) == 2
    assert results[0].id == 1
    assert results[0].original == "Hello"
    assert results[0].translated == "你好"
    assert results[1].id == 2
    assert results[1].original == "World"
    assert results[1].translated == "世界"


def test_parse_response_missing_item():
    """LLM 漏翻某条时不出错"""

    class DummyTranslator(OpenAICompatibleTranslator):
        def _call_api(self, prompt):
            return ("", 0, 0)

    t = DummyTranslator(api_key="sk-test")
    batch = [
        Subtitle(id=1, start="00:00:01,000", end="00:00:03,000", text="A"),
        Subtitle(id=2, start="00:00:04,000", end="00:00:06,000", text="B"),
    ]

    # 只返回了第一条的翻译
    response = "[1] 翻译A"
    results = t._parse_response(response, batch)

    assert len(results) == 2
    assert results[0].translated == "翻译A"
    assert results[1].translated == ""  # 漏翻项为空


def test_no_api_key_error():
    """无 API key 时正确报错"""
    t = OpenAICompatibleTranslator(api_key="")
    try:
        t._call_api("test")
        assert False, "应该抛出 RuntimeError"
    except RuntimeError as e:
        assert "API key" in str(e)


def test_translate_result_dataclass():
    """TranslateResult 数据类字段完整性"""
    r = TranslateResult(
        id=1,
        original="Hello",
        translated="你好",
        tokens_in=100,
        tokens_out=50,
        cost=0.001,
        cached=False,
    )
    assert r.id == 1
    assert r.original == "Hello"
    assert r.translated == "你好"
    assert r.tokens_in == 100
    assert r.tokens_out == 50
    assert r.cost == 0.001
    assert r.cached is False


def test_env_var_api_key():
    """从环境变量读取 API key"""
    os.environ["LLM_API_KEY"] = "sk-from-env"
    t = OpenAICompatibleTranslator()
    assert t.api_key == "sk-from-env"
    del os.environ["LLM_API_KEY"]


def test_explicit_empty_base_url_never_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://environment.example/v1")
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url="",
    )

    assert translator.base_url == ""
    with pytest.raises(LLMConfigurationError, match="接口地址不能为空"):
        translator._call_api("prompt")


def _sse_lines(*contents, with_usage=True):
    """Build OpenAI-compatible SSE lines: role chunk, content deltas, [DONE]."""
    lines = [
        'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,'
        '"delta":{"role":"assistant"},"finish_reason":null}]}',
    ]
    for piece in contents:
        lines.append(
            'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,'
            f'"delta":{{"content":"{piece}"}},"finish_reason":null}}]}}'
        )
    lines.append(
        'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,'
        '"delta":{},"finish_reason":"stop"}]}'
    )
    if with_usage:
        lines.append(
            'data: {"id":"x","object":"chat.completion.chunk","choices":[],'
            '"usage":{"prompt_tokens":2,"completion_tokens":1}}'
        )
    lines.append("data: [DONE]")
    return lines


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *args):
        return False


def _sse_response(status_code=200, lines=None, text=""):
    return SimpleNamespace(
        status_code=status_code,
        text=text,
        headers={},
        iter_lines=lambda: iter(lines or []),
    )


def test_retryable_api_status_is_retried(monkeypatch):
    responses = [
        SimpleNamespace(status_code=500, text="busy", headers={}),
        SimpleNamespace(status_code=429, text="limited", headers={}),
        _sse_response(lines=_sse_lines("[1] 好")),
    ]
    fake_httpx = SimpleNamespace(
        stream=lambda *args, **kwargs: _FakeStream(responses.pop(0)),
        Timeout=lambda *args, **kwargs: None,
        ConnectError=OSError,
        TimeoutException=TimeoutError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
    )

    text, tokens_in, tokens_out = translator._call_api("prompt")

    assert text == "[1] 好"
    assert (tokens_in, tokens_out) == (2, 1)
    assert not responses


def test_timeout_and_malformed_response_are_retried(monkeypatch):
    calls = 0

    def stream(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("simulated timeout")
        if calls == 2:
            # 流中断：只收到部分内容，没有 [DONE]/finish_reason → 必须重试整批
            return _FakeStream(_sse_response(lines=[
                'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,'
                '"delta":{"content":"[1] 残"},finish_reason":null}]}',
            ]))
        return _FakeStream(_sse_response(lines=_sse_lines("[1] 已恢复")))

    fake_httpx = SimpleNamespace(
        stream=stream,
        Timeout=lambda *args, **kwargs: None,
        ConnectError=OSError,
        TimeoutException=TimeoutError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
    )

    text, tokens_in, tokens_out = translator._call_api("prompt")

    assert text == "[1] 已恢复"
    assert (tokens_in, tokens_out) == (2, 1)
    assert calls == 3


def test_authentication_error_is_not_retried(monkeypatch):
    calls = []
    response = _sse_response(
        status_code=401, text='{"error":{"message":"Invalid token"}}'
    )
    fake_httpx = SimpleNamespace(
        stream=lambda *args, **kwargs: calls.append(kwargs) or _FakeStream(response),
        Timeout=lambda *args, **kwargs: None,
        ConnectError=OSError,
        TimeoutException=TimeoutError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    translator = OpenAICompatibleTranslator(
        api_key="bad", model="test", base_url=TEST_BASE_URL,
    )

    with pytest.raises(LLMAuthenticationError) as raised:
        translator._call_api("prompt")

    assert raised.value.status_code == 401
    assert len(calls) == 1


def test_provider_public_fields_never_echo_exception_text():
    malicious = (
        "Authorization: Bearer sk-live-public-fields Cookie: session=private; "
        "subtitle=PRIVATE_SUBTITLE_LINE C:\\FixtureHome\\Alice\\secret\\clip.srt"
    )
    error = LLMAuthenticationError(malicious, status_code=401)

    fields = error.public_fields()
    encoded = json.dumps(fields, ensure_ascii=False)

    assert fields["error_code"] == "authentication_error"
    assert fields["upstream_status"] == 401
    for secret in (
        "sk-live-public-fields",
        "session=private",
        "PRIVATE_SUBTITLE_LINE",
        "C:\\FixtureHome\\Alice",
    ):
        assert secret not in encoded


def test_public_translate_never_exposes_streamed_http_400_body(monkeypatch):
    """A provider-controlled error body must not enter public exceptions."""
    import httpx as real_httpx

    calls = []
    response = real_httpx.Response(
        status_code=400,
        request=real_httpx.Request(
            "POST", "https://relay.example/v1/chat/completions",
        ),
        stream=real_httpx.ByteStream(
            b'{"error":{"message":"Bearer leaked-token; private subtitle"}}'
        ),
    )
    fake_httpx = SimpleNamespace(
        stream=lambda *args, **kwargs: (
            calls.append(kwargs) or _FakeStream(response)
        ),
        Timeout=lambda *args, **kwargs: None,
        ConnectError=OSError,
        TimeoutException=TimeoutError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
    )
    prompt_builder = SimpleNamespace(build=lambda *args, **kwargs: "prompt")
    batch = [Subtitle(
        id=1, start="00:00:00,000", end="00:00:01,000", text="hello",
    )]

    with pytest.raises(LLMConfigurationError) as raised:
        translator.translate(batch, prompt_builder=prompt_builder)

    assert raised.value.status_code == 400
    assert "接口拒绝了请求" in str(raised.value)
    assert "leaked-token" not in str(raised.value)
    assert "private subtitle" not in str(raised.value)
    assert len(calls) == 1


def test_reasoning_content_is_never_merged_into_translation(monkeypatch):
    """流式响应若带 reasoning_content，不得混入最终译文。"""
    responses = [
        _FakeStream(_sse_response(lines=[
            'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,'
            '"delta":{"reasoning_content":"内部推理不应出现"},"finish_reason":null}]}',
            'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,'
            '"delta":{"content":"你好"},"finish_reason":null}]}',
            'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,'
            '"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ])),
    ]
    fake_httpx = SimpleNamespace(
        stream=lambda *args, **kwargs: responses.pop(0),
        Timeout=lambda *args, **kwargs: None,
        ConnectError=OSError,
        TimeoutException=TimeoutError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
    )

    text, _, _ = translator._call_api("prompt")

    assert text == "你好"
    assert "推理" not in text and "内部推理" not in text


def test_empty_content_is_retried_like_transient_error(monkeypatch):
    """推理模型偶发'只输出推理、content 为空'时，必须重试同一批次。"""
    calls = []

    def stream(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            # finish_reason=stop 但 content 为空
            return _FakeStream(_sse_response(lines=[
                'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,'
                '"delta":{"role":"assistant"},"finish_reason":null}]}',
                'data: {"id":"x","object":"chat.completion.chunk","choices":[{"index":0,'
                '"delta":{},"finish_reason":"stop"}]}',
                "data: [DONE]",
            ]))
        return _FakeStream(_sse_response(lines=_sse_lines("[1] 重试成功")))

    fake_httpx = SimpleNamespace(
        stream=stream,
        Timeout=lambda *args, **kwargs: None,
        ConnectError=OSError,
        TimeoutException=TimeoutError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
    )

    text, _, _ = translator._call_api("prompt")

    assert text == "[1] 重试成功"
    assert len(calls) == 2


def test_http_client_is_reused_and_preflight_runs_once(monkeypatch):
    clients = []
    client_options = []
    payloads = []

    class Client:
        def __init__(self, **kwargs):
            clients.append(self)
            client_options.append(kwargs)

        def stream(self, *args, **kwargs):
            payloads.append(kwargs.get("json"))
            return _FakeStream(_sse_response(lines=_sse_lines("OK")))

        def close(self):
            pass

    fake_httpx = SimpleNamespace(
        Client=Client,
        Timeout=lambda *args, **kwargs: None,
        ConnectError=OSError,
        TimeoutException=TimeoutError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    translator = OpenAICompatibleTranslator(
        api_key="key",
        model="test",
        base_url=TEST_BASE_URL,
        proxy="http://127.0.0.1:7890",
    )

    translator.preflight()
    translator.preflight()
    translator._call_api("translate")

    assert len(clients) == 1
    assert client_options[0]["proxy"] == "http://127.0.0.1:7890"
    assert client_options[0]["trust_env"] is False
    assert len(payloads) == 2
    assert payloads[0]["max_tokens"] == 128
    assert payloads[0]["stream"] is True


def test_public_translate_retries_stream_that_exceeds_total_deadline(monkeypatch):
    """A chatty SSE stream cannot keep one translation request alive forever."""
    clock = [0.0]
    calls = []

    class DeadlineLines:
        def __iter__(self):
            clock[0] = 0.4
            yield (
                'data: {"id":"x","choices":[{"delta":'
                '{"reasoning_content":"still thinking"},"finish_reason":null}]}'
            )
            clock[0] = 1.1
            yield (
                'data: {"id":"x","choices":[{"delta":'
                '{"reasoning_content":"still thinking"},"finish_reason":null}]}'
            )

    def stream(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return _FakeStream(_sse_response(lines=DeadlineLines()))
        return _FakeStream(_sse_response(lines=_sse_lines("[1] 已恢复")))

    fake_httpx = SimpleNamespace(
        stream=stream,
        Timeout=lambda *args, **kwargs: None,
        ConnectError=OSError,
        TimeoutException=TimeoutError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
        stream_deadline_seconds=1.0,
    )
    prompt_builder = SimpleNamespace(build=lambda *args, **kwargs: "prompt")
    batch = [Subtitle(
        id=1, start="00:00:00,000", end="00:00:01,000", text="hello",
    )]

    results = translator.translate(batch, prompt_builder=prompt_builder)

    assert results[0].translated == "已恢复"
    assert len(calls) == 2


# ============================================================
# O-2：关闭上游 reasoning（thinking）开关与 preflight 探测
# ============================================================


def _capture_httpx(monkeypatch, responses=None):
    """Fake httpx whose stream() records each request payload (kwargs['json']).

    responses 为 None 时每次返回合法 SSE 'OK'；否则按序 pop（用完报错）。
    复用本文件已有的 _FakeStream / _sse_response / _sse_lines 桩。
    """
    payloads = []

    def stream(*args, **kwargs):
        payloads.append(kwargs.get("json"))
        if responses is None:
            return _FakeStream(_sse_response(lines=_sse_lines("OK")))
        return _FakeStream(responses.pop(0))

    fake_httpx = SimpleNamespace(
        stream=stream,
        Timeout=lambda *args, **kwargs: None,
        ConnectError=OSError,
        TimeoutException=TimeoutError,
    )
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    return payloads


def test_disable_thinking_on_injects_thinking_into_payload(monkeypatch):
    """mode=on：强制在请求体注入 thinking={"type":"disabled"}。"""
    payloads = _capture_httpx(monkeypatch)
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
        disable_thinking="on",
    )

    translator._call_api("prompt")

    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert translator.thinking_disabled_report()["effective"] is True


def test_disable_thinking_off_omits_thinking_from_payload(monkeypatch):
    """mode=off：永不注入 thinking（换成真推理模型时保住质量）。"""
    payloads = _capture_httpx(monkeypatch)
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
        disable_thinking="off",
    )

    translator._call_api("prompt")

    assert "thinking" not in payloads[0]
    assert translator.thinking_disabled_report()["effective"] is False


def test_disable_thinking_auto_unprobed_omits_thinking(monkeypatch):
    """mode=auto 且未探测：保守不注入（绝不全局硬开）。"""
    payloads = _capture_httpx(monkeypatch)
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
        disable_thinking="auto",
    )

    translator._call_api("prompt")  # 未跑 preflight → 未探测

    assert "thinking" not in payloads[0]
    report = translator.thinking_disabled_report()
    assert report["mode"] == "auto"
    assert report["effective"] is False
    assert report["probed"] is False


def test_disable_thinking_defaults_to_auto(monkeypatch):
    """不传参数时默认走 auto 探测（env 未设 → 回落 Config 默认 auto）。"""
    monkeypatch.delenv("LLM_DISABLE_THINKING", raising=False)
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
    )

    assert translator._thinking_mode == "auto"
    assert translator.thinking_disabled_report()["effective"] is False


def test_preflight_probe_success_enables_thinking(monkeypatch):
    """auto：探测成功 → 仅一次预检请求、带 thinking、max_tokens=128，后续请求继续带。"""
    payloads = _capture_httpx(monkeypatch)
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
        disable_thinking="auto",
    )

    translator.preflight()

    # 探测请求本身就完成了预检：只发了一次，且带 thinking + max_tokens=128 保护
    assert len(payloads) == 1
    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert payloads[0]["max_tokens"] == 128
    assert translator.thinking_disabled_report() == {
        "mode": "auto", "effective": True, "probed": True,
        "unsupported_reason": None,
    }

    translator._call_api("translate")
    assert payloads[1]["thinking"] == {"type": "disabled"}


def test_preflight_probe_400_downgrades_and_omits_thinking(monkeypatch):
    """auto：探测遇 400/未知参数 → 降级；回退预检与后续真实请求都不带 thinking。"""
    responses = [
        _sse_response(
            status_code=400,
            text='{"error":{"message":"unknown parameter: thinking"}}',
        ),
        _sse_response(lines=_sse_lines("OK")),        # 回退的正常预检
        _sse_response(lines=_sse_lines("[1] 好")),     # 后续真实翻译请求
    ]
    payloads = _capture_httpx(monkeypatch, responses)
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
        disable_thinking="auto",
    )

    translator.preflight()

    # 探测（第 1 次）带 thinking 并被 400 拒绝；回退预检（第 2 次）不带 thinking
    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert "thinking" not in payloads[1]
    report = translator.thinking_disabled_report()
    assert report["effective"] is False
    assert report["probed"] is True
    assert report["unsupported_reason"]  # 记录了站点不支持

    translator._call_api("prompt")
    assert "thinking" not in payloads[2]
    assert len(payloads) == 3


def test_preflight_probe_auth_error_propagates(monkeypatch):
    """auto：探测遇 401/403 鉴权错误必须上抛，不能被误判成'站点不支持 thinking'。"""
    responses = [
        _sse_response(status_code=401, text='{"error":{"message":"bad key"}}'),
    ]
    payloads = _capture_httpx(monkeypatch, responses)
    translator = OpenAICompatibleTranslator(
        api_key="bad", model="test", base_url=TEST_BASE_URL,
        disable_thinking="auto",
    )

    with pytest.raises(LLMAuthenticationError):
        translator.preflight()

    assert translator.thinking_disabled_report()["effective"] is False
    assert len(payloads) == 1  # 鉴权失败即止，不再补发回退预检


# ============================================================
# P0-1：欠费错误归类（quota_error，17→18 公开码）
# ============================================================


def test_403_insufficient_quota_body_raises_quota_error(monkeypatch):
    """403 + insufficient_user_quota body → quota_error，且 body 原文/额度数字不进公开字段。"""
    body = (
        '{"error":{"message":"预扣费额度失败, 用户剩余额度: ＄0.011294, '
        '需要预扣费额度: ＄0.013710","type":"new_api_error",'
        '"code":"insufficient_user_quota"}}'
    )
    payloads = _capture_httpx(
        monkeypatch, [_sse_response(status_code=403, text=body)]
    )
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
    )

    with pytest.raises(LLMQuotaError) as raised:
        translator._call_api("prompt")

    fields = raised.value.public_fields()
    assert fields["error_code"] == "quota_error"
    assert fields["upstream_status"] == 403
    # 红线：上游 body 原文与额度数字绝不进入任何公开字段
    for leaked in ("0.011294", "0.013710", "insufficient_user_quota", "预扣费"):
        assert leaked not in fields["message"]
    assert len(payloads) == 1  # 欠费不可重试，只打一次


def test_403_billing_code_body_raises_quota_error(monkeypatch):
    """403 + "code": "billing"（冒号带空格变体）也归 quota_error。"""
    body = '{"error": {"code": "billing", "message": "balance exhausted"}}'
    payloads = _capture_httpx(
        monkeypatch, [_sse_response(status_code=403, text=body)]
    )
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
    )

    with pytest.raises(LLMQuotaError):
        translator._call_api("prompt")

    assert len(payloads) == 1


def test_429_usage_limit_body_raises_quota_error(monkeypatch):
    """429 + GoUsageLimitError（订阅周限额）→ quota_error：只打一次、不退避、不误报鉴权。"""
    body = (
        '{"type":"error","error":{"type":"GoUsageLimitError",'
        '"message":"Weekly usage limit reached. Resets in 22hr 41min."}}'
    )
    payloads = _capture_httpx(
        monkeypatch, [_sse_response(status_code=429, text=body)]
    )
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
    )

    with pytest.raises(LLMQuotaError) as raised:
        translator._call_api("prompt")

    fields = raised.value.public_fields()
    assert fields["error_code"] == "quota_error"
    assert fields["upstream_status"] == 429
    # 红线：限额详情/重置时间等 body 原文绝不进入公开字段
    for leaked in ("GoUsageLimitError", "22hr", "Weekly"):
        assert leaked not in fields["message"]
    assert len(payloads) == 1  # 限额不可重试：不走 2/5/10/20s 退避阶梯


def test_429_openai_insufficient_quota_body_raises_quota_error(monkeypatch):
    """429 + OpenAI 风格 "code":"insufficient_quota" 同样归 quota_error。"""
    body = (
        '{"error":{"code":"insufficient_quota",'
        '"message":"You exceeded your current quota."}}'
    )
    payloads = _capture_httpx(
        monkeypatch, [_sse_response(status_code=429, text=body)]
    )
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
    )

    with pytest.raises(LLMQuotaError):
        translator._call_api("prompt")
    assert len(payloads) == 1


def test_429_rate_limit_without_quota_signal_stays_transient(monkeypatch):
    """429 普通限流（无额度信号）保持瞬时可重试语义，不误报成欠费。"""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    payloads = _capture_httpx(
        monkeypatch,
        [
            _sse_response(
                status_code=429, text="rate limit exceeded, retry after 5s"
            ),
            _sse_response(
                status_code=429, text="rate limit exceeded, retry after 5s"
            ),
            _sse_response(
                status_code=429, text="rate limit exceeded, retry after 5s"
            ),
        ],
    )
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
    )

    with pytest.raises(LLMTransientError):
        translator._call_api("prompt")

    assert len(payloads) == 3  # 瞬时限流仍走完整退避阶梯


def test_403_without_quota_signal_stays_authentication_error(monkeypatch):
    """403 但无欠费信号 → 维持 authentication_error（回归护栏，不回退现有行为）。"""
    body = '{"error":{"message":"forbidden: invalid scope"}}'
    _capture_httpx(monkeypatch, [_sse_response(status_code=403, text=body)])
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
    )

    with pytest.raises(LLMAuthenticationError):
        translator._call_api("prompt")


def test_401_without_quota_signal_stays_authentication_error(monkeypatch):
    """401 无欠费信号 → 仍是 authentication_error。"""
    _capture_httpx(
        monkeypatch,
        [_sse_response(status_code=401, text='{"error":{"message":"bad key"}}')],
    )
    translator = OpenAICompatibleTranslator(
        api_key="bad", model="test", base_url=TEST_BASE_URL,
    )

    with pytest.raises(LLMAuthenticationError):
        translator._call_api("prompt")


def test_preflight_probe_propagates_quota_error(monkeypatch):
    """auto 探测遇 403 欠费 → 直接上抛 LLMQuotaError，不降级、不补发回退预检。"""
    body = '{"error":{"code":"insufficient_user_quota"}}'
    payloads = _capture_httpx(
        monkeypatch, [_sse_response(status_code=403, text=body)]
    )
    translator = OpenAICompatibleTranslator(
        api_key="key", model="test", base_url=TEST_BASE_URL,
        disable_thinking="auto",
    )

    with pytest.raises(LLMQuotaError):
        translator.preflight()

    assert translator.thinking_disabled_report()["effective"] is False
    assert len(payloads) == 1  # 欠费即止，不再补发不带 thinking 的回退预检


def test_quota_error_is_in_public_error_contract():
    """quota_error 进公开错误契约，_PUBLIC_MESSAGES 锁定 18 码。"""
    from pipeline.safe_errors import _PUBLIC_MESSAGES, normalize_error_code

    assert "quota_error" in _PUBLIC_MESSAGES
    assert len(_PUBLIC_MESSAGES) == 18
    assert normalize_error_code("quota_error") == "quota_error"
    assert normalize_error_code("bogus_code") == "internal_error"


@pytest.fixture(autouse=True)
def _clear_parameter_compatibility_cache(tmp_path, monkeypatch):
    """兼容缓存是模块级全局，跨用例污染会让降级断言失去意义。"""
    monkeypatch.setattr(
        llm_module, "_COMPATIBILITY_CACHE_PATH",
        tmp_path / "provider-compatibility.json",
    )
    _COMPATIBILITY_CACHE.clear()
    yield
    _COMPATIBILITY_CACHE.clear()


def _compatibility_translator(**kwargs):
    kwargs.setdefault("disable_thinking", "off")
    kwargs.setdefault("model", "test")
    return OpenAICompatibleTranslator(
        api_key="key", base_url=TEST_BASE_URL, **kwargs,
    )


@pytest.mark.parametrize("status_code", [400, 422])
def test_parameter_error_degrades_and_retries_without_it(monkeypatch, status_code):
    payloads = _capture_httpx(monkeypatch, responses=[
        _sse_response(
            status_code=status_code,
            text='{"error":{"message":"invalid parameter: temperature '
                 'is not supported by this model"}}',
        ),
        _sse_response(lines=_sse_lines("OK")),
    ])
    translator = _compatibility_translator()

    text, _, _ = translator._call_api("prompt")

    assert text == "OK"
    assert len(payloads) == 2
    assert "temperature" in payloads[0]
    assert "temperature" not in payloads[1]
    # 其余参数不受牵连。
    assert payloads[1]["reasoning_effort"] == "low"
    assert translator.parameter_compatibility_report()["dropped_parameters"] == [
        "temperature",
    ]


def test_parameter_detection_ignores_echoed_request_fields(monkeypatch):
    payloads = _capture_httpx(monkeypatch, responses=[
        _sse_response(
            status_code=400,
            text=(
                '{"error":{"message":"invalid parameter: temperature",'
                '"request":{"thinking":{"type":"disabled"},'
                '"reasoning_effort":"low","stream":true}}}'
            ),
        ),
        _sse_response(lines=_sse_lines("OK")),
    ])
    translator = _compatibility_translator(disable_thinking="on")

    assert translator._call_api("prompt")[0] == "OK"

    assert payloads[1]["thinking"] == {"type": "disabled"}
    assert "temperature" not in payloads[1]
    assert payloads[1]["reasoning_effort"] == "low"


def test_parameter_detection_accepts_explicit_error_param_field(monkeypatch):
    payloads = _capture_httpx(monkeypatch, responses=[
        _sse_response(
            status_code=422,
            text=(
                '{"error":{"message":"unsupported parameter",'
                '"param":"reasoning_effort"}}'
            ),
        ),
        _sse_response(lines=_sse_lines("OK")),
    ])
    translator = _compatibility_translator()

    assert translator._call_api("prompt")[0] == "OK"
    assert "reasoning_effort" in payloads[0]
    assert "reasoning_effort" not in payloads[1]


def test_successful_combination_is_cached_per_endpoint_and_model(monkeypatch):
    first = _capture_httpx(monkeypatch, responses=[
        _sse_response(
            status_code=400,
            text='{"error":{"message":"unsupported parameter temperature"}}',
        ),
        _sse_response(lines=_sse_lines("OK")),
    ])
    _compatibility_translator()._call_api("prompt")
    assert len(first) == 2

    # 同一 endpoint+model 的第二个实例直接用缓存，不再白跑一次被拒请求。
    second = _capture_httpx(monkeypatch)
    _compatibility_translator()._call_api("prompt")

    assert len(second) == 1
    assert "temperature" not in second[0]


def test_successful_compatibility_cache_survives_process_memory_reset():
    translator = _compatibility_translator()
    translator._remember_dropped_params(
        TEST_BASE_URL, frozenset({"temperature", "reasoning_effort"}),
    )
    cache_text = llm_module._COMPATIBILITY_CACHE_PATH.read_text(encoding="utf-8")

    assert TEST_BASE_URL not in cache_text
    assert translator.model not in cache_text

    _COMPATIBILITY_CACHE.clear()
    restored = _compatibility_translator()._cached_dropped_params(TEST_BASE_URL)

    assert restored == frozenset({"temperature", "reasoning_effort"})


def test_corrupt_persisted_compatibility_entry_is_ignored():
    llm_module._COMPATIBILITY_CACHE_PATH.write_text(json.dumps({
        "version": 1,
        "entries": {
            "not-a-secret-fingerprint": {
                "dropped": ["temperature"], "saved_at": "not-a-time",
            },
        },
    }), encoding="utf-8")

    assert _compatibility_translator()._cached_dropped_params(TEST_BASE_URL) == frozenset()


def test_cache_is_scoped_to_the_model_fingerprint(monkeypatch):
    _capture_httpx(monkeypatch, responses=[
        _sse_response(
            status_code=400,
            text='{"error":{"message":"unsupported parameter temperature"}}',
        ),
        _sse_response(lines=_sse_lines("OK")),
    ])
    _compatibility_translator()._call_api("prompt")

    # 换模型等于换指纹：新模型仍然要带上完整参数，不能继承上一个模型的降级。
    payloads = _capture_httpx(monkeypatch)
    _compatibility_translator(model="other-model")._call_api("prompt")

    assert len(payloads) == 1
    assert "temperature" in payloads[0]


def test_failed_degradation_never_populates_the_cache(monkeypatch):
    payloads = _capture_httpx(monkeypatch, responses=[
        _sse_response(
            status_code=400,
            text='{"error":{"message":"unsupported parameter temperature"}}',
        ),
        _sse_response(
            status_code=400,
            text='{"error":{"message":"unsupported parameter reasoning_effort"}}',
        ),
        _sse_response(
            status_code=400,
            text='{"error":{"message":"streaming is not supported here"}}',
        ),
        _sse_response(status_code=400, text='{"error":{"message":"model absent"}}'),
    ])
    translator = _compatibility_translator()

    with pytest.raises(LLMConfigurationError):
        translator._request_api("prompt", attempts=1)

    assert len(payloads) == 4
    assert payloads[3] == {
        "model": "test",
        "max_tokens": translator.max_tokens,
        "messages": [{"role": "user", "content": "prompt"}],
        "stream": False,
    }
    assert _COMPATIBILITY_CACHE == {}


@pytest.mark.parametrize("status_code, body, expected", [
    (401, '{"error":{"message":"invalid api key"}}', LLMAuthenticationError),
    (
        403,
        '{"error":{"code":"billing","message":"insufficient_user_quota"}}',
        LLMQuotaError,
    ),
    (429, '{"error":{"message":"usage limit reached"}}', LLMQuotaError),
    (
        500,
        '{"error":{"message":"temperature subsystem invalid"}}',
        LLMTransientError,
    ),
])
def test_auth_quota_and_server_errors_never_degrade(
    monkeypatch, status_code, body, expected,
):
    """鉴权/欠费/限流/5xx 不是参数兼容问题，降级只会掩盖真正的根因。"""
    payloads = _capture_httpx(monkeypatch, responses=[
        _sse_response(status_code=status_code, text=body),
    ])
    translator = _compatibility_translator()

    with pytest.raises(expected):
        translator._request_api("prompt", attempts=1)

    assert len(payloads) == 1
    assert _COMPATIBILITY_CACHE == {}


def test_400_without_a_named_parameter_keeps_the_configuration_error(monkeypatch):
    payloads = _capture_httpx(monkeypatch, responses=[
        _sse_response(status_code=400, text='{"error":{"message":"model absent"}}'),
    ])
    translator = _compatibility_translator()

    with pytest.raises(LLMConfigurationError) as info:
        translator._request_api("prompt", attempts=1)

    assert len(payloads) == 1
    assert "接口拒绝了请求" in str(info.value)
    assert _COMPATIBILITY_CACHE == {}


def test_stream_degradation_switches_to_the_json_reader(monkeypatch):
    payloads = _capture_httpx(monkeypatch, responses=[
        _sse_response(
            status_code=400,
            text='{"error":{"message":"streaming is not supported by this relay"}}',
        ),
        _sse_response(
            text='{"choices":[{"message":{"content":"[1] 好"}}],'
                 '"usage":{"prompt_tokens":11,"completion_tokens":7}}',
        ),
    ])
    translator = _compatibility_translator()

    text, tokens_in, tokens_out = translator._call_api("prompt")

    assert text == "[1] 好"
    assert (tokens_in, tokens_out) == (11, 7)
    assert payloads[0]["stream"] is True
    assert payloads[1]["stream"] is False
    assert translator.parameter_compatibility_report()["dropped_parameters"] == [
        "stream",
    ]


def test_forced_thinking_still_degrades_on_real_requests(monkeypatch):
    """mode=on 的真实请求也可以降级；只有 thinking 探测请求豁免。"""
    payloads = _capture_httpx(monkeypatch, responses=[
        _sse_response(
            status_code=400,
            text='{"error":{"message":"unknown parameter: thinking"}}',
        ),
        _sse_response(lines=_sse_lines("OK")),
    ])
    translator = _compatibility_translator(disable_thinking="on")

    assert translator._call_api("prompt")[0] == "OK"
    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert "thinking" not in payloads[1]


def test_probe_request_never_degrades_thinking(monkeypatch):
    """探测必须看到原始的 400，否则站点会被误报成"支持 thinking"。"""
    payloads = _capture_httpx(monkeypatch, responses=[
        _sse_response(
            status_code=400,
            text='{"error":{"message":"unknown parameter: thinking"}}',
        ),
        _sse_response(lines=_sse_lines("OK")),
    ])
    translator = _compatibility_translator(disable_thinking="auto")

    translator.preflight()

    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert "thinking" not in payloads[1]
    assert translator.thinking_disabled_report()["effective"] is False


def test_probe_unrelated_400_does_not_disable_thinking_or_retry(monkeypatch):
    """只有正文明确指向 thinking 的 400/422 才能触发兼容降级。"""
    payloads = _capture_httpx(monkeypatch, responses=[
        _sse_response(status_code=400, text='{"error":{"message":"model absent"}}'),
        _sse_response(status_code=400, text='{"error":{"message":"model absent"}}'),
    ])
    translator = _compatibility_translator(disable_thinking="auto")

    with pytest.raises(LLMConfigurationError):
        translator.preflight()

    assert len(payloads) == 1
    assert payloads[0]["thinking"] == {"type": "disabled"}


def test_probe_degrades_other_parameter_without_dropping_thinking(monkeypatch):
    payloads = _capture_httpx(monkeypatch, responses=[
        _sse_response(
            status_code=422,
            text='{"error":{"message":"unsupported parameter temperature"}}',
        ),
        _sse_response(lines=_sse_lines("OK")),
    ])
    translator = _compatibility_translator(disable_thinking="auto")

    translator.preflight()

    assert len(payloads) == 2
    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert "temperature" in payloads[0]
    assert payloads[1]["thinking"] == {"type": "disabled"}
    assert "temperature" not in payloads[1]
    assert translator.thinking_disabled_report()["effective"] is True
    assert translator.parameter_compatibility_report()["dropped_parameters"] == [
        "temperature",
    ]


def test_upstream_body_never_reaches_public_error_fields(monkeypatch):
    payloads = _capture_httpx(monkeypatch, responses=[
        _sse_response(
            status_code=400,
            text='{"error":{"message":"unsupported parameter temperature",'
                 '"detail":"sk-live-SECRETVALUE"}}',
        ),
        _sse_response(status_code=400, text='{"error":{"message":"model absent"}}'),
    ])
    translator = _compatibility_translator()

    with pytest.raises(LLMConfigurationError) as info:
        translator._request_api("prompt", attempts=1)

    assert len(payloads) == 2
    message = str(info.value)
    fields = json.dumps(info.value.public_fields(), ensure_ascii=False)
    assert "sk-live-SECRETVALUE" not in message + fields
    assert "unsupported parameter" not in message + fields


def test_degradation_order_puts_stream_last():
    """丢掉 stream 要换非流式读法，所以它必须排在降级链最后。"""
    assert PARAMETER_DEGRADATION_ORDER[-1] == "stream"
    assert PARAMETER_DEGRADATION_ORDER[0] == "thinking"


if __name__ == "__main__":
    print("=" * 60)
    print("LLM Translator 测试套件")
    print("=" * 60)

    tests = [
        ("工厂函数创建", test_factory_create),
        ("工厂函数支持base_url", test_factory_with_base_url),
        ("默认模型", test_default_model),
        ("后端名称", test_name),
        ("响应解析-正常", test_parse_response_normal),
        ("响应解析-漏翻", test_parse_response_missing_item),
        ("无API key报错", test_no_api_key_error),
        ("TranslateResult字段", test_translate_result_dataclass),
        ("环境变量读取key", test_env_var_api_key),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"[FAIL] {name}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败, {passed+failed} 总计")
    sys.exit(0 if failed == 0 else 1)
