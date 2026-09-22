"""
Japanese Filter

检测并过滤日文字幕行。

职责：
- 检测文本中是否包含平假名（U+3040–U+309F）或片假名（U+30A0–U+30FF）
- 删除含日文的行，保留纯英文行
- 对于日英混排行：仅删除日文字符，保留英文部分

Unicode 范围：
- 平假名：U+3040 – U+309F
- 片假名：U+30A0 – U+30FF
- 片假名语音扩展：U+31F0 – U+31FF（罕见但存在）
- 日文标点（如「」、。）也会随着所在行被删除
"""
import re
import unicodedata


# 日文字符正则：平假名 + 片假名 + 片假名扩展
_JAPANESE_CHAR_RE = re.compile(
    r'[぀-ゟ゠-ヿㇰ-ㇿ]'
)

# 匹配含日文的整行（用于按行过滤）
_JAPANESE_LINE_RE = re.compile(
    r'[぀-ゟ゠-ヿㇰ-ㇿ]'
)

# 非英文字符正则：包含中日韩统一表意文字（含常用汉字）、韩文、阿拉伯文、泰文、西里尔文等
_NON_ENGLISH_RE = re.compile(
    r'['
    r'一-鿿'     # CJK 统一表意文字（中文汉字）
    r'぀-ゟ'     # 平假名
    r'゠-ヿ'     # 片假名
    r'가-힯'     # 韩文
    r'؀-ۿ'     # 阿拉伯文
    r'฀-๿'     # 泰文
    r'Ѐ-ӿ'     # 西里尔文（俄文等）
    r']'
)

def has_non_english(text: str) -> bool:
    """
    判断文本中是否包含非英文字符（中文/日文/韩文/阿拉伯文/泰文/西里尔文等）。

    Args:
        text: 输入文本

    Returns:
        True  — 包含非英文字符
        False — 纯英文/数字/标点
    """
    return bool(_NON_ENGLISH_RE.search(text))


def remove_non_english(text: str) -> str:
    """
    删除文本中的所有非英文字符。

    Args:
        text: 输入文本（可能包含多行）

    Returns:
        过滤后的纯英文文本

    Examples:
        >>> remove_non_english("Hello世界")
        'Hello'
        >>> remove_non_english("こんにちは World")
        'World'
        >>> remove_non_english("안녕하세요")
        ''
    """
    return _NON_ENGLISH_RE.sub('', text).strip()


def has_japanese(text: str) -> bool:
    """
    判断文本中是否包含日文假名。

    Args:
        text: 输入文本

    Returns:
        True  — 包含平假名或片假名
        False — 不含日文
    """
    return bool(_JAPANESE_CHAR_RE.search(text))


def remove_japanese(text: str) -> str:
    """
    删除文本中的日文字符。

    处理逻辑（按行）：
    - 整行只有日文（且无英文） → 删除整行
    - 日英混排行 → 删除日文字符，保留英文
    - 纯英文/中文/其他行 → 原样保留

    Args:
        text: 输入文本（可能包含多行）

    Returns:
        过滤后的文本，空行会被清理

    Examples:
        >>> remove_japanese("Hello")
        'Hello'
        >>> remove_japanese("Hello こんにちは")
        'Hello '
        >>> remove_japanese("わーい")
        ''
    """
    lines = text.split('\n')
    result_lines = []

    for line in lines:
        if not _JAPANESE_LINE_RE.search(line):
            # 不含日文，直接保留
            result_lines.append(line)
        else:
            # 包含日文：删除所有日文字符
            cleaned = _JAPANESE_CHAR_RE.sub('', line).strip()
            # 删除后如果还有内容（英文等），保留
            if cleaned:
                result_lines.append(cleaned)
            # 删除后为空的行 → 跳过（已被完全过滤）

    return '\n'.join(result_lines)


_JAPANESE_STAGE_CUE_RE = re.compile(
    r'^\s*(?:\[\s*(?:音楽|拍手)\s*\]|[（(]\s*(?:音楽|拍手|笑)\s*[)）]|[♪♬]+)\s*$'
)


def clean_japanese_source(text: str) -> str:
    """Normalize Japanese dialogue and remove only explicit non-dialogue cues."""
    lines = []
    for line in unicodedata.normalize("NFKC", str(text or "")).splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line and not _JAPANESE_STAGE_CUE_RE.fullmatch(line):
            lines.append(line)
    return "\n".join(lines)


def filter_japanese_subtitles(subtitles: list) -> list:
    """
    批量过滤字幕列表中的日文行。

    对每条字幕的 text 字段执行 remove_japanese。
    如果字幕文本被完全清空，则标记该字幕为跳过状态（text 置为空字符串）。

    Args:
        subtitles: Subtitle 对象列表

    Returns:
        过滤后的字幕列表（text 中的日文已被清除）
    """
    for sub in subtitles:
        sub.text = remove_japanese(sub.text)
    return subtitles
