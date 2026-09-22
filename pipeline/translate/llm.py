"""
LLM Translator — 统一翻译接口

职责：
- 定义 translate(batch) 统一接口
- 通过 OpenAI 兼容协议调用任意 LLM 后端
- 用户只需提供 base_url + api_key + model 即可适配所有厂商

使用方式：
    from translate.llm import create_translator

    translator = create_translator(api_key="...", model="gpt-4o", base_url="https://api.openai.com/v1")
    results = translator.translate(batch, glossary={"Changli": "长离"})
    # → [(subtitle_id, "翻译后的中文"), ...]
"""
import json
import hashlib
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pipeline.safe_errors import public_error_fields
from pipeline.translate.prompt_builder import PromptBuilder, load_prompt_builder


class LLMAPIError(RuntimeError):
    """Base error for provider, authentication, and transport failures."""

    code = "api_error"

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    def public_fields(self) -> dict[str, object]:
        """Return only locally generated fields safe for logs and manifests."""
        return public_error_fields(
            self.code,
            self.status_code,
            fallback_code="api_error",
        )


class LLMAuthenticationError(LLMAPIError):
    """The provider rejected the supplied credentials or permissions."""

    code = "authentication_error"


class LLMConfigurationError(LLMAPIError):
    """The endpoint, model, or local API configuration is invalid."""

    code = "configuration_error"


class LLMTransientError(LLMAPIError):
    """A retryable provider or network failure exhausted local retries."""

    code = "provider_unavailable"


class LLMResponseError(LLMAPIError):
    """The provider returned a successful but unusable response."""

    code = "invalid_provider_response"


class LLMQuotaError(LLMAPIError):
    """The provider rejected the request because the account balance is exhausted.

    部分中转站（new-api 系）用 HTTP 403 + body `insufficient_user_quota` 表达
    余额不足，与"密钥无效"是两回事，且不可重试——必须靠独立错误码区分，
    否则自动续跑会对一个欠费任务无限重试。
    """

    code = "quota_error"


_QUOTA_BODY_SIGNALS = (
    "insufficient_user_quota",  # new-api 系中转站（HTTP 403）
    "insufficient_quota",       # OpenAI 风格配额（HTTP 429）
    "usagelimiterror",          # opencode GoUsageLimitError 等订阅限额
    "usage limit reached",
    "usage limit exceeded",
)


def _is_quota_error_body(body: str) -> bool:
    """Detect provider quota/billing exhaustion signals in an upstream error body.

    只认精确信号，避免把普通限流或鉴权错误误报成欠费；仅做布尔判定，
    body 原文绝不进入任何公开字段（异常 message 用固定本地文案）。
    """
    if not body:
        return False
    folded = body.casefold()
    if any(signal in folded for signal in _QUOTA_BODY_SIGNALS):
        return True
    return bool(re.search(r'"code"\s*:\s*"billing"', body, re.IGNORECASE))


class _ParameterCompatibilityError(LLMConfigurationError):
    """Internal: a 400/422 whose body explicitly blames one optional parameter.

    message 是本地固定文案，上游 body 原文绝不进入任何公开字段；被拒的参数
    名只放在 ``parameter`` 属性里，供进程内的降级循环使用。
    """

    def __init__(self, parameter: str, status_code: int):
        super().__init__(
            "API 拒绝了请求中的一个可选参数", status_code=status_code,
        )
        self.parameter = parameter


# 参数兼容降级链。顺序即可选程度：推理相关参数最先丢，stream 最后丢
# （丢掉它就必须改用非流式读法）。
PARAMETER_DEGRADATION_ORDER = (
    "thinking", "reasoning_effort", "temperature", "stream",
)
_PARAMETER_COMPATIBILITY_STATUSES = frozenset({400, 422})
# 必须同时命中「拒绝措辞」与「参数名」才降级：只看参数名会把内容策略类 400
# （body 里回显了整个请求体）误判成参数不兼容，白白改掉翻译参数。
_PARAMETER_REJECTION_MARKERS = (
    "unsupported", "not supported", "unknown parameter", "unknown field",
    "unrecognized", "unexpected", "invalid", "not allowed",
    "不支持", "未知参数", "无效",
)
_PARAMETER_NAME_PATTERNS = {
    "thinking": re.compile(r"(?<![a-z])thinking(?![a-z])", re.IGNORECASE),
    "reasoning_effort": re.compile(
        r"(?<![a-z])reasoning[_\s-]?effort(?![a-z])", re.IGNORECASE,
    ),
    "temperature": re.compile(r"(?<![a-z])temperature(?![a-z])", re.IGNORECASE),
    "stream": re.compile(r"(?<![a-z])stream(?:ing)?(?![a-z])", re.IGNORECASE),
}
# 成功组合按 base_url|model 指纹做进程内缓存。只缓存成功：失败组合一旦被
# 复用，就会让后续请求凭空少发一个本来支持的参数。
_COMPATIBILITY_CACHE: Dict[str, frozenset] = {}
_COMPATIBILITY_CACHE_LOCK = threading.Lock()
_COMPATIBILITY_CACHE_MAX_ENTRIES = 64
_COMPATIBILITY_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
_COMPATIBILITY_CACHE_PATH = Path(
    os.getenv("SUBTITLE_PROVIDER_COMPATIBILITY_CACHE", "")
    or Path(__file__).resolve().parents[2] / "data" / "provider_compatibility.json"
)


def _read_persisted_compatibility_cache() -> dict[str, frozenset]:
    try:
        document = json.loads(_COMPATIBILITY_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if document.get("version") != 1 or not isinstance(document.get("entries"), dict):
        return {}
    cutoff = time.time() - _COMPATIBILITY_CACHE_TTL_SECONDS
    result: dict[str, frozenset] = {}
    for fingerprint, entry in document["entries"].items():
        if not isinstance(entry, dict):
            continue
        try:
            saved_at = float(entry.get("saved_at", 0) or 0)
        except (TypeError, ValueError):
            continue
        if saved_at < cutoff:
            continue
        dropped = entry.get("dropped")
        if not isinstance(dropped, list):
            continue
        names = frozenset(str(name) for name in dropped)
        if names <= set(PARAMETER_DEGRADATION_ORDER):
            result[str(fingerprint)] = names
    return result


def _write_persisted_compatibility_cache() -> None:
    path = _COMPATIBILITY_CACHE_PATH
    temporary = path.with_suffix(path.suffix + ".tmp")
    entries = {
        fingerprint: {
            "dropped": [
                name for name in PARAMETER_DEGRADATION_ORDER if name in dropped
            ],
            "saved_at": time.time(),
        }
        for fingerprint, dropped in list(_COMPATIBILITY_CACHE.items())[
            -_COMPATIBILITY_CACHE_MAX_ENTRIES:
        ]
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps({"version": 1, "entries": entries}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _parameter_rejected_by_body(body: str) -> str | None:
    """Return the optional parameter a 400/422 body explicitly blames."""
    raw_text = str(body or "")
    if not raw_text:
        return None

    # Many relays echo the entire rejected request beside error.message. Looking
    # for names in the whole JSON would then pick the first parameter in our
    # degradation order, not the parameter the message actually blamed.
    texts = [raw_text]
    try:
        document = json.loads(raw_text)
    except (TypeError, ValueError):
        pass
    else:
        if isinstance(document, dict):
            error = document.get("error", document)
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"]
                parameter = error.get("param", error.get("parameter"))
                if isinstance(parameter, str):
                    message = f"{message} {parameter}"
                texts = [message]
            elif isinstance(document.get("message"), str):
                texts = [document["message"]]

    for text in texts:
        folded = text.casefold()
        if not any(marker in folded for marker in _PARAMETER_REJECTION_MARKERS):
            continue
        for name in PARAMETER_DEGRADATION_ORDER:
            if _PARAMETER_NAME_PATTERNS[name].search(text):
                return name
    return None


@dataclass
class TranslateResult:
    """单条翻译结果"""
    id: int               # 对应的字幕 id
    original: str         # 原文
    translated: str       # 译文
    tokens_in: int = 0    # 输入 token 数
    tokens_out: int = 0   # 输出 token 数
    cost: float = 0.0     # 费用（美元）
    cached: bool = False  # 是否命中缓存


class BaseTranslator(ABC):
    """
    翻译器抽象基类。

    所有 LLM 后端实现此接口，确保管道代码无需因后端切换而改动。
    子类需要实现的唯一方法：_call_api(prompt) -> str
    """

    def __init__(self, api_key: str = None, model: str = None,
                 temperature: float = 0.3, max_tokens: int = 4096,
                 base_url: str = None, proxy: str = None,
                 stream_deadline_seconds: float = None,
                 disable_thinking: str | bool | None = None):
        self.api_key = api_key or os.getenv("LLM_API_KEY", "")
        self.model = model or self.default_model
        self.temperature = temperature
        self.max_tokens = max_tokens
        from pipeline.config import normalize_llm_base_url
        configured_base_url = (
            os.getenv("LLM_BASE_URL", "") if base_url is None else base_url
        )
        self.base_url = normalize_llm_base_url(
            configured_base_url, required=False
        )
        self.proxy = proxy or os.getenv("LLM_PROXY", "")
        # 2026-09-03 实测：opencode.ai/zen/go 网关对 deepseek-v4-flash 慢窗口
        # 单请求可拖到 2-3 分钟。默认总时限从 180s 放宽到 300s，配合下方
        # httpx 读超时 300s，让慢窗口请求能完整返回而不误判 provider_unavailable。
        configured_deadline = (
            stream_deadline_seconds
            if stream_deadline_seconds is not None
            else os.getenv("LLM_STREAM_DEADLINE_SECONDS", "300")
        )
        try:
            self.stream_deadline_seconds = float(configured_deadline)
        except (TypeError, ValueError) as exc:
            raise ValueError("stream_deadline_seconds 必须是正数") from exc
        if self.stream_deadline_seconds <= 0:
            raise ValueError("stream_deadline_seconds 必须是正数")

        # O-2：thinking（上游 reasoning）关闭开关。模式来源优先级：
        # 显式参数 > 环境变量 LLM_DISABLE_THINKING > Config 默认（auto）。
        # auto = 由 preflight() 探测站点是否支持 thinking={"type":"disabled"}，
        # 支持才注入；on = 强制注入；off = 永不注入。生效值（effective）只有在
        # 探测确认支持、或模式为 on 时才为 True——绝不全局硬开。
        from pipeline.config import Config, normalize_disable_thinking
        if disable_thinking is None:
            disable_thinking = os.getenv(
                "LLM_DISABLE_THINKING", Config.LLM_DISABLE_THINKING
            )
        self._thinking_mode = normalize_disable_thinking(disable_thinking)
        self._thinking_disabled_effective = self._thinking_mode == "on"
        self._thinking_probe_done = False
        self._thinking_unsupported_reason: str | None = None

    @property
    @abstractmethod
    def default_model(self) -> str:
        """返回默认模型名"""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """返回后端名称"""
        ...

    @abstractmethod
    def _call_api(self, prompt: str) -> Tuple[str, int, int]:
        """
        调用 LLM API。

        Returns:
            (response_text, tokens_in, tokens_out)

        Raises:
            RuntimeError: API 调用失败
        """
        ...

    # ---- 公共接口 ----

    def translate(self,
                  batch: list,
                  glossary: Dict[str, str] = None,
                  prompt_builder: PromptBuilder = None,
                  full_glossary: Dict[str, str] = None,
                  all_subs: list = None,
                  established_terms: Dict[str, str] = None,
                  refine_pairs: list = None,
                  global_context: dict = None,
                  translation_memory: list[dict] = None) -> List[TranslateResult]:
        """
        翻译一个字幕 batch。

        Args:
            batch: Subtitle 对象列表
            glossary: 术语映射表
            prompt_builder: PromptBuilder 实例（默认自动加载）
            full_glossary: 完整术语表（注入 prompt 以确保译名准确）
            all_subs: 全量字幕列表（用于提供上下文）
            established_terms: 已在前序批次中锁定译名的术语→译名映射
            refine_pairs: 精校模式下的 (英文, 参考译文) 配对列表
            global_context: 视频标题、频道、游戏和全片术语记忆
            translation_memory: 仅包含用户明确批准的历史译法

        Returns:
            TranslateResult 列表，顺序与输入 batch 一致
        """
        glossary = glossary or {}

        if prompt_builder is None:
            prompt_builder = load_prompt_builder()
        prompt = prompt_builder.build(batch, glossary,
                                      full_glossary=full_glossary,
                                      all_subs=all_subs,
                                      established_terms=established_terms,
                                      refine_pairs=refine_pairs,
                                      global_context=global_context,
                                      translation_memory=translation_memory)

        response_text, tokens_in, tokens_out = self._call_api(prompt)
        results = self._parse_response(response_text, batch)
        cost = self._estimate_cost(tokens_in, tokens_out)

        for r in results:
            r.tokens_in = tokens_in
            r.tokens_out = tokens_out // max(len(results), 1)
            r.cost = cost / max(len(results), 1)

        return results

    def translate_single(self,
                         subtitle,
                         glossary: Dict[str, str] = None) -> TranslateResult:
        """翻译单条字幕"""
        results = self.translate([subtitle], glossary=glossary)
        return results[0] if results else None

    def last_call_stats(self) -> dict:
        """最近一次 translate() 的传输层统计（线程本地，并发安全）。

        PERF-P0-03 / S-3：语义补译循环需要知道每次调用实际发出了几次
        HTTP 请求（传输重试会计数），用于批次 API 尝试预算与可观测性。
        未实现统计的子类（测试桩）自然返回 0。
        """
        tls = getattr(self, "_call_stats_tls", None)
        if tls is None:
            return {"transport_attempts": 0}
        return {"transport_attempts": getattr(tls, "transport_attempts", 0)}

    def _note_transport_attempt(self) -> None:
        tls = getattr(self, "_call_stats_tls", None)
        if tls is None:
            tls = self._call_stats_tls = threading.local()
        tls.transport_attempts = getattr(tls, "transport_attempts", 0) + 1

    # ---- 内部方法 ----

    def _parse_response(self, response: str, batch: list) -> List[TranslateResult]:
        """
        解析 LLM 响应，提取每条字幕的翻译。
        按 "[序号] 翻译" 格式解析。
        """
        import re

        # Build a lookup: id => subtitle for fast matching
        sub_by_id = {sub.id: sub for sub in batch}
        found = {}

        # Allow flexible patterns:
        # - [ 160 ]
        # - [160]
        # - [ID:160]
        # - [160]: xxx
        pattern = re.compile(r'^\s*\[(\s*\d+\s*)\][\s:：]*(.*)$')

        for line in response.strip().split('\n'):
            m = pattern.match(line)
            if m:
                id_str, text = m.groups()
                try:
                    sid = int(id_str.strip())
                    if sid in sub_by_id:
                        found[sid] = text.strip()
                except ValueError:
                    continue

        results = []
        for sub in batch:
            translated = found.get(sub.id, "")
            results.append(TranslateResult(
                id=sub.id,
                original=sub.text,
                translated=translated,
            ))
        return results

    def _estimate_cost(self, tokens_in: int, tokens_out: int) -> float:
        """估算 API 调用费用，子类可覆写。"""
        return 0.0


# ============================================================
# 统一后端：OpenAI 兼容协议
# ============================================================


class OpenAICompatibleTranslator(BaseTranslator):
    """OpenAI 兼容协议翻译后端。

    通过 base_url + api_key + model 三参数适配所有提供
    OpenAI 兼容接口的 LLM 厂商（OpenAI / DeepSeek / 智谱 /
    Gemini OpenAI 兼容层 / 中转站等）。
    """

    # 部分 Cloudflare 防护的中转站需要浏览器 UA
    _BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._http_client = None
        self._client_lock = threading.Lock()
        self._preflight_complete = False
        self._last_dropped_params: frozenset = frozenset()

    @property
    def default_model(self) -> str:
        return os.getenv("LLM_MODEL", "").strip()

    @property
    def name(self) -> str:
        return "openai-compatible"

    def _call_api(self, prompt: str) -> Tuple[str, int, int]:
        return self._request_api(prompt)

    def preflight(self) -> None:
        """Verify the key, endpoint, and model before concurrent work starts.

        O-2：auto 模式下先探测站点是否支持 thinking={"type":"disabled"}。
        探测本身就是一次合法的超小预检请求，成功即完成预检；只有探测被
        400/未知参数拒绝时才降级并补一次不带 thinking 的正常预检。
        """
        if self._preflight_complete:
            return
        if self._thinking_mode == "auto" and not self._thinking_probe_done:
            if self._probe_thinking_support():
                self._preflight_complete = True
                return
        self._request_api(
            "Reply with exactly: OK",
            # 推理模型（reasoning）也会占用输出 token；2 太小会被
            # reasoning 吃光导致 content 为空，预检误判失败。
            max_tokens=128,
            attempts=1,
        )
        self._preflight_complete = True

    def _probe_thinking_support(self) -> bool:
        """Send one tiny request with thinking disabled to probe site support.

        返回 True 表示站点接受该参数（探测请求同时完成了预检）；返回 False
        表示已降级为不注入，调用方需补一次正常预检。鉴权失败（401/403）直接
        上抛——那是真正的密钥/权限问题，不能被误判成"站点不支持 thinking"。

        探测请求刻意不参与通用参数兼容降级：否则降级链会在探测内部悄悄丢掉
        thinking 并重试成功，本站点就会被误报成"支持 thinking"。
        """
        self._thinking_probe_done = True
        try:
            self._request_api(
                "Reply with exactly: OK",
                max_tokens=128,
                attempts=1,
                _force_thinking_disabled=True,
                # 预检仍可降级其他被明确拒绝的参数；thinking 自身必须把
                # 原始拒绝交给本方法判定，不能在循环里悄悄丢掉。
                _protected_params=frozenset({"thinking"}),
            )
        except (LLMAuthenticationError, LLMQuotaError):
            self._thinking_disabled_effective = False
            raise
        except _ParameterCompatibilityError as exc:
            # 只有正文明确点名 thinking 的 400/422 才能证明站点不支持它。
            if exc.parameter != "thinking":
                raise
            self._thinking_disabled_effective = False
            self._thinking_unsupported_reason = (
                f"API {exc.status_code}: thinking parameter is unsupported"
            )
            return False
        except LLMConfigurationError:
            # 模型名、接口地址、上下文限制等 400/422 与 thinking 兼容性无关；
            # 不得静默关闭参数并补发请求。
            self._thinking_disabled_effective = False
            raise
        except LLMAPIError:
            # 瞬时/响应类错误：不能据此判定站点支持与否，保守置 False，
            # 由随后的正常预检暴露真正的失败原因。
            self._thinking_disabled_effective = False
            return False
        self._thinking_disabled_effective = True
        self._thinking_unsupported_reason = None
        return True

    def _should_send_thinking_disabled(self) -> bool:
        """Whether _request_api should inject thinking={"type":"disabled"}."""
        if self._thinking_mode == "on":
            return True
        if self._thinking_mode == "off":
            return False
        return bool(self._thinking_disabled_effective)

    def thinking_disabled_report(self) -> dict:
        """生效值报告，写入 manifest options 便于复现（不含任何密钥）。"""
        return {
            "mode": self._thinking_mode,
            "effective": bool(self._thinking_disabled_effective),
            "probed": bool(self._thinking_probe_done),
            "unsupported_reason": self._thinking_unsupported_reason,
        }

    def parameter_compatibility_report(self) -> dict:
        """本站点当前丢掉了哪些可选参数（不含密钥，也不含上游正文）。

        降级会静默改变请求参数（例如丢掉 temperature 让上游改用默认值），
        所以必须可检视。
        """
        return {
            "dropped_parameters": sorted(self._last_dropped_params),
            "degradation_order": list(PARAMETER_DEGRADATION_ORDER),
        }

    def close(self) -> None:
        client = self._http_client
        self._http_client = None
        if client is not None:
            client.close()

    def _post(self, httpx, url: str, headers: dict, payload: dict, *, stream: bool = False):
        """Use a shared connection pool, with a test-compatible fallback.

        stream=True 返回 httpx 流式上下文管理器（调用方负责 with）。

        读超时 300s：opencode.ai/zen/go 网关慢窗口单请求可达 2-3 分钟，
        原 120s 读超时会中途掐断导致 provider_unavailable（2026-09-03 实测）。
        """
        proxy_options = {"proxy": self.proxy} if self.proxy else {}
        client_type = getattr(httpx, "Client", None)
        if client_type is None:
            if stream:
                return httpx.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                    timeout=httpx.Timeout(300.0, connect=10.0),
                    trust_env=False,
                    **proxy_options,
                )
            return httpx.post(
                url,
                headers=headers,
                json=payload,
                timeout=httpx.Timeout(300.0, connect=10.0),
                trust_env=False,
                **proxy_options,
            )
        if self._http_client is None:
            with self._client_lock:
                if self._http_client is None:
                    self._http_client = client_type(
                        timeout=httpx.Timeout(300.0, connect=10.0),
                        trust_env=False,
                        **proxy_options,
                    )
        if stream:
            return self._http_client.stream(
                "POST", url, headers=headers, json=payload,
            )
        return self._http_client.post(url, headers=headers, json=payload)

    @staticmethod
    def _read_body(resp) -> str:
        """Read a response body that may still be streamed.

        流式响应必须先 read() 才能拿到正文（否则访问 text 触发
        ResponseNotRead）；读取失败一律按空 body 处理。
        """
        try:
            read = getattr(resp, "read", None)
            if callable(read):
                read()
            return str(getattr(resp, "text", "") or "")
        except Exception:
            return ""

    @classmethod
    def _read_error_body(cls, resp) -> str:
        """Return a bounded upstream error body for local classification only.

        body 只用于 _is_quota_error_body / _parameter_rejected_by_body 的本地
        布尔判定，绝不进入任何公开字段。成功正文走 _read_body，不能截断。
        """
        return cls._read_body(resp)[:4000]

    def _request_api(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        attempts: int = 3,
        _force_thinking_disabled: bool = False,
        _allow_param_degradation: bool = True,
        _protected_params: frozenset[str] = frozenset(),
    ) -> Tuple[str, int, int]:
        """直接用 httpx 调用 OpenAI 兼容 API，绕过 openai SDK 的编码问题。

        站点若用 400/422 明确拒绝某个可选参数，就丢掉它再试一次（顺序见
        PARAMETER_DEGRADATION_ORDER）；成功的组合按 base_url|model 指纹缓存，
        后续请求直接少发那个参数，不再白跑一次注定被拒的请求。鉴权、欠费、
        限流与 5xx 一律不触发降级——那些不是参数兼容问题。
        """
        if not self.api_key:
            raise LLMConfigurationError(
                "API key 未设置 (设置环境变量 LLM_API_KEY 或通过参数传入)"
            )

        import httpx

        from pipeline.config import normalize_llm_base_url
        try:
            effective_base_url = normalize_llm_base_url(
                self.base_url, required=True
            )
        except ValueError as error:
            raise LLMConfigurationError(str(error)) from None
        url = effective_base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": self._BROWSER_UA,
        }

        # 传输层计数（线程本地）：供 last_call_stats() / 尝试预算使用。
        # 降级重试也是真实的 API 调用，所以计数只在整个降级循环外清零一次。
        tls = getattr(self, "_call_stats_tls", None)
        if tls is None:
            tls = self._call_stats_tls = threading.local()
        tls.transport_attempts = 0

        dropped = self._cached_dropped_params(effective_base_url)
        while True:
            payload = self._build_payload(
                prompt, max_tokens, dropped, _force_thinking_disabled,
            )
            try:
                result = self._attempt_request(
                    httpx, url, headers, payload, attempts, tls,
                )
            except _ParameterCompatibilityError as exc:
                if (
                    not _allow_param_degradation
                    or exc.parameter in _protected_params
                ):
                    raise
                if exc.parameter in dropped:
                    raise LLMConfigurationError(
                        f"API {exc.status_code}: 接口拒绝了请求，请检查模型名称、"
                        "接口地址或上下文限制",
                        status_code=exc.status_code,
                    ) from None
                dropped = dropped | {exc.parameter}
                continue
            self._last_dropped_params = dropped
            if dropped:
                self._remember_dropped_params(effective_base_url, dropped)
            return result

    def _build_payload(
        self, prompt: str, max_tokens: int | None, dropped: frozenset,
        force_thinking_disabled: bool,
    ) -> dict:
        """Build one request body, minus whichever parameters the site rejected."""
        payload = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            # 推理模型（deepseek-v* 等）默认 reasoning 档位波动极大：
            # 长 prompt 下偶发 reasoning 占满输出、content 为空（概率性）。
            # 实测：low 档 4/4 稳定出内容（7-13s）；high 档 2/3；基线 40-60s 且不稳。
            "reasoning_effort": "low",
        }
        # O-2：cun.ai 网关只认 thinking={"type":"disabled"}（reasoning_effort 被
        # 忽略）。auto 模式经 preflight 探测确认支持后才注入；on 强制注入；
        # off 永不注入。探测请求用 _force_thinking_disabled 强制带上该参数。
        if force_thinking_disabled or self._should_send_thinking_disabled():
            payload["thinking"] = {"type": "disabled"}
        for name in dropped:
            if name == "stream":
                # 丢掉 stream 等于改用非流式读法，必须显式声明而不是删键。
                payload["stream"] = False
            else:
                payload.pop(name, None)
        return payload

    def _compatibility_key(self, base_url: str) -> str:
        """Fingerprint an endpoint+model pair without persisting credentials."""
        raw = f"{base_url}|{self.model}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _cached_dropped_params(self, base_url: str) -> frozenset:
        """Return the parameters this endpoint already proved it cannot take."""
        with _COMPATIBILITY_CACHE_LOCK:
            key = self._compatibility_key(base_url)
            if key not in _COMPATIBILITY_CACHE:
                _COMPATIBILITY_CACHE.update(_read_persisted_compatibility_cache())
            return _COMPATIBILITY_CACHE.get(key, frozenset())

    def _remember_dropped_params(self, base_url: str, dropped: frozenset) -> None:
        """Cache a combination that actually worked.

        只缓存成功：失败组合一旦被复用，后续请求就会凭空少发一个本来支持的
        参数（例如白白丢掉 temperature，让译文风格漂移）。
        """
        with _COMPATIBILITY_CACHE_LOCK:
            if len(_COMPATIBILITY_CACHE) >= _COMPATIBILITY_CACHE_MAX_ENTRIES:
                _COMPATIBILITY_CACHE.clear()
            _COMPATIBILITY_CACHE[self._compatibility_key(base_url)] = dropped
            _write_persisted_compatibility_cache()

    def _attempt_request(
        self, httpx, url: str, headers: dict, payload: dict,
        attempts: int, tls,
    ) -> Tuple[str, int, int]:
        """Run the transport retry loop against one fixed payload."""
        # 单批最多 4 次重试：2s / 5s / 10s / 20s 退避。
        # 只重试当前失败批次；已成功批次由 manifest 复用，不会重复请求。
        retry_delays = (2.0, 5.0, 10.0, 20.0)
        attempts = max(1, int(attempts))
        last_err: Exception | None = None
        for attempt in range(attempts):
            tls.transport_attempts += 1
            delay = retry_delays[min(attempt, len(retry_delays) - 1)]
            try:
                with self._post(httpx, url, headers, payload, stream=True) as resp:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        if resp.status_code == 429:
                            # 订阅/账户限额常以 429 表达（如 GoUsageLimitError），
                            # 与可重试的瞬时限流是两回事：归类为 quota_error
                            # 后自动续跑与传输退避都不再白白重试。
                            body = self._read_error_body(resp)
                            if _is_quota_error_body(body):
                                raise LLMQuotaError(
                                    "API 429: 账户/订阅额度已用尽，"
                                    "请到服务商后台确认重置时间或充值",
                                    status_code=429,
                                )
                        last_err = LLMTransientError(
                            f"API {resp.status_code}: 上游网关错误",
                            status_code=resp.status_code,
                        )
                        if attempt >= attempts - 1:
                            raise last_err
                        time.sleep(delay)
                        continue
                    if resp.status_code in {401, 403}:
                        body = self._read_error_body(resp)
                        if _is_quota_error_body(body):
                            raise LLMQuotaError(
                                f"API {resp.status_code}: 账户额度不足，请充值后重试",
                                status_code=resp.status_code,
                            )
                        raise LLMAuthenticationError(
                            f"API {resp.status_code}: API Key 无效、已过期或无权访问当前模型",
                            status_code=resp.status_code,
                        )
                    if resp.status_code != 200:
                        if resp.status_code in _PARAMETER_COMPATIBILITY_STATUSES:
                            rejected = _parameter_rejected_by_body(
                                self._read_error_body(resp)
                            )
                            if rejected is not None:
                                raise _ParameterCompatibilityError(
                                    rejected, resp.status_code,
                                )
                        raise LLMConfigurationError(
                            f"API {resp.status_code}: 接口拒绝了请求，请检查模型名称、"
                            "接口地址或上下文限制",
                            status_code=resp.status_code,
                        )

                    if payload.get("stream", True):
                        return self._consume_sse(resp)
                    # stream 被降级掉之后上游返回的是一整个 JSON，不是 SSE。
                    return self._consume_json(resp)

            except (httpx.ConnectError, httpx.TimeoutException, ConnectionError, OSError) as exc:
                last_err = exc
                if attempt >= attempts - 1:
                    raise LLMTransientError(
                        "API 连接失败，请检查接口地址、代理和网络"
                    ) from exc
                time.sleep(delay)
                continue
            except LLMTransientError as exc:
                last_err = exc
                if attempt >= attempts - 1:
                    raise
                time.sleep(delay)
                continue
            except LLMAPIError:
                raise
            except Exception as exc:
                last_err = exc
                if attempt >= attempts - 1:
                    raise LLMResponseError(
                        "API 响应格式无效或流式读取失败"
                    ) from exc
                time.sleep(delay)
                continue

        status_code = (
            last_err.status_code if isinstance(last_err, LLMAPIError) else None
        )
        raise LLMTransientError(
            "API 调用失败，请稍后重试", status_code=status_code
        ) from last_err

    def _consume_sse(self, resp) -> Tuple[str, int, int]:
        """Consume an OpenAI-compatible SSE stream and join content deltas.

        - 只拼接 delta.content；reasoning_content（如果出现）绝不写入译文。
        - 流中途断开（未收到 [DONE] / finish_reason）视为失败，
          残缺内容不会被标记为成功，调用方会重试整个批次。
        - usage 从带 usage 字段的 chunk 提取（可能在末尾或单独 chunk）。
        """
        parts: list[str] = []
        tokens_in = tokens_out = 0
        finished = False
        started_at = time.monotonic()
        for line in resp.iter_lines():
            if time.monotonic() - started_at > self.stream_deadline_seconds:
                raise LLMTransientError(
                    "API 流式响应超过总时限 "
                    f"({self.stream_deadline_seconds:g} 秒)"
                )
            if not line:
                continue
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                finished = True
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                tokens_in = int(usage.get("prompt_tokens", tokens_in) or tokens_in)
                tokens_out = int(usage.get("completion_tokens", tokens_out) or tokens_out)
            choices = chunk.get("choices")
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
            if choice.get("finish_reason"):
                # 记录完成，但不 break：usage chunk / [DONE] 仍在流末尾
                finished = True
        if not finished:
            raise LLMTransientError("API 流式响应中断：未收到结束标记")
        text = "".join(parts)
        if not text.strip():
            # 推理模型偶尔只输出 reasoning、content 为空（概率性，非必现）。
            # 视作可重试的瞬时错误，重试同一批次。
            raise LLMTransientError("API 返回空内容（推理模型可能只输出了推理）")
        return text, tokens_in, tokens_out

    def _consume_json(self, resp) -> Tuple[str, int, int]:
        """Read a non-streaming body, used once ``stream`` has been dropped."""
        try:
            document = json.loads(self._read_body(resp))
        except ValueError as exc:
            raise LLMResponseError("API 响应格式无效或流式读取失败") from exc
        if not isinstance(document, dict):
            raise LLMResponseError("API 响应格式无效或流式读取失败")
        tokens_in = tokens_out = 0
        usage = document.get("usage")
        if isinstance(usage, dict):
            tokens_in = int(usage.get("prompt_tokens", 0) or 0)
            tokens_out = int(usage.get("completion_tokens", 0) or 0)
        text = ""
        choices = document.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                text = message["content"]
        if not text.strip():
            # 与 SSE 路径一致：推理模型可能只输出 reasoning、content 为空。
            raise LLMTransientError("API 返回空内容（推理模型可能只输出了推理）")
        return text, tokens_in, tokens_out


# ============================================================
# 工厂函数
# ============================================================


def thinking_policy_report(translator) -> dict:
    """O-2 生效值报告，供 manifest options 记录（不含任何密钥）。

    生产环境的 OpenAICompatibleTranslator 一定实现 thinking_disabled_report()；
    但 translator 参数在本仓是鸭子类型（测试替身只实现 _call_api/preflight，
    且 preflight 本身也用 getattr 探测），故对缺失该方法的替身回落到 Config
    配置模式，与现有 getattr 惯例保持一致。
    """
    report = getattr(translator, "thinking_disabled_report", None)
    if callable(report):
        return report()
    from pipeline.config import Config
    mode = Config.LLM_DISABLE_THINKING
    return {
        "mode": mode,
        "effective": mode == "on",
        "probed": False,
        "unsupported_reason": None,
    }


def create_translator(**kwargs) -> BaseTranslator:
    """
    创建翻译器实例。

    通过 base_url + api_key + model 三参数适配所有
    OpenAI 兼容的 LLM 后端，无需选择 provider。

    Args:
        api_key: API 密钥
        model: 模型名称
        base_url: API 接口地址
        temperature: 生成温度
        max_tokens: 最大输出 token 数

    Returns:
        OpenAICompatibleTranslator 实例

    Examples:
        t = create_translator(api_key="sk-...", model="gpt-4o", base_url="https://api.openai.com/v1")
        t = create_translator()  # 使用环境变量
    """
    return OpenAICompatibleTranslator(**kwargs)
