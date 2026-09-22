"""Back-project semantic translations onto readable display cue timelines."""
from __future__ import annotations

import math
import re

from pipeline.parser.srt_parser import Subtitle, timestamp_to_ms


_CLAUSE_RE = re.compile(r"[^，。！？；：,.!?;:…]+[，。！？；：,.!?;:…]*")
_VERSION_NUMBER_RE = re.compile(r"(?<!\d)\d+(?:\.\d+)+(?!\d)")


def _protect_version_number_boundary(text: str, cut: int) -> int:
    """Move a cut inside ``3.1``/``2.4.1`` to the end of the number."""
    for match in _VERSION_NUMBER_RE.finditer(text):
        if match.start() < cut < match.end():
            return match.end()
    return cut


def _merge_split_version_numbers(pieces: list[str]) -> list[str]:
    """Repair punctuation-led clause splitting at ``3.|1剧情``."""
    merged: list[str] = []
    for piece in pieces:
        if merged and re.search(r"\d+\.$", merged[-1]) and re.match(r"\d", piece):
            merged[-1] += piece
        else:
            merged.append(piece)
    return merged


def _format_timestamp(milliseconds: int) -> str:
    milliseconds = max(0, int(milliseconds))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _visible_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _merge_short_fragments(pieces: list[str], separators: list[str] | None = None) -> list[str]:
    """合并 <4 可见字符的孤儿残片到前一片，避免残句/单字 cue。

    审计实证（KO Job final）：'和负担啊。'、'的人生中，即'、'的她，如今…'
    等孤儿片段来自字符硬切。短残片并入前一片；若首片过短（无前片可并入），
    并入第二片。separators 记录空格文本（韩/英）的切点分隔符，合并时补回
    以免词与词粘连。
    """
    if separators is None:
        separators = [""] * max(0, len(pieces) - 1)
    merged: list[str] = []
    for index, piece in enumerate(pieces):
        if merged and _visible_length(piece) < 4:
            merged[-1] += separators[index - 1] + piece
        else:
            merged.append(piece)
    # 首片过短：并入第二片（无前一片可并入），补回原分隔符
    if len(merged) > 1 and _visible_length(merged[0]) < 4:
        merged[1] = merged[0] + separators[0] + merged[1]
        merged.pop(0)
    return merged


def _find_function_word_boundary(text: str, cut: int, cursor: int) -> int | None:
    """在目标切点附近找中文虚词边界，返回切点（虚词前）或 None。

    覆盖的虚词：的 之 把 在 了 和 与 是 就 都 也 又 但 而 或 及。
    在 cut ±4 字符内找最近的虚词；切点取虚词位置（虚词归下段）——
    这样"斯特赖德之门"切在"之"前，音译专名保持完整。
    """
    boundary_words = "的之把在了和与是就都也又但而或及"
    best: int | None = None
    for offset in range(5):
        for sign in (1, -1):
            pos = cut + sign * offset
            if pos <= cursor or pos >= len(text):
                continue
            if text[pos] in boundary_words:
                if sign == -1 and text[pos] == "之" and pos + 1 <= cut:
                    # 虚词在切点之前：切在虚词后（如"把|斯特赖德"）
                    return pos + 1
                return pos
    return best


def _split_text(text: str, desired: int) -> list[str]:
    if desired <= 1:
        return [text]
    clauses = [part for part in _CLAUSE_RE.findall(text) if part]
    if len(clauses) >= desired:
        groups: list[str] = []
        remaining = clauses[:]
        for position in range(desired):
            groups_left = desired - position
            if groups_left == 1:
                groups.append("".join(remaining))
                break
            target = sum(_visible_length(item) for item in remaining) / groups_left
            current = ""
            while remaining:
                candidate = current + remaining[0]
                if current and _visible_length(candidate) > target * 1.3:
                    break
                current = candidate
                remaining.pop(0)
                if _visible_length(current) >= target * 0.75:
                    break
            groups.append(current)
        return _merge_split_version_numbers(
            _merge_short_fragments([item for item in groups if item])
        )

    # Punctuation is sparse. Split near balanced character targets, moving
    # punctuation to the preceding piece so no cue begins with an orphan mark.
    # When the text uses spaces (English, Korean), prefer the nearest space
    # boundary so words are never cut in the middle.
    pieces = []
    separators: list[str] = []
    cursor = 0
    for position in range(1, desired):
        target = round(len(text) * position / desired)
        cut = max(cursor + 1, min(len(text) - (desired - position), target))
        while cut < len(text) and text[cut] in "，。！？；：,.!?;:…":
            cut += 1
        # 中文无空格无标点：优先在虚词（的/之/把/在/了等）边界切，
        # 避免把音译专名（如"斯特赖德"）切成两半。
        if cut < len(text) and text[cut] not in "，。！？；：,.!?;:…":
            boundary = _find_function_word_boundary(text, cut, cursor)
            if boundary is not None:
                cut = boundary
        if cut < len(text):
            left_space = text.rfind(" ", max(cursor, cut - 10), cut)
            right_space = text.find(" ", cut, min(len(text), cut + 10))
            if left_space != -1 or right_space != -1:
                if left_space == -1:
                    cut = right_space
                elif right_space == -1:
                    cut = left_space
                elif cut - left_space <= right_space - cut:
                    cut = left_space
                else:
                    cut = right_space
        cut = _protect_version_number_boundary(text, cut)
        piece = text[cursor:cut]
        if cut < len(text) and text[cut] == " ":
            separators.append(" ")
            cursor = cut + 1
        else:
            separators.append("")
            cursor = cut
        if piece:
            pieces.append(piece)
    tail = text[cursor:]
    if tail:
        pieces.append(tail)
    return _merge_split_version_numbers(_merge_short_fragments(pieces, separators))


def redistribute_translation(
    subtitle: Subtitle,
    source_spans: list[dict],
    *,
    target_chars: int = 18,
    max_duration_ms: int = 6_000,
) -> list[Subtitle]:
    """Split one semantic translation using its original display boundaries."""
    text = str(subtitle.text or "").strip()
    if not text:
        return []
    start_ms = timestamp_to_ms(subtitle.start)
    end_ms = timestamp_to_ms(subtitle.end)
    duration_ms = max(1, end_ms - start_ms)
    spans = [
        {
            "start": max(start_ms, timestamp_to_ms(str(item["start"]))),
            "end": min(end_ms, timestamp_to_ms(str(item["end"]))),
        }
        for item in source_spans
        if item.get("start") and item.get("end")
    ]
    spans = [item for item in spans if item["end"] > item["start"]]
    text_len = _visible_length(text)
    char_based = math.ceil(text_len / max(target_chars, 1))
    time_based = math.ceil(duration_ms / max(max_duration_ms, 1))
    # Guard: never split text shorter than target_chars regardless of
    # duration — reaction videos have long pauses where the spoken text
    # is very short, and forcing a split produces unreadable fragments
    # like "所以" + "嘛。".
    if text_len < target_chars:
        desired = 1
    else:
        # Cap desired so each piece has at least ~6 visible characters;
        # this prevents 20-char text from being split into 3+ pieces by
        # a long duration alone.
        min_piece = 6
        max_by_length = max(1, text_len // min_piece)
        # m-02: desired 还必须受 source spans 数量约束 —— 每个显示 cue 的
        # 时间边界来自各 span 的 end（+ 最终 end_ms），最多 len(spans)+1 段。
        # 审计实证：33s 超长 cue 只有 1 个 source span 时按 6s 硬切成 5-6 片，
        # 产生 "和负担啊。"、"的人生中，即" 等孤儿残句。
        max_by_spans = max(len(spans), 1) + 1
        desired = max(
            1,
            min(char_based, max_by_spans),
            min(time_based, max_by_length, max_by_spans),
        )
    if desired <= 1:
        return [Subtitle(subtitle.id, subtitle.start, subtitle.end, text)]

    pieces = _split_text(text, desired)
    desired = len(pieces)
    boundaries = [start_ms]
    candidate_boundaries = [item["end"] for item in spans[:-1]]
    for position in range(1, desired):
        target = start_ms + round(duration_ms * position / desired)
        if candidate_boundaries:
            available = [value for value in candidate_boundaries if value > boundaries[-1]]
            boundary = min(available, key=lambda value: abs(value - target)) if available else target
            candidate_boundaries = [value for value in candidate_boundaries if value > boundary]
        else:
            boundary = target
        boundary = max(boundaries[-1] + 1, min(end_ms - (desired - position), boundary))
        boundaries.append(boundary)
    boundaries.append(end_ms)
    return [
        Subtitle(
            subtitle.id if index == 0 else int(f"{subtitle.id}{index}"),
            _format_timestamp(boundaries[index]),
            _format_timestamp(boundaries[index + 1]),
            piece,
        )
        for index, piece in enumerate(pieces)
    ]


def redistribute_subtitles(
    subtitles: list[Subtitle], mapping: dict[str, list[dict]],
) -> list[Subtitle]:
    output: list[Subtitle] = []
    previous_end_ms: int | None = None
    for subtitle in subtitles:
        for piece in redistribute_translation(
            subtitle, mapping.get(str(subtitle.id), []),
        ):
            # 双源证据边界交叉（YouTube 与 Whisper 同段落不同边界）时，
            # 回填的显示 cue 会互相重叠/包含。全局 clamp：后一条显示 cue
            # 不得早于前一条结束（+1ms 保证严格递增）。
            start_ms = timestamp_to_ms(piece.start)
            if previous_end_ms is not None:
                start_ms = max(start_ms, previous_end_ms + 1)
            end_ms = max(start_ms + 1, timestamp_to_ms(piece.end))
            if start_ms != timestamp_to_ms(piece.start):
                piece.start = _format_timestamp(start_ms)
            if end_ms != timestamp_to_ms(piece.end):
                piece.end = _format_timestamp(end_ms)
            # 1ms 废 cue 兜底：clamp 后不足 300ms 的片段并入前一条显示 cue，
            # 避免瞬间闪烁不可读（部分重叠的双源候选被 clamp 截断时发生）。
            if end_ms - start_ms < 300 and output and piece.text.strip():
                previous = output[-1]
                previous.text = (previous.text + " " + piece.text).strip()
                # m-03: 并入后必须把前一条的 end 延至被并入片段的实际结束
                # 时间，否则时间轴出现缝隙/错位（审计实证：<300ms 短片段
                # 拼入前行时不延时 → display-map 段数与 final 段数不一致）。
                previous.end = _format_timestamp(end_ms)
                previous_end_ms = end_ms
                continue
            output.append(piece)
            previous_end_ms = max(previous_end_ms or 0, end_ms)
    for index, subtitle in enumerate(output, start=1):
        subtitle.id = index
    return output


def enforce_reading_rhythm(
    subtitles: list[Subtitle],
    *,
    target_chars: int = 18,
    max_duration_ms: int = 6_000,
) -> list[Subtitle]:
    """Deterministically split only cues that are both long and text-dense.

    A short reaction held over a long pause is intentionally left alone.  The
    pass activates only when a cue exceeds the duration limit *and* contains
    enough text for at least two useful pieces.  This keeps text and the outer
    time window unchanged, and makes a second pass a no-op.
    """
    output: list[Subtitle] = []
    minimum_piece_chars = 6
    for cue in subtitles:
        text = str(cue.text or "").strip()
        start_ms = timestamp_to_ms(cue.start)
        end_ms = timestamp_to_ms(cue.end)
        duration_ms = max(1, end_ms - start_ms)
        visible = _visible_length(text)
        if (
            duration_ms <= max_duration_ms
            or visible < max(target_chars * 2, minimum_piece_chars * 2)
        ):
            output.append(Subtitle(cue.id, cue.start, cue.end, text))
            continue

        desired = max(
            math.ceil(duration_ms / max(max_duration_ms, 1)),
            math.ceil(visible / max(target_chars, 1)),
        )
        desired = min(desired, max(2, visible // minimum_piece_chars))
        pieces = _split_text(text, desired)
        if len(pieces) <= 1:
            output.append(Subtitle(cue.id, cue.start, cue.end, text))
            continue

        boundaries = [
            start_ms + round(duration_ms * index / len(pieces))
            for index in range(len(pieces) + 1)
        ]
        boundaries[0] = start_ms
        boundaries[-1] = end_ms
        output.extend(
            Subtitle(
                cue.id,
                _format_timestamp(boundaries[index]),
                _format_timestamp(boundaries[index + 1]),
                piece,
            )
            for index, piece in enumerate(pieces)
        )

    for index, cue in enumerate(output, start=1):
        cue.id = index
    return output


def enforce_minimum_duration(
    subtitles: list[Subtitle],
    *,
    min_ms: int = 300,
    max_extension_ms: int = 1_000,
) -> list[Subtitle]:
    """Post-pass guaranteeing every display cue is at least ``min_ms`` long.

    ``redistribute_subtitles`` 的 <300ms 兜底把短片段并入**前一条**，但首条
    display cue 没有前一条可并，会漏出 10ms 闪帧 cue（2026-09-06 真实任务
    实测：'00:00:03,550 --> 00:00:03,560'）。本后处理补上这个洞：

    - 有右邻且空隙足够 → 顺延 end 到 ``min(start+max_extension_ms,
      next_start-1)``，数学上不产生任何新重叠（validator 的 >250ms 重叠
      是 error 红线）；
    - 相邻无空隙 → 文本空格并入下一条后删除本条，下一条时间窗保持不动；
    - 末条无右邻 → 直接顺延到最小可读时长。

    处理后无 <min_ms 残留，因此幂等：审校页每次保存都会重写 reviewed SRT
    并重跑本函数，二次执行必须零改动。调用点：``long_video._publish_final``
    与 ``jobs.JobManager._write_reviewed_srt`` 两条发布管线。
    """
    result: list[Subtitle] = list(subtitles)
    index = 0
    while index < len(result):
        cue = result[index]
        start_ms = timestamp_to_ms(cue.start)
        end_ms = timestamp_to_ms(cue.end)
        if end_ms - start_ms >= min_ms:
            index += 1
            continue
        if index + 1 >= len(result):
            cue.end = _format_timestamp(max(end_ms, start_ms + min_ms))
            index += 1
            continue
        next_start_ms = timestamp_to_ms(result[index + 1].start)
        target_end = min(start_ms + max_extension_ms, next_start_ms - 1)
        if target_end - start_ms >= min_ms and target_end > end_ms:
            cue.end = _format_timestamp(target_end)
            index += 1
            continue
        # 空隙不足以达到 min_ms：文本前向并入下一条并删除本条。
        # 不改下一条的 start/end，避免与上一条产生新重叠。
        result[index + 1].text = f"{cue.text} {result[index + 1].text}".strip()
        result.pop(index)
        # 不前进 index：归并后的下一条自身可能仍 <min_ms，需要继续处理。
    for position, cue in enumerate(result, start=1):
        cue.id = position
    return result
