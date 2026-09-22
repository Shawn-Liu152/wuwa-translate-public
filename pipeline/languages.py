"""Language-pair contract shared by jobs, manifests, and pipeline stages."""
from __future__ import annotations

from collections.abc import Mapping


DEFAULT_SOURCE_LANGUAGE = "en"
DEFAULT_TARGET_LANGUAGE = "zh-CN"
SUPPORTED_SOURCE_LANGUAGES = frozenset({"en", "ja", "ko"})
SUPPORTED_TARGET_LANGUAGES = frozenset({"zh-CN"})


def normalize_language_pair(values: Mapping[str, object] | None = None) -> dict[str, str]:
    """Return the supported language pair, defaulting legacy records to en→zh-CN.

    Missing values are deliberately treated as legacy data. Present but invalid
    values are rejected so a client cannot silently create a mixed-language job.
    """
    values = values or {}
    raw_source = values.get("source_language", DEFAULT_SOURCE_LANGUAGE)
    raw_target = values.get("target_language", DEFAULT_TARGET_LANGUAGE)
    if not isinstance(raw_source, str) or not isinstance(raw_target, str):
        raise ValueError("语言代码必须是文本")
    source_language = raw_source.strip().lower()
    target_language = raw_target.strip()
    if source_language not in SUPPORTED_SOURCE_LANGUAGES:
        raise ValueError("来源语言仅支持 en、ja 或 ko")
    if target_language not in SUPPORTED_TARGET_LANGUAGES:
        raise ValueError("目标语言首版仅支持 zh-CN")
    return {
        "source_language": source_language,
        "target_language": target_language,
    }


def language_code_suffix(source_language: str) -> str:
    """Validate and return the source-language file suffix."""
    return normalize_language_pair({"source_language": source_language})[
        "source_language"
    ]
