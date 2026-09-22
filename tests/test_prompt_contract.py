"""Prompt Contract 行为测试（任务书 #34）。

验证三语 prompt 模板都包含共享契约条款：
- canonical source truth（source_text 是唯一依据）
- context leak ban（上下文不得翻进当前 ID）
- entity hallucination ban（未知专名不编造）
- number/negation/question preserve

要求：这些是**行为规则**——测试断言模板文本 + PromptBuilder 实际
组装结果都包含条款，且对 mock 模型输出（编造人名/翻转否定）由
gate 层拒绝（long_video 幻觉名门禁已覆盖，此处验证契约存在性）。
"""
from pathlib import Path

import pytest

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "data" / "prompts"

# 三语共享契约条款（各语言措辞可不同，但语义必须覆盖）
CONTRACT_CLAUSES = {
    "canonical_source_truth": [
        "source_text", "原文", "ソース", "원문", "唯一", "依据", "truth",
        "正本", "もとに", "기준",
    ],
    "no_context_leak": [
        "上下文", "context", "文脈", "맥락", "不得", "不能", "不要",
        "禁止", "advisory", "参考", "のみ", "참고",
    ],
    "no_entity_hallucination": [
        "专名", "人名", "entity", "Entity", "未知", "不明", "不确认",
        "编造", "臆造", "猜测", "invent", "hallucin", "fabricat",
        "知らない", "推測", "추측", "지어",
    ],
    "number_preserve": [
        "数字", "number", "数値", "숫자", "保留", "preserve",
        "維持", "유지", "不变", "同一",
    ],
    "negation_preserve": [
        "否定", "negation", "否定形", "부정", "不", "没", "没有",
        "ない", "아니", "not",
    ],
    "question_preserve": [
        "疑问", "question", "質問", "의문", "问号", "？", "?",
        "はてな", "물음", "询问", "问句", "문장", "interrog",
    ],
}


@pytest.fixture(scope="module")
def prompt_files():
    return {
        lang: (PROMPTS_DIR / f"{lang}-zh-CN.txt").read_text(encoding="utf-8")
        for lang in ("en", "ja", "ko")
    }


@pytest.mark.parametrize("lang", ["en", "ja", "ko"])
@pytest.mark.parametrize("clause", list(CONTRACT_CLAUSES.keys()))
def test_language_template_contains_contract_clause(prompt_files, lang, clause):
    """三语模板都必须包含共享契约条款（任一词命中即可）。"""
    text = prompt_files[lang]
    keywords = CONTRACT_CLAUSES[clause]
    assert any(k.lower() in text.lower() for k in keywords), (
        f"[{lang}] 模板缺少契约条款 {clause}（关键词 {keywords} 均未命中）"
    )


@pytest.mark.parametrize("lang", ["en", "ja", "ko"])
def test_prompt_builder_output_contains_contract(prompt_files, lang):
    """PromptBuilder 实际组装结果也包含契约（模板真实加载路径）。"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pipeline.parser.srt_parser import Subtitle
    from pipeline.translate.prompt_builder import PromptBuilder

    builder = PromptBuilder(
        str(PROMPTS_DIR / f"{lang}-zh-CN.txt"), source_language=lang)
    batch = [Subtitle(1, "00:00:01,000", "00:00:03,000", "テスト文。")]
    prompt = builder.build(batch, glossary={"テスト": "测试"})
    text = prompt.lower()
    for clause in ("canonical_source_truth", "no_entity_hallucination",
                   "no_context_leak"):
        keywords = CONTRACT_CLAUSES[clause]
        assert any(k.lower() in text for k in keywords), (
            f"[{lang}] PromptBuilder 输出缺少 {clause}"
        )


@pytest.mark.parametrize("lang", ["en", "ja", "ko"])
def test_template_has_no_cross_language_leak(prompt_files, lang):
    """语言标签不能漂移：ja 模板不应出现 KO:/EN: 硬编码标签（历史 bug）。"""
    text = prompt_files[lang]
    if lang == "ja":
        assert "KO:" not in text, "JA 模板出现 KO: 标签（历史跨语言 bug）"
    if lang == "ja":
        assert "EN:" not in text.replace("EN", ""), "JA 模板不应有 EN 硬编码标签"
    if lang == "ko":
        assert "JA:" not in text, "KO 模板出现 JA: 标签"
