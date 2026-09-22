"""ASR 听错归一化：把转写变体修正为标准形式。

数据来源：真实 reaction 素材审计（2026-08-06）暴露的听错模式——
同一个人名/术语被 YouTube 自动字幕与 Whisper 转写出多个变体
（如 데미야/데니야 → 데니아）。归一化在 union 构建前执行，
保证术语匹配、翻译输入使用标准形式。
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict

_CORRECTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "asr_corrections.json",
)
_EN_CORRECTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "asr_corrections_en.json",
)


def load_asr_corrections(
    path: str | None = None, source_language: str | None = None,
) -> Dict[str, str]:
    """加载 ASR 修正表 {变体: 标准形式}，按长度降序。"""
    if path is None:
        path = _EN_CORRECTIONS_PATH if source_language == "en" else _CORRECTIONS_PATH
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return dict(sorted(raw.items(), key=lambda kv: len(kv[0]), reverse=True))


def normalize_asr_text(text: str, corrections: Dict[str, str] | None = None) -> str:
    """把文本中的 ASR 变体替换为标准形式（长变体优先）。

    拉丁字母变体（如 AMS → 에이메스）按词边界 + 大小写不敏感匹配，
    避免误伤含该子串的普通单词；非拉丁变体保持精确匹配。
    """
    if not text:
        return text
    if corrections is None:
        corrections = load_asr_corrections()
    if not corrections:
        return text

    def _is_latin(key: str) -> bool:
        return bool(re.search(r"[A-Za-z]", key)) and not re.search(
            r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af\u3040-\u30ff\u4e00-\u9fff]",
            key,
        )

    result = text
    for key in sorted(corrections, key=len, reverse=True):
        replacement = corrections[key]
        if _is_latin(key):
            pattern = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(key)}(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
        else:
            pattern = re.compile(re.escape(key))
        result = pattern.sub(lambda m: replacement, result)
    return result
