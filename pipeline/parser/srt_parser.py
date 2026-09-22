"""
SRT Parser — 手写实现，零外部依赖

职责：
- 读取 SRT 文件（UTF-8 编码）
- 解析为 Subtitle 对象列表
- 将 Subtitle 对象列表写回 SRT 文件
- 保证往返一致性：parse() → save() 后文件内容完全不变
"""
import re
from dataclasses import dataclass
from typing import List


@dataclass
class Subtitle:
    """单条字幕"""
    id: int
    start: str       # 格式: "00:00:01,000"
    end: str         # 格式: "00:00:03,000"
    text: str        # 原始文本（可能包含多行）


# SRT 时间戳正则: HH:MM:SS,mmm
TIMESTAMP_RE = re.compile(r'^(\d{2}):(\d{2}):(\d{2}),(\d{3})$')

# SRT 序号行正则: 纯数字
INDEX_RE = re.compile(r'^(\d+)\s*$')

# SRT 时间范围行: 00:00:01,000 --> 00:00:03,000
TIMERANGE_RE = re.compile(
    r'^(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*$'
)


def parse_srt(filepath: str) -> List[Subtitle]:
    """
    解析 SRT 文件，返回 Subtitle 对象列表。

    Args:
        filepath: SRT 文件路径

    Returns:
        List[Subtitle]: 按 id 排序的字幕列表

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: SRT 格式错误
    """
    # 读取整个文件内容
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        raw = f.read()

    # 规范化换行符
    raw = raw.replace('\r\n', '\n').replace('\r', '\n')
    # 按双换行分割字幕块
    blocks = raw.strip().split('\n\n')

    subtitles = []
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 2:
            continue

        # 第一行: 序号
        m = INDEX_RE.match(lines[0].strip())
        if not m:
            raise ValueError(f"期望字幕序号，得到: '{lines[0]}'")
        current_id = int(m.group(1))

        # 第二行: 时间范围
        m = TIMERANGE_RE.match(lines[1].strip())
        if not m:
            raise ValueError(f"期望时间范围，得到: '{lines[1]}' (id={current_id})")
        current_start = m.group(1)
        current_end = m.group(2)

        # 剩余行: 文本（过滤掉纯空格的"空行"）
        text_lines = [l for l in lines[2:] if l.strip()]

        subtitles.append(Subtitle(
            id=current_id,
            start=current_start,
            end=current_end,
            text='\n'.join(text_lines) if text_lines else '',
        ))

    return subtitles


def timestamp_to_ms(timestamp: str) -> int:
    """Convert an SRT timestamp to milliseconds."""
    match = TIMESTAMP_RE.match(timestamp)
    if not match:
        raise ValueError(f"非法 SRT 时间戳: '{timestamp}'")
    hours, minutes, seconds, milliseconds = map(int, match.groups())
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"非法 SRT 时间戳: '{timestamp}'")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def ms_to_timestamp(milliseconds: int) -> str:
    """Convert milliseconds to an SRT timestamp, clamping negative values to zero."""
    milliseconds = max(0, int(milliseconds))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def subtitle_key(subtitle: Subtitle) -> str:
    """Return a stable key for matching a subtitle across renumbered artifacts."""
    return f"{subtitle.start}|{subtitle.end}|{subtitle.id}"


def overlap_ms(first: Subtitle, second: Subtitle) -> int:
    """Return the positive overlap between two subtitle intervals in milliseconds."""
    return max(0, min(timestamp_to_ms(first.end), timestamp_to_ms(second.end))
               - max(timestamp_to_ms(first.start), timestamp_to_ms(second.start)))


def coverage_ratio(candidate: Subtitle, reference: Subtitle) -> float:
    """Return the proportion of candidate's interval covered by reference."""
    duration = timestamp_to_ms(candidate.end) - timestamp_to_ms(candidate.start)
    return overlap_ms(candidate, reference) / duration if duration > 0 else 0.0


def save_srt(filepath: str, subtitles: List[Subtitle]) -> None:
    """
    将 Subtitle 对象列表写入 SRT 文件。

    Args:
        filepath: 输出文件路径
        subtitles: Subtitle 对象列表
    """
    with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
        for i, sub in enumerate(subtitles):
            if i > 0:
                f.write('\n')  # 字幕块之间空行分隔
            f.write(f"{sub.id}\r\n")
            f.write(f"{sub.start} --> {sub.end}\r\n")
            f.write(f"{sub.text}\r\n")


def generate_sample_subtitles(count: int = 1000) -> List[Subtitle]:
    """
    生成指定数量的示例字幕，用于测试。

    覆盖：
    - 纯英文
    - 中英混排
    - Unicode 音乐符号
    - 日文假名
    - 多行文本
    - 特殊字符（引号、破折号等）
    """
    samples = []
    for i in range(1, count + 1):
        # 时间线递进
        start_sec = (i - 1) * 5
        end_sec = start_sec + 4
        start = f"00:{start_sec // 60:02d}:{start_sec % 60:02d},000"
        end = f"00:{end_sec // 60:02d}:{end_sec % 60:02d},000"

        # 文本类型轮换覆盖各种边界情况
        mod = i % 12
        if mod == 0:
            text = f"This is subtitle number {i}."
        elif mod == 1:
            text = f"Hello\nWorld"
        elif mod == 2:
            text = f"Rover, let's go!"
        elif mod == 3:
            text = f"Changli said: \"The Echo is powerful.\""
        elif mod == 4:
            text = f"Scar\nis here\nwith friends"
        elif mod == 5:
            text = "♪ BGM ♪"
        elif mod == 6:
            text = f"こんにちは\nHello"
        elif mod == 7:
            text = f"It's 100% correct—or not?"
        elif mod == 8:
            text = "(no dialogue)"
        elif mod == 9:
            text = "Line 1\nLine 2\nLine 3"
        elif mod == 10:
            text = f"Camelia & Mornye: The Astrite"
        elif mod == 11:
            text = f"[Music]\n♪ ♫ ♪"

        samples.append(Subtitle(id=i, start=start, end=end, text=text))
    return samples


def save_sample_srt(filepath: str, count: int = 1000) -> None:
    """生成测试用的 SRT 文件"""
    subs = generate_sample_subtitles(count)
    save_srt(filepath, subs)
    print(f"[OK] 生成 {count} 条测试字幕 → {filepath}")
