"""
Translation Validator — 翻译完整性校验 + 英文残留检查

职责：
- 检查输入字幕数量 == 输出字幕数量（不一致则抛异常）
- 检查翻译结果中是否有空字段
- 检查 id 是否一一对应
- **英文残留检查**：检测翻译结果中是否残留未翻译的英文单词
- 可选：检查翻译文本长度是否异常

原则：
- 验证失败就抛异常，不要让坏数据流入下一阶段
"""
import re
from dataclasses import dataclass, field
from typing import List, Tuple

from pipeline.parser.srt_parser import timestamp_to_ms


class ValidationError(Exception):
    """翻译验证失败。管道应立即停止。"""
    pass


@dataclass
class ValidationReport:
    """验证结果报告"""
    passed: bool = True
    input_count: int = 0
    output_count: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        status = "✅ 通过" if self.passed else "❌ 失败"
        lines = [f"验证结果: {status}"]
        lines.append(f"  输入: {self.input_count} 条")
        lines.append(f"  输出: {self.output_count} 条")
        for e in self.errors:
            lines.append(f"  ❌ {e}")
        for w in self.warnings:
            lines.append(f"  ⚠️ {w}")
        return '\n'.join(lines)


def validate(input_subtitles: list,
             output_results: list,
             strict: bool = True) -> ValidationReport:
    """
    验证翻译结果完整性。

    核心检查（任何一项失败都抛异常）：
    1. 输入数量 == 输出数量
    2. 无空翻译（strict 模式下）
    3. id 一一对应

    警告（不抛异常，但记录到 report）：
    - 翻译文本异常短（< 原文字符数的 5%）
    - 翻译文本异常长（> 原文字符数的 500%）

    Args:
        input_subtitles: 输入的 Subtitle 对象列表
        output_results: 输出的 TranslateResult 对象列表
        strict: True 时遇到空翻译也抛异常

    Returns:
        ValidationReport

    Raises:
        ValidationError: 验证失败时抛出（管道应捕获并停止）
    """
    report = ValidationReport(
        input_count=len(input_subtitles),
        output_count=len(output_results),
    )

    # --- 检查1: 数量一致 ---
    if report.input_count != report.output_count:
        report.passed = False
        msg = (
            f"数量不一致: 输入 {report.input_count} 条, "
            f"输出 {report.output_count} 条 (差异: "
            f"{abs(report.input_count - report.output_count)} 条)"
        )
        report.errors.append(msg)
        raise ValidationError(str(report))

    # --- 检查2: id 一一对应 ---
    input_ids = {sub.id for sub in input_subtitles}
    output_ids = {r.id for r in output_results}

    missing_in_output = input_ids - output_ids
    extra_in_output = output_ids - input_ids

    if missing_in_output:
        report.passed = False
        report.errors.append(f"输出中缺少的字幕 id: {sorted(missing_in_output)}")

    if extra_in_output:
        report.passed = False
        report.errors.append(f"输出中多余的字幕 id: {sorted(extra_in_output)}")

    # --- 检查3: 空翻译 ---
    for r in output_results:
        if not r.translated.strip():
            if strict:
                report.passed = False
                report.errors.append(f"字幕 #{r.id} 翻译为空")
            else:
                report.warnings.append(f"字幕 #{r.id} 翻译为空")

    # --- 检查4: 翻译长度异常 ---
    for r in output_results:
        if not r.translated or not r.original:
            continue
        ratio = len(r.translated) / max(len(r.original), 1)
        if ratio < 0.05:
            report.warnings.append(
                f"字幕 #{r.id} 翻译异常短 (原文 {len(r.original)} 字符 → "
                f"译文 {len(r.translated)} 字符)"
            )
        elif ratio > 5.0:
            report.warnings.append(
                f"字幕 #{r.id} 翻译异常长 (原文 {len(r.original)} 字符 → "
                f"译文 {len(r.translated)} 字符)"
            )

    # --- 汇总 ---
    if not report.passed:
        raise ValidationError(str(report))

    return report


# ---- 英文残留检查 ----

# 英文单词模式（至少 3 个字母，排除专有名词缩写和数字）
_ENGLISH_WORD_RE = re.compile(r'\b[A-Za-z]{3,}\b')

# 已知不需要翻译的关键词（游戏术语、UI 文本等）
_KNOWN_SAFE_WORDS = {
    'DPS', 'AoE', 'BOSS', 'CD', 'ATK', 'DEF', 'HP', 'EXP', 'XP',
    'DMG', 'ER', 'HB', 'TH', 'CC', 'BGM', 'CDR', 'WTF', 'WTH',
    'OK', 'OKs', 'Yay', 'Wow', 'Oh', 'Yeah', 'Nah', 'Bro', 'Dude',
    'Man', 'Hmm', 'Ugh', 'Eww', 'Ah', 'Ha', 'Heh', 'Huh', 'Hm',
    'Oof', 'Pop', 'Bam', 'Boom', 'Whoa', 'Woo', 'Yay', 'Hey',
    'PVP', 'PVE', 'NPC', 'DLC', 'FPS', 'RPG', 'META', 'GG',
}


def check_english_residual(text: str) -> Tuple[bool, List[str]]:
    """
    检查中文译文中是否残留英文单词。

    Args:
        text: 翻译后的文本

    Returns:
        (has_residual, [残留的英文单词列表])
    """
    if not text or not text.strip():
        return False, []

    words = _ENGLISH_WORD_RE.findall(text)
    # 排除已知安全词（大小写不敏感）
    residual = [w for w in words if w not in _KNOWN_SAFE_WORDS
                and w.upper() not in _KNOWN_SAFE_WORDS
                and w.capitalize() not in _KNOWN_SAFE_WORDS]

    return len(residual) > 0, residual


def validate_english_residual(output_results: list) -> List[str]:
    """
    对翻译结果做英文残留检查，返回警告列表。

    Args:
        output_results: TranslateResult 列表

    Returns:
        警告信息列表（每条格式: "字幕 #id: 残留 [word1, word2, ...]"）
    """
    warnings = []
    for r in output_results:
        has_en, words = check_english_residual(r.translated)
        if has_en:
            warnings.append(f"字幕 #{r.id}: 英文残留 {words}")
    return warnings


def validate_display_cues(
    subtitles: list,
    *,
    video_duration_ms: int | None = None,
    raise_on_error: bool = True,
    allow_source_residue: bool = False,
) -> ValidationReport:
    """Validate the final, viewer-facing subtitle timeline and readability."""
    report = ValidationReport(
        input_count=len(subtitles), output_count=len(subtitles),
    )
    previous_start = -1
    previous_end = -1
    for subtitle in subtitles:
        start = timestamp_to_ms(subtitle.start)
        end = timestamp_to_ms(subtitle.end)
        text = str(subtitle.text or "").strip()
        visible = len(re.sub(r"\s+", "", text))
        duration = end - start
        if end <= start:
            report.errors.append(f"字幕 #{subtitle.id} 结束时间必须晚于开始时间")
        if start < previous_start:
            report.errors.append(f"字幕 #{subtitle.id} 时间轴未递增")
        if previous_end >= 0 and start < previous_end - 250:
            report.errors.append(f"字幕 #{subtitle.id} 与前一条发生不合理重叠")
        if video_duration_ms is not None and end > video_duration_ms + 250:
            report.errors.append(f"字幕 #{subtitle.id} 超出视频总时长")
        if 0 < duration < 300:
            report.warnings.append(f"字幕 #{subtitle.id} 显示不足 0.3 秒，可能闪现")
        if duration > 7_000:
            report.warnings.append(f"字幕 #{subtitle.id} 显示超过 7 秒")
        if duration > 0 and visible >= 8 and visible / (duration / 1000) > 14:
            report.warnings.append(f"字幕 #{subtitle.id} 中文阅读速度过快")
        lines = text.splitlines() or [""]
        if len(lines) > 2:
            report.warnings.append(f"字幕 #{subtitle.id} 超过两行")
        if any(len(re.sub(r"\s+", "", line)) > 22 for line in lines):
            report.warnings.append(f"字幕 #{subtitle.id} 单行超过 22 个可见字符")
        if text.startswith(tuple("，。！？；：,.!?;:…")):
            report.warnings.append(f"字幕 #{subtitle.id} 以孤立标点开始")
        if not allow_source_residue and re.search(r"[\u3040-\u30ff]", text):
            report.errors.append(f"字幕 #{subtitle.id} 存在日文假名残留")
        if not allow_source_residue and re.search(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", text):
            report.errors.append(f"字幕 #{subtitle.id} 存在韩文残留")
        previous_start, previous_end = start, max(previous_end, end)

    report.passed = not report.errors
    if report.errors and raise_on_error:
        raise ValidationError(str(report))
    return report
