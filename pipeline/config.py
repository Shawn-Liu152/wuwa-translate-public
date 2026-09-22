"""
全局配置

集中管理所有可配置参数，包括：
- 文件路径
- LLM 参数（模型、API Key、温度等）
- 管道开关（可跳过特定步骤）
- 批次大小
- 日志级别
"""
import ipaddress
import os
import re
from urllib.parse import urlsplit, urlunsplit


LOOPBACK_LLM_HOSTS = {"localhost", "127.0.0.1", "::1"}
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _normalize_llm_hostname(value: str) -> tuple[str, bool]:
    """Return a canonical ASCII hostname and whether it is an IPv6 literal."""
    if not value or "%" in value:
        raise ValueError("API 接口地址主机名无效")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            hostname = value.encode("idna").decode("ascii").casefold()
        except UnicodeError as error:
            raise ValueError("API 接口地址主机名无效") from error
        labels = hostname.split(".")
        if (
            len(hostname) > 253
            or any(not _DNS_LABEL_RE.fullmatch(label) for label in labels)
        ):
            raise ValueError("API 接口地址主机名无效")
        return hostname, False
    return address.compressed.casefold(), address.version == 6


def normalize_llm_base_url(value: str | None, *, required: bool = True) -> str:
    """Validate an explicit OpenAI-compatible endpoint root.

    APP-ENDPOINT-001: remote credentials and subtitles require HTTPS. Plain
    HTTP is accepted only for an exact loopback hostname.
    """
    normalized = str(value or "").strip()
    if not normalized:
        if required:
            raise ValueError("API 接口地址不能为空")
        return ""
    if (
        any(character.isspace() or ord(character) == 127 for character in normalized)
        or "\\" in normalized
    ):
        raise ValueError("API 接口地址格式无效")
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError) as error:
        raise ValueError("API 接口地址格式无效") from error
    scheme = parsed.scheme.casefold()
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if (
        scheme not in {"http", "https"}
        or not hostname
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or authority.endswith(":")
        or port is not None and not 1 <= port <= 65535
    ):
        raise ValueError("API 接口地址必须是有效的 HTTP(S) 根地址")
    canonical_host, is_ipv6 = _normalize_llm_hostname(hostname)
    if scheme == "http" and canonical_host not in LOOPBACK_LLM_HOSTS:
        raise ValueError("远程 API 接口必须使用 HTTPS；HTTP 仅允许本机回环地址")
    canonical_netloc = f"[{canonical_host}]" if is_ipv6 else canonical_host
    if port is not None:
        canonical_netloc = f"{canonical_netloc}:{port}"
    return urlunsplit((
        scheme,
        canonical_netloc,
        parsed.path,
        "",
        "",
    )).rstrip("/")


def infer_provider(base_url: str) -> str:
    """Infer a stable provider label from a non-secret endpoint hostname."""
    try:
        hostname = (urlsplit(str(base_url or "")).hostname or "").lower()
    except ValueError:
        hostname = ""
    if hostname == "opencode.ai" or hostname.endswith(".opencode.ai"):
        return "opencode-go"
    if hostname == "deepseek.com" or hostname.endswith(".deepseek.com"):
        return "deepseek"
    return hostname or "unknown"


def normalize_disable_thinking(value: object) -> str:
    """Normalize the LLM_DISABLE_THINKING switch to auto/on/off.

    O-2：cun.ai 网关只认 thinking={"type":"disabled"}，会忽略 reasoning_effort
    与 enable_thinking。"auto"（默认）= 预检探测后再决定是否注入；"on" = 强制
    注入；"off" = 永不注入。绝不允许把 "on" 设成全局默认——换成真推理模型会
    把译文质量打掉。
    """
    raw = str(value if value is not None else "auto").strip().lower()
    if raw in {"auto", "probe", ""}:
        return "auto"
    if raw in {"on", "true", "1", "yes", "disabled", "disable"}:
        return "on"
    if raw in {"off", "false", "0", "no", "none"}:
        return "off"
    raise ValueError("LLM_DISABLE_THINKING 只接受 auto/on/off")


class Config:
    """管道全局配置"""

    # --- 路径配置 ---
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(BASE_DIR, "data")
    INPUT_DIR = os.path.join(BASE_DIR, "input")
    OUTPUT_DIR = os.path.join(BASE_DIR, "output")
    DATABASE_DIR = os.path.join(BASE_DIR, "database")

    # --- 数据文件 ---
    ALIAS_FILE = os.path.join(DATA_DIR, "alias.json")
    GLOSSARY_DB = os.path.join(DATA_DIR, "glossary.db")
    MUSIC_FILE = os.path.join(DATA_DIR, "music.txt")
    PROMPT_TEMPLATE_FILE = os.path.join(DATA_DIR, "prompt_template.txt")
    # O-1：审校分流白名单（大牌外部实体 + entity guard 误报的普通词）。
    # 独立于 glossary.db，只影响风险队列的自动放行，不参与翻译。
    EXTERNAL_ENTITIES_FILE = os.path.join(DATA_DIR, "external_entities.json")
    # O-1 激进分流：信任 round2 的高置信 keep，把对应条目移出人工队列。
    #   off   = 只走白名单/源冲突/提示级（最保守）
    #   names = 只信任实体/人名类（unknown_entity/person_name_present/contextual_*）
    #   all   = 任何 round2 keep&conf≥阈值都放行（含 hallucinated_entity/内容类，
    #           有错误风险，靠 5% 抽检兜底）—— 用户选定的激进档
    # 红线：无论哪档都只改派生 state，绝不动 review_required 旗标，
    #       _delivery_status 恒为 review_required，成品仍需人工显式点头。
    _RISK_TRUST_KEEP_SCOPE = os.getenv("RISK_TRUST_KEEP_SCOPE", "all").strip().lower()
    if _RISK_TRUST_KEEP_SCOPE not in {"off", "names", "all"}:
        raise ValueError("RISK_TRUST_KEEP_SCOPE 只接受 off/names/all")
    RISK_TRUST_KEEP_SCOPE = _RISK_TRUST_KEEP_SCOPE
    try:
        RISK_TRUST_KEEP_CONFIDENCE = min(
            1.0, max(0.0, float(os.getenv("RISK_TRUST_KEEP_CONFIDENCE", "0.9")))
        )
    except ValueError:
        raise ValueError("RISK_TRUST_KEEP_CONFIDENCE 只接受 0~1 的数字") from None

    # --- LLM 配置 ---
    LLM_MODEL = os.getenv("LLM_MODEL", "").strip()
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()
    _PROVIDER_OVERRIDE = os.getenv("PROVIDER", "").strip()
    PROVIDER = _PROVIDER_OVERRIDE or infer_provider(LLM_BASE_URL)
    LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
    LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "8192"))  # 推理模型需要更大空间
    # PERF-P0-02：任务前 API/模型权限预检开关。
    # "on"（默认）= 现行为：需要翻译的全新/部分恢复任务先做一次 128 token 预检。
    # "off" = 跳过预检，错误改由第一批暴露（省一次往返，坏 key 会多花一个批次的请求）。
    # 仅影响预检；LLM_PREFLIGHT=off 不会跳过任何翻译请求本身。
    _PREFLIGHT_RAW = os.getenv("LLM_PREFLIGHT", "on").strip().lower()
    if _PREFLIGHT_RAW not in {"on", "off", "true", "false", "1", "0"}:
        raise ValueError("LLM_PREFLIGHT 只接受 on/off/true/false/1/0")
    LLM_PREFLIGHT_ENABLED = _PREFLIGHT_RAW in {"on", "true", "1"}

    # O-2：关闭上游 reasoning（thinking）的开关，归一化为 auto/on/off。
    # 默认 auto = 由 translator.preflight() 发一次超小探测请求决定该站点是否
    # 支持 thinking={"type":"disabled"}；支持才注入，400/未知参数则自动降级。
    LLM_DISABLE_THINKING = normalize_disable_thinking(
        os.getenv("LLM_DISABLE_THINKING", "auto")
    )

    # --- manifest 持久化节流（S-2，修复 O(n^2) 整份重写） ---
    # 每批整份 JSON 重写会随批次数线性变慢（2000 条/63 批实测 1229ms 且
    # 全程持锁串行化并发批次）。改为：failed/cancelled 立即落盘；
    # running/success 累计 MANIFEST_FLUSH_EVERY 批或距上次写超过
    # MANIFEST_FLUSH_SECONDS 秒才落盘；终态前强制 flush。
    # 崩溃窗口：最多丢失 flush_every-1 批的落盘进度，resume 时重译。
    try:
        MANIFEST_FLUSH_EVERY = max(1, int(os.getenv("MANIFEST_FLUSH_EVERY", "8")))
    except ValueError:
        raise ValueError("MANIFEST_FLUSH_EVERY 只接受正整数") from None
    try:
        MANIFEST_FLUSH_SECONDS = max(
            0.0, float(os.getenv("MANIFEST_FLUSH_SECONDS", "30"))
        )
    except ValueError:
        raise ValueError("MANIFEST_FLUSH_SECONDS 只接受非负数字") from None

    # --- 批次 API 尝试预算（PERF-P0-03 / S-3） ---
    # 单批最坏情况 = 语义补译 3 次 × 传输重试 3 次 = 9 次调用 + 长 Backoff。
    # 预算只约束「传输层已反复退避 + 语义补译仍未通过」的组合场景：
    # 累计 API 调用数达到预算后停止继续补译，原因写入 manifest 的
    # validation_events（kind=attempt_budget_exhausted）。传输健康的
    # 批次（每次 1 次调用 × 3 次补译 = 3 次）不受任何影响。
    try:
        BATCH_API_ATTEMPT_BUDGET = max(
            1, int(os.getenv("LLM_BATCH_ATTEMPT_BUDGET", "6"))
        )
    except ValueError:
        raise ValueError("LLM_BATCH_ATTEMPT_BUDGET 只接受正整数") from None

    # --- P0-2：可重试 provider 失败的有界自动续跑 ---
    # 仅对 provider_unavailable / api_error / invalid_provider_response 生效；
    # 欠费(quota_error)/鉴权/配置类永不续跑。0 = 关闭自动续跑（行为同现状）。
    # 退避 = BASE * 2**(attempt-1)，默认 30/60/120 秒；等待可被取消打断。
    try:
        LLM_AUTO_RESUME_MAX = max(0, int(os.getenv("LLM_AUTO_RESUME_MAX", "3")))
    except ValueError:
        raise ValueError("LLM_AUTO_RESUME_MAX 只接受非负整数") from None
    try:
        LLM_AUTO_RESUME_BASE_DELAY_SECONDS = max(
            0.0, float(os.getenv("LLM_AUTO_RESUME_BASE_DELAY_SECONDS", "30"))
        )
    except ValueError:
        raise ValueError(
            "LLM_AUTO_RESUME_BASE_DELAY_SECONDS 只接受非负数字"
        ) from None

    # --- 卡死任务看门狗 ---
    # 单 worker 串行执行时，一个孤儿任务会堵住整个队列。阈值必须为正数：
    # 0 秒循环会空转占满 CPU，0 分钟超时则会把刚启动的任务误判为卡死。
    try:
        STUCK_JOB_TIMEOUT_MINUTES = max(
            1, int(os.getenv("STUCK_JOB_TIMEOUT_MINUTES", "30"))
        )
    except ValueError:
        raise ValueError("STUCK_JOB_TIMEOUT_MINUTES 只接受正整数") from None
    try:
        STUCK_JOB_CHECK_INTERVAL_SECONDS = max(
            1, int(os.getenv("STUCK_JOB_CHECK_INTERVAL_SECONDS", "300"))
        )
    except ValueError:
        raise ValueError(
            "STUCK_JOB_CHECK_INTERVAL_SECONDS 只接受正整数"
        ) from None

    # --- 下载（yt-dlp）弹性参数 ---
    # Web 工作台是单 worker 串行下载，yt-dlp 默认不设 socket 超时，网络挂起
    # 会永久卡住整条队列（看门狗只能事后兜底）。socket-timeout 让挂起的连接
    # 自己超时并进入 yt-dlp 的重试路径。
    # 0 = 不传该参数（回到 yt-dlp 默认行为）。
    try:
        YTDLP_SOCKET_TIMEOUT = min(
            600.0, max(0.0, float(os.getenv("YTDLP_SOCKET_TIMEOUT", "30")))
        )
    except ValueError:
        raise ValueError("YTDLP_SOCKET_TIMEOUT 只接受 0~600 的数字") from None
    try:
        YTDLP_RETRY_SLEEP_SECONDS = min(
            60.0, max(0.0, float(os.getenv("YTDLP_RETRY_SLEEP", "3")))
        )
    except ValueError:
        raise ValueError("YTDLP_RETRY_SLEEP 只接受 0~60 的数字") from None
    # 并发分片只影响 DASH/HLS 分片下载；<=1 时不传，保持 yt-dlp 默认串行。
    try:
        YTDLP_CONCURRENT_FRAGMENTS = min(
            16, max(0, int(os.getenv("YTDLP_CONCURRENT_FRAGMENTS", "4")))
        )
    except ValueError:
        raise ValueError(
            "YTDLP_CONCURRENT_FRAGMENTS 只接受 0~16 的整数"
        ) from None
    # 以下两项默认关闭：自定义 UA 与伪造 X-Forwarded-For 会改变请求指纹，
    # 在部分站点反而更容易被拦。需要时再用 env 显式打开。
    YTDLP_USER_AGENT = os.getenv("YTDLP_USER_AGENT", "").strip()
    _YTDLP_GEO_BYPASS_RAW = os.getenv("YTDLP_GEO_BYPASS", "off").strip().lower()
    if _YTDLP_GEO_BYPASS_RAW not in {
        "on", "off", "true", "false", "1", "0", "yes", "no", "",
    }:
        raise ValueError("YTDLP_GEO_BYPASS 只接受 on/off/true/false/1/0")
    YTDLP_GEO_BYPASS = _YTDLP_GEO_BYPASS_RAW in {"on", "true", "1", "yes"}

    # --- 管道控制 ---
    BATCH_SIZE = 28               # Token 自适应批次的目标字幕条数
    ROUND1_SOURCE_TOKEN_BUDGET = int(
        os.getenv("ROUND1_SOURCE_TOKEN_BUDGET", "2600")
    )
    MAX_CONCURRENT = 5            # 最大并发 API 请求
    STATEFUL_MAX_CONCURRENT = max(
        1, min(3, int(os.getenv("STATEFUL_MAX_CONCURRENT", "3")))
    )                              # manifest/术语锚定任务的安全并发上限
    ENABLE_ALIAS = True           # 是否启用 Alias 修正
    ENABLE_JAPANESE_FILTER = True # 是否过滤日文
    ENABLE_MUSIC_DETECTION = True # 是否检测音乐行
    ENABLE_GLOSSARY = True        # 是否提取术语
    ENABLE_VALIDATION = True      # 是否验证翻译完整性
    ENABLE_DEDUPE = True          # 是否启用 YouTube 字幕去重合并
    ENABLE_CONTEXT = True         # 翻译时是否带入前后文（提高翻译连贯性）
    ENABLE_TERM_ANCHOR = True    # 是否启用跨批次术语锚定（扫描已完成批次的术语确保一致性）
    ENABLE_PROFILER = True        # 是否输出性能统计

    # --- 日志 ---
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    @classmethod
    def provider_fingerprint(
        cls, base_url: str | None = None, provider: str | None = None,
    ) -> dict[str, str]:
        """Return reproducibility metadata without persisting credentials."""
        effective_url = str(
            cls.LLM_BASE_URL if base_url is None else base_url
        ).strip()
        try:
            hostname = (urlsplit(effective_url).hostname or "").lower()
        except ValueError:
            hostname = ""
        return {
            "name": str(provider or "").strip()
            or cls._PROVIDER_OVERRIDE
            or infer_provider(effective_url),
            "hostname": hostname,
        }
