"""
Validator 测试

测试点：
- 数量一致 → 通过
- 数量不一致 → 抛 ValidationError
- id 不匹配 → 抛异常
- 空翻译检测
- 翻译长度异常检测（警告模式）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.postprocess.validate import (
    validate, validate_display_cues, ValidationError, ValidationReport,
)
from pipeline.parser.srt_parser import Subtitle
from pipeline.translate.llm import TranslateResult


def _make_input(n: int = 3) -> list:
    return [
        Subtitle(id=1, start="00:00:01,000", end="00:00:03,000", text="Hello"),
        Subtitle(id=2, start="00:00:04,000", end="00:00:06,000", text="World"),
        Subtitle(id=3, start="00:00:07,000", end="00:00:09,000", text="Rover"),
    ][:n]


def _make_output(n: int = 3) -> list:
    return [
        TranslateResult(id=1, original="Hello", translated="你好"),
        TranslateResult(id=2, original="World", translated="世界"),
        TranslateResult(id=3, original="Rover", translated="漂泊者"),
    ][:n]


def test_equal_count_passes():
    """输入=输出 → 通过"""
    inp = _make_input(3)
    out = _make_output(3)
    report = validate(inp, out)
    assert report.passed is True
    assert report.input_count == 3
    assert report.output_count == 3


def test_unequal_count_raises():
    """输入 ≠ 输出 → 抛异常"""
    inp = _make_input(3)
    out = _make_output(2)

    try:
        validate(inp, out)
        assert False, "应该抛出 ValidationError"
    except ValidationError as e:
        assert "数量不一致" in str(e)
        assert "3" in str(e)
        assert "2" in str(e)


def test_empty_translation_raises():
    """空翻译 → 抛异常 (strict 模式)"""
    inp = _make_input(2)
    out = [
        TranslateResult(id=1, original="Hello", translated="你好"),
        TranslateResult(id=2, original="World", translated=""),  # 空翻译
    ]

    try:
        validate(inp, out, strict=True)
        assert False, "应该抛出 ValidationError"
    except ValidationError as e:
        assert "翻译为空" in str(e)
        assert "2" in str(e)


def test_empty_translation_warning_non_strict():
    """非 strict 模式：空翻译只产生警告，不抛异常"""
    inp = _make_input(2)
    out = [
        TranslateResult(id=1, original="Hello", translated="你好"),
        TranslateResult(id=2, original="World", translated=""),
    ]

    report = validate(inp, out, strict=False)
    assert report.passed is True
    assert len(report.warnings) >= 1
    assert any("翻译为空" in w for w in report.warnings)


def test_id_mismatch():
    """id 不匹配 → 抛异常"""
    inp = [
        Subtitle(id=1, start="00:00:01,000", end="00:00:03,000", text="A"),
        Subtitle(id=2, start="00:00:04,000", end="00:00:06,000", text="B"),
    ]
    out = [
        TranslateResult(id=1, original="A", translated="翻译A"),
        TranslateResult(id=3, original="C", translated="翻译C"),  # id=3 不在输入中
    ]

    try:
        validate(inp, out)
        assert False, "应该抛出 ValidationError"
    except ValidationError as e:
        assert "多余" in str(e) or "缺少" in str(e)


def test_length_anomaly_warning():
    """翻译长度异常 → 警告（不抛异常）"""
    inp = [
        Subtitle(id=1, start="00:00:01,000", end="00:00:03,000",
                 text="A very long English sentence with many words in it"),
    ]
    out = [
        TranslateResult(id=1,
                        original="A very long English sentence with many words in it",
                        translated="短"),  # 长度比例 <5%
    ]

    report = validate(inp, out, strict=False)
    assert report.passed is True
    assert len(report.warnings) >= 1
    assert any("异常短" in w for w in report.warnings)


def test_validation_report_str():
    """ValidationReport 字符串表示"""
    report = ValidationReport(
        passed=True,
        input_count=100,
        output_count=100,
        warnings=["字幕 #5 翻译异常短"],
    )
    s = str(report)
    assert "通过" in s
    assert "100" in s
    assert "5" in s


def test_validation_error_str():
    """ValidationError 包含报告信息"""
    try:
        validate(_make_input(5), _make_output(3))
    except ValidationError as e:
        s = str(e)
        assert "数量不一致" in s


def test_display_validation_blocks_invalid_timeline_and_warns_unreadable_cues():
    cues = [
        Subtitle(1, "00:00:02,000", "00:00:01,000", "倒置时间轴"),
        Subtitle(2, "00:00:03,000", "00:00:03,200", "这条字幕闪现得太快"),
        Subtitle(3, "00:00:04,000", "00:00:05,000", "这是一条非常非常非常非常非常非常长的中文字幕"),
    ]

    report = validate_display_cues(cues, raise_on_error=False)

    assert not report.passed
    assert any("结束时间" in error for error in report.errors)
    assert any("闪现" in warning for warning in report.warnings)
    assert any("阅读速度" in warning or "单行" in warning for warning in report.warnings)


def test_display_validation_detects_hangul_residue():
    report = validate_display_cues([
        Subtitle(1, "00:00:00,000", "00:00:02,000", "카르테시아很强"),
    ], raise_on_error=False)

    assert not report.passed
    assert any("韩文残留" in error for error in report.errors)


if __name__ == "__main__":
    print("=" * 60)
    print("Validator 测试套件")
    print("=" * 60)

    tests = [
        ("数量一致通过", test_equal_count_passes),
        ("数量不一致抛异常", test_unequal_count_raises),
        ("空翻译抛异常", test_empty_translation_raises),
        ("非strict空翻译警告", test_empty_translation_warning_non_strict),
        ("id不匹配", test_id_mismatch),
        ("翻译长度异常警告", test_length_anomaly_warning),
        ("ValidationReport字符串", test_validation_report_str),
        ("ValidationError包含报告", test_validation_error_str),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
            passed += 1
        except Exception as e:
            print(f"[FAIL] {name}: {e}")
            failed += 1

    print(f"\n结果: {passed} 通过, {failed} 失败, {passed+failed} 总计")
    sys.exit(0 if failed == 0 else 1)
