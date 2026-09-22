"""阶段 E 测试：Rolling Context（滚动上下文）。

验证：上下文（含后文）被注入 prompt，但格式上明确区分——
batch 行用 >>> 标记，上下文行四空格缩进；模板规则硬性禁止
把上下文翻进当前 ID（Case 8）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.parser.srt_parser import Subtitle
from pipeline.translate.prompt_builder import PromptBuilder

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def _subs(count: int):
    return [Subtitle(i, f"00:00:{i:02d},000", f"00:00:{i:02d},500", f"문장{i}") for i in range(1, count + 1)]


def _builder(source_language: str = "ko") -> PromptBuilder:
    path = os.path.join(DATA_DIR, "prompts", f"{source_language}-zh-CN.txt")
    return PromptBuilder(path, source_language=source_language)


def test_context_includes_following_subs():
    """后文存在：batch 之后的语义单元也注入 prompt（仅供理解）。"""
    all_subs = _subs(12)
    builder = _builder()
    batch = all_subs[3:7]  # id 4-7
    text = builder._format_subtitles_with_context(batch, all_subs, context_window=2)
    # batch 之后的行（id 8、9）以四空格缩进出现
    assert "    [8] 문장8" in text
    assert "    [9] 문장9" in text
    # batch 之前的行（id 2、3）也在
    assert "    [2] 문장2" in text
    assert "    [3] 문장3" in text


def test_context_marks_batch_but_never_context_ids():
    """格式区分：batch 行 >>> 标记，上下文行四空格；上下文行绝不带 >>>。"""
    all_subs = _subs(10)
    builder = _builder()
    batch = all_subs[4:6]  # id 5-6
    text = builder._format_subtitles_with_context(batch, all_subs, context_window=2)
    assert ">>> [5] 문장5 <<<" in text
    assert ">>> [6] 문장6 <<<" in text
    # 上下文行（含后文）不得被误标为 batch
    assert ">>> [7]" not in text
    assert ">>> [8]" not in text
    assert ">>> [4]" not in text
    # 上下文行必须是四空格缩进
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(">>>"):
            continue
        assert line.startswith("    "), f"上下文行必须四空格缩进: {line!r}"


def test_template_rule_prevents_context_leak():
    """模板规则：上下文只用于理解，绝不能翻进当前 ID。"""
    for lang in ("ko", "ja"):
        path = os.path.join(DATA_DIR, "prompts", f"{lang}-zh-CN.txt")
        raw = open(path, "rb").read()
        for enc in ("utf-8-sig", "utf-8"):
            try:
                content = raw.decode(enc)
                break
            except Exception:
                continue
        assert ("只用于理解" in content or "只能用于理解" in content), \
            f"{lang} 模板缺上下文约束"
        assert "不能" in content or "绝不" in content
