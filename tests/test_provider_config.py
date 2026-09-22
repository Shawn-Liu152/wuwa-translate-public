import importlib

import pytest

import pipeline.config as config_module
from pipeline.config import Config, infer_provider, normalize_llm_base_url
from tools.run_benchmark import resolve_provider_settings


def test_default_provider_has_no_hidden_endpoint(monkeypatch):
    with monkeypatch.context() as clean_environment:
        clean_environment.delenv("LLM_BASE_URL", raising=False)
        clean_environment.delenv("PROVIDER", raising=False)
        reloaded = importlib.reload(config_module)

        assert reloaded.Config.LLM_BASE_URL == ""
        assert reloaded.Config.PROVIDER == "unknown"

    importlib.reload(config_module)


def test_default_provider_has_no_hidden_model(monkeypatch):
    with monkeypatch.context() as clean_environment:
        clean_environment.delenv("LLM_MODEL", raising=False)
        reloaded = importlib.reload(config_module)

        assert reloaded.Config.LLM_MODEL == ""

    importlib.reload(config_module)


def test_provider_is_inferred_from_base_url_hostname():
    assert infer_provider("https://opencode.ai/zen/go/v1") == "opencode-go"
    assert infer_provider("https://api.deepseek.com/v1") == "deepseek"


def test_benchmark_provider_defaults_to_shared_config():
    provider, base_url = resolve_provider_settings()

    assert provider == Config.PROVIDER
    assert base_url == Config.LLM_BASE_URL


def test_benchmark_provider_switch_selects_matching_endpoint():
    assert resolve_provider_settings("deepseek") == (
        "deepseek",
        "https://api.deepseek.com/v1",
    )


@pytest.mark.parametrize(("value", "expected"), [
    (" https://Example.COM/v1/ ", "https://example.com/v1"),
    ("https://例子.测试/v1", "https://xn--fsqu00a.xn--0zwm56d/v1"),
    ("http://localhost:8000/v1/", "http://localhost:8000/v1"),
    ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1"),
    ("http://[::1]:11434/v1", "http://[::1]:11434/v1"),
])
def test_llm_endpoint_normalizes_safe_roots(value, expected):
    assert normalize_llm_base_url(value) == expected


@pytest.mark.parametrize("value", [
    "http://api.example.com/v1",
    "http://localhost.example.com/v1",
    "https://exa mple.com/v1",
    "https://bad_host.example/v1",
    "https://example.com./v1",
    "https://user@example/v1",
    "https://example.com/v1?token=secret",
    "ftp://example.com/v1",
    "https://[::1",
])
def test_llm_endpoint_rejects_unsafe_or_malformed_roots(value):
    with pytest.raises(ValueError):
        normalize_llm_base_url(value)


def test_optional_llm_endpoint_accepts_only_a_truly_blank_value():
    assert normalize_llm_base_url("  ", required=False) == ""


def test_explicit_empty_benchmark_endpoint_never_falls_back(monkeypatch):
    monkeypatch.setattr(Config, "LLM_BASE_URL", "https://env.example/v1")
    monkeypatch.setattr(Config, "PROVIDER", "environment-provider")

    assert resolve_provider_settings(base_url="") == ("unknown", "")


def test_benchmark_rejects_remote_plain_http_endpoint():
    with pytest.raises(ValueError, match="HTTPS"):
        resolve_provider_settings(base_url="http://api.example.com/v1")


def test_llm_preflight_toggle_defaults_on(monkeypatch):
    with monkeypatch.context() as clean_environment:
        clean_environment.delenv("LLM_PREFLIGHT", raising=False)
        reloaded = importlib.reload(config_module)

        assert reloaded.Config.LLM_PREFLIGHT_ENABLED is True

    importlib.reload(config_module)


def test_llm_preflight_toggle_accepts_off_and_aliases(monkeypatch):
    for raw, expected in (
        ("off", False),
        ("false", False),
        ("0", False),
        ("on", True),
        ("true", True),
        ("1", True),
    ):
        with monkeypatch.context() as environment:
            environment.setenv("LLM_PREFLIGHT", raw)
            reloaded = importlib.reload(config_module)

            assert reloaded.Config.LLM_PREFLIGHT_ENABLED is expected

    importlib.reload(config_module)


def test_llm_preflight_toggle_rejects_unknown_values(monkeypatch):
    with monkeypatch.context() as environment:
        environment.setenv("LLM_PREFLIGHT", "maybe")
        with pytest.raises(ValueError, match="LLM_PREFLIGHT"):
            importlib.reload(config_module)

    importlib.reload(config_module)


_YTDLP_ENV_NAMES = (
    "YTDLP_SOCKET_TIMEOUT",
    "YTDLP_RETRY_SLEEP",
    "YTDLP_CONCURRENT_FRAGMENTS",
    "YTDLP_USER_AGENT",
    "YTDLP_GEO_BYPASS",
)


def test_ytdlp_resilience_defaults_protect_the_serial_queue(monkeypatch):
    with monkeypatch.context() as clean_environment:
        for name in _YTDLP_ENV_NAMES:
            clean_environment.delenv(name, raising=False)
        reloaded = importlib.reload(config_module)

        assert reloaded.Config.YTDLP_SOCKET_TIMEOUT == 30.0
        assert reloaded.Config.YTDLP_RETRY_SLEEP_SECONDS == 3.0
        assert reloaded.Config.YTDLP_CONCURRENT_FRAGMENTS == 4
        # 改变请求指纹的两项默认关闭。
        assert reloaded.Config.YTDLP_USER_AGENT == ""
        assert reloaded.Config.YTDLP_GEO_BYPASS is False

    importlib.reload(config_module)


def test_ytdlp_resilience_env_overrides_are_parsed_and_clamped(monkeypatch):
    with monkeypatch.context() as environment:
        environment.setenv("YTDLP_SOCKET_TIMEOUT", "12")
        environment.setenv("YTDLP_RETRY_SLEEP", "0")
        environment.setenv("YTDLP_CONCURRENT_FRAGMENTS", "64")
        environment.setenv("YTDLP_USER_AGENT", "  Mozilla/5.0 (TestUA)  ")
        environment.setenv("YTDLP_GEO_BYPASS", "on")
        reloaded = importlib.reload(config_module)

        assert reloaded.Config.YTDLP_SOCKET_TIMEOUT == 12.0
        assert reloaded.Config.YTDLP_RETRY_SLEEP_SECONDS == 0.0
        assert reloaded.Config.YTDLP_CONCURRENT_FRAGMENTS == 16
        assert reloaded.Config.YTDLP_USER_AGENT == "Mozilla/5.0 (TestUA)"
        assert reloaded.Config.YTDLP_GEO_BYPASS is True

    importlib.reload(config_module)


@pytest.mark.parametrize(("name", "value"), [
    ("YTDLP_SOCKET_TIMEOUT", "soon"),
    ("YTDLP_RETRY_SLEEP", "3s"),
    ("YTDLP_CONCURRENT_FRAGMENTS", "many"),
    ("YTDLP_GEO_BYPASS", "maybe"),
])
def test_ytdlp_resilience_rejects_unknown_values(monkeypatch, name, value):
    with monkeypatch.context() as environment:
        environment.setenv(name, value)
        with pytest.raises(ValueError, match=name):
            importlib.reload(config_module)

    importlib.reload(config_module)


def test_stuck_job_watchdog_config_has_safe_defaults(monkeypatch):
    with monkeypatch.context() as clean_environment:
        clean_environment.delenv("STUCK_JOB_TIMEOUT_MINUTES", raising=False)
        clean_environment.delenv(
            "STUCK_JOB_CHECK_INTERVAL_SECONDS", raising=False,
        )
        reloaded = importlib.reload(config_module)

        assert reloaded.Config.STUCK_JOB_TIMEOUT_MINUTES == 30
        assert reloaded.Config.STUCK_JOB_CHECK_INTERVAL_SECONDS == 300

    importlib.reload(config_module)


def test_stuck_job_watchdog_config_accepts_positive_overrides(monkeypatch):
    with monkeypatch.context() as environment:
        environment.setenv("STUCK_JOB_TIMEOUT_MINUTES", "45")
        environment.setenv("STUCK_JOB_CHECK_INTERVAL_SECONDS", "90")
        reloaded = importlib.reload(config_module)

        assert reloaded.Config.STUCK_JOB_TIMEOUT_MINUTES == 45
        assert reloaded.Config.STUCK_JOB_CHECK_INTERVAL_SECONDS == 90

    importlib.reload(config_module)
