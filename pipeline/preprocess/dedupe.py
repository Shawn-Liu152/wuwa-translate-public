"""
YouTube SRT Deduplicator

两步去重：
  Step 1: 旧版合并 — 相似度>=50%的滚动窗口合并取更长版本（451→~132条）
  Step 2: 修剪开头重复 — 去掉每条开头与前一条结尾重复的词（保留前句，删后句重复）

效果：每条字幕只包含新增内容，不再有滚动窗口重复，条目数和时间轴基本不变。
"""
import re
import statistics
import unicodedata
from typing import List


_CENSORED_MARKER_RE = re.compile(
    r"\[\s*(?:\\h\s*)?_{2,}(?:\s*\\h)?\s*\]",
    re.IGNORECASE,
)

_QUESTION_MARKS = frozenset("?？¿؟")
_SENTENCE_END_RE = re.compile(r"[.!?。！？…]+[\"'”’）)\]]*$")


def _window_normalise(text: str) -> str:
    """Return a comparison stream while retaining semantics separately."""
    return re.sub(
        r"[\W_]+",
        "",
        unicodedata.normalize("NFKC", str(text or "")).casefold(),
    )


def _window_tokens(text: str) -> list[str]:
    """Tokenise whitespace-delimited captions without language word lists."""
    return re.findall(
        r"[^\W_]+(?:['’][^\W_]+)?",
        unicodedata.normalize("NFKC", str(text or "")).casefold(),
    )


def _contains_contiguous_tokens(shorter: list[str], longer: list[str]) -> bool:
    if not shorter or len(shorter) > len(longer):
        return False
    width = len(shorter)
    return any(
        longer[index:index + width] == shorter
        for index in range(len(longer) - width + 1)
    )


def _is_subsequence(shorter: str, longer: str) -> bool:
    iterator = iter(longer)
    return all(character in iterator for character in shorter)


def _rolling_window_relation(
    left_text: str,
    right_text: str,
    *,
    minimum_length: int,
    minimum_coverage: float = 0.45,
) -> str | None:
    """Classify a safely collapsible rolling-window relationship.

    The result names the text that dominates (``left``/``right``), or
    ``equivalent``.  A similarity score is deliberately insufficient here:
    token insertions/replacements, numeric changes, and question changes can
    alter source truth despite very high character similarity.
    """
    left = _window_normalise(left_text)
    right = _window_normalise(right_text)
    if not left or not right:
        return None

    left_numbers = tuple(re.findall(r"\d+(?:[.,]\d+)?", left_text))
    right_numbers = tuple(re.findall(r"\d+(?:[.,]\d+)?", right_text))
    if left_numbers != right_numbers and (left_numbers or right_numbers):
        return None

    left_question = any(mark in left_text for mark in _QUESTION_MARKS)
    right_question = any(mark in right_text for mark in _QUESTION_MARKS)
    if left == right:
        return None if left_question != right_question else "equivalent"
    if min(len(left), len(right)) < minimum_length:
        return None

    if len(left) <= len(right):
        shorter, longer = left, right
        shorter_text, longer_text = left_text, right_text
        dominant = "right"
    else:
        shorter, longer = right, left
        shorter_text, longer_text = right_text, left_text
        dominant = "left"

    coverage = len(shorter) / max(len(longer), 1)
    if coverage < minimum_coverage:
        return None

    anchored_containment = (
        longer.startswith(shorter) or longer.endswith(shorter)
    )

    # Whitespace-delimited captions expose semantic edits as token structure.
    # Only a contiguous prefix/suffix can be a safe rolling expansion; an
    # insertion or replacement inside the sentence remains a distinct truth.
    shorter_tokens = _window_tokens(shorter_text)
    longer_tokens = _window_tokens(longer_text)
    uses_word_boundaries = (
        bool(re.search(r"\s", shorter_text))
        and bool(re.search(r"\s", longer_text))
        and len(shorter_tokens) >= 2
    )
    if uses_word_boundaries:
        if not _contains_contiguous_tokens(shorter_tokens, longer_tokens):
            return None
        token_width = len(shorter_tokens)
        token_at_boundary = (
            longer_tokens[:token_width] == shorter_tokens
            or longer_tokens[-token_width:] == shorter_tokens
        )
        if not token_at_boundary:
            return None
    elif not anchored_containment:
        # For unsegmented CJK, permit only a high-coverage pure insertion.
        # This preserves established ASR enrichment such as an added adverb,
        # while suffix replacements and other mutations stay separate.
        if coverage < 0.80 or not _is_subsequence(shorter, longer):
            return None

    if left_question != right_question:
        # A bare punctuation flip is semantic.  The sole safe exception is a
        # substantially longer row that retains the marked text plus other
        # source content (for example a two-speaker rolling window).
        longer_has_question = any(
            mark in longer_text for mark in _QUESTION_MARKS
        )
        if not longer_has_question or coverage >= 0.80:
            return None

    shorter_is_complete = bool(
        _SENTENCE_END_RE.search(str(shorter_text).strip())
    )
    if longer.startswith(shorter) and shorter_is_complete:
        return None

    return dominant


def _join_caption_text(left: str, right: str) -> str:
    """Join a rolling-caption continuation without damaging CJK text."""
    left = str(left or "").rstrip()
    right = str(right or "").lstrip()
    if not left:
        return right
    if not right:
        return left
    if right.startswith(">>"):
        return f"{left}\n{right}"
    separator = (
        " "
        if re.search(r"[A-Za-z0-9.!?,;:]$", left)
        and re.match(r"[A-Za-z0-9]", right)
        else ""
    )
    return f"{left}{separator}{right}"


def repair_rolling_overlaps(
    subtitles: list,
    *,
    source_language: str = "en",
    min_overlap_ms: int = 1_000,
    min_duration_ms: int = 400,
    max_chars: int = 300,
) -> list:
    """修复 YouTube 滚动式字幕：相邻 cue 时间重叠但文本不重复时裁剪为首尾相接。

    YouTube 自动字幕是滑动窗口结构：每行显示约 5 秒、窗口步进约 2.5 秒，
    相邻 cue 重叠 50% 以上，但文本不重复（后半行是上一行的延续）。
    这类重叠会经 display-map 回填传染到最终 SRT 并被交付验证器拦截。

    - 重叠 >= min_overlap_ms 且缺少完整滚动窗口结构证据（衔接型）→
      前一条 end 裁剪到后一条 start，形成首尾相接序列。
    - 裁剪后前一条过短（< min_duration_ms）→ 让后一条吸收前一条
      （文本拼接、start 前移），保持完整滚动窗口。
    - 无重叠或有安全的双向覆盖/边界扩展证据时，cue 原样保留。
    """
    from pipeline.parser.srt_parser import Subtitle, timestamp_to_ms

    work = [
        Subtitle(item.id, item.start, item.end, str(item.text or "").strip())
        for item in subtitles
        if str(item.text or "").strip()
    ]
    if len(work) < 2:
        return work

    repaired: list[Subtitle] = []
    for item in work:
        if repaired:
            previous = repaired[-1]
            overlap_ms = (
                timestamp_to_ms(previous.end) - timestamp_to_ms(item.start)
            )
            if overlap_ms >= min_overlap_ms:
                same_window = _rolling_window_relation(
                    previous.text,
                    item.text,
                    minimum_length=5 if source_language == "en" else 2,
                )
                if same_window is None:
                    clipped_duration_ms = (
                        timestamp_to_ms(item.start)
                        - timestamp_to_ms(previous.start)
                    )
                    if clipped_duration_ms >= min_duration_ms:
                        previous.end = item.start
                    elif len(previous.text) + len(item.text) <= max_chars:
                        joiner = "" if source_language == "ja" else " "
                        item.text = (
                            f"{previous.text}{joiner}{item.text.lstrip()}"
                        )
                        item.start = previous.start
                        repaired.pop()
        repaired.append(item)
    return repaired


def coalesce_micro_cues(
    subtitles: list,
    *,
    max_duration_ms: int = 50,
) -> list:
    """Merge meaningful rolling-caption micro cues into a readable neighbour.

    YouTube sometimes emits a 10 ms cue containing the final word of a
    sentence. The duration is display noise, but the text is not. Duplicate
    fragments are absorbed into the matching neighbour; unique fragments are
    appended to the unfinished previous cue or prepended to the next cue.
    """
    from pipeline.parser.srt_parser import Subtitle, timestamp_to_ms

    work = [
        Subtitle(item.id, item.start, item.end, str(item.text or "").strip())
        for item in subtitles
        if str(item.text or "").strip()
    ]
    merged: list[Subtitle] = []

    def normalise(text: str) -> str:
        return " ".join(re.findall(r"[a-z0-9']+", text.casefold()))

    for index, current in enumerate(work):
        if not current.text.strip():
            continue
        duration_ms = (
            timestamp_to_ms(current.end) - timestamp_to_ms(current.start)
        )
        if duration_ms > max_duration_ms:
            merged.append(current)
            continue

        previous = merged[-1] if merged else None
        following = work[index + 1] if index + 1 < len(work) else None
        fragment = normalise(current.text)
        previous_text = normalise(previous.text) if previous else ""
        following_text = normalise(following.text) if following else ""
        starts_new_speaker = current.text.lstrip().startswith(">>")
        previous_is_open = bool(
            previous
            and not re.search(r"[.!?。！？…][\"'”’)]?$", previous.text.strip())
        )
        starts_lowercase = bool(
            re.match(r"[a-z]", current.text.lstrip().lstrip(">").lstrip())
        )

        if fragment and previous_text.endswith(fragment):
            previous.end = current.end
            continue
        if fragment and following_text.startswith(fragment):
            if previous and previous_is_open and not starts_new_speaker:
                previous.text = _join_caption_text(
                    previous.text, current.text
                )
                previous.end = current.end
                following.text = re.sub(
                    rf"^\s*{re.escape(current.text.strip())}",
                    "",
                    following.text,
                    count=1,
                    flags=re.IGNORECASE,
                ).lstrip()
                continue
            following.start = current.start
            continue

        if previous and not starts_new_speaker and (
            previous_is_open or starts_lowercase or not following
        ):
            previous.text = _join_caption_text(previous.text, current.text)
            previous.end = current.end
            continue
        if following:
            following.text = _join_caption_text(current.text, following.text)
            following.start = current.start
            continue
        if previous:
            previous.text = _join_caption_text(previous.text, current.text)
            previous.end = current.end
            continue
        merged.append(current)

    return merged


def _dedupe_join(left: str, right: str, source_language: str = "en") -> str:
    """Join two cue texts, removing YouTube rolling-window duplicates.

    YouTube's sliding window often emits cue 2 = cue 1 + new text.
    Naive concatenation produces duplicate content that infects the union
    and ultimately the translation.  This function detects containment
    and head-tail overlap, returning only the unique portion.
    """
    left = str(left or "").strip()
    right = str(right or "").strip()
    if not left:
        return right
    if not right:
        return left

    if source_language in {"ja", "ko"}:
        left_norm = re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", left))
        right_norm = re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", right))
    else:
        left_norm = re.sub(r"\s+", "", left.lower())
        right_norm = re.sub(r"\s+", "", right.lower())

    # Case 1: right contains left (right is a superset) → take right
    if len(left_norm) >= 5 and left_norm in right_norm:
        return right
    # Case 2: left contains right (left is a superset) → take left
    if len(right_norm) >= 5 and right_norm in left_norm:
        return left

    # Case 3: head-tail overlap (right starts with left's tail)
    max_check = min(len(left_norm), len(right_norm), 50)
    best_overlap = 0
    for length in range(max_check, 2, -1):
        if left_norm[-length:] == right_norm[:length]:
            best_overlap = length
            break
    if best_overlap > 0:
        if source_language in {"ja", "ko"}:
            # For CJK, trim characters from the start of right until
            # the normalized prefix no longer matches the overlap.
            trimmed = right
            trimmed_norm = right_norm
            while trimmed and len(trimmed_norm) > len(right_norm) - best_overlap:
                trimmed = trimmed[1:]
                trimmed_norm = re.sub(
                    r"[\s\W_]+", "",
                    unicodedata.normalize("NFKC", trimmed),
                )
            if trimmed.strip():
                joiner = "" if source_language == "ja" else " "
                return f"{left}{joiner}{trimmed.strip()}"
            return left
        else:
            left_words = left.lower().split()
            right_words = right.lower().split()
            for n in range(min(len(left_words), len(right_words), 15), 0, -1):
                if left_words[-n:] == right_words[:n]:
                    remaining = " ".join(right.split()[n:])
                    if remaining:
                        return f"{left} {remaining}"
                    return left

    # No overlap → join normally
    if source_language == "ko":
        return f"{left} {right}"
    return _join_caption_text(left, right)


def merge_fragment_openings(
    subtitles: list,
    *,
    source_language: str = "en",
    max_gap_ms: int = 1_000,
    max_duration_ms: int = 16_000,
    max_characters: int = 240,
) -> list:
    """把以接续残片开头的单元并入前一单元（跨滚动窗口残句修复）。

    YouTube 滚动字幕把完整句切碎时，下一窗口可能以接续助词/承接片段
    开头（日语：こその/だろうな/だから 等；韩语：그래서/그니까 等）。
    这类残句若独立成翻译单元，模型拿残句翻译必然错位（2026-08-10
    JA 实证：id=76 `こその...`、id=77 `だろうな...` 23 单元错位）。

    必须在 coalesce 的早退保护（median 时长判定）之前执行——长 cue
    视频中残句单元会被早退跳过合并，真实 Job 因此仍错位。
    """
    from pipeline.parser.srt_parser import Subtitle, timestamp_to_ms

    work = [
        Subtitle(item.id, item.start, item.end, str(item.text or "").strip())
        for item in subtitles
        if str(item.text or "").strip()
    ]
    if source_language not in {"en", "ja", "ko"} or len(work) < 2:
        return work

    fragment_openers_ja = (
        "こその", "だから", "それで", "そして", "なので", "ですが",
        "だろうな", "でしょう", "ですね", "ですよ", "ますね", "なんだ",
        "っていう", "って",
    )
    fragment_openers_ko = (
        "그래서", "그러니까", "그런데", "그리고", "그래도", "하지만",
        "그니까", "그거", "그게", "그건", "라고", "이라고", "해서",
        "면서", "니까", "는데",
    )
    openers = (
        fragment_openers_ja if source_language == "ja"
        else fragment_openers_ko
    )

    merged: list[Subtitle] = []
    for item in work:
        stripped = item.text.lstrip()
        fragment_opening = (
            bool(re.match(r"[a-z]", stripped))
            if source_language == "en"
            else any(stripped.startswith(opener) for opener in openers)
        )
        if (
            merged
            and not stripped.startswith(">>")
            and fragment_opening
        ):
            previous = merged[-1]
            gap_ms = timestamp_to_ms(item.start) - timestamp_to_ms(previous.end)
            projected_duration = (
                timestamp_to_ms(item.end) - timestamp_to_ms(previous.start)
            )
            projected_characters = len(previous.text) + len(item.text)
            within_english_bounds = (
                source_language != "en"
                or (
                    projected_duration <= max_duration_ms
                    and projected_characters <= max_characters
                )
            )
            if gap_ms <= max_gap_ms and within_english_bounds:
                previous.text = _dedupe_join(
                    previous.text, item.text, source_language=source_language,
                )
                previous.end = item.end
                continue
        merged.append(item)
    # 重新编号保持后续逻辑稳定
    for index, item in enumerate(merged, start=1):
        item.id = index
    return merged


def coalesce_semantic_cues(
    subtitles: list,
    *,
    source_language: str = "en",
    max_duration_ms: int = 16_000,
    max_characters: int = 140,
) -> list:
    """Reassemble rapidly split source cues into complete translation units.

    CapCut's speech timeline is useful, but its Japanese export commonly puts
    particles, names and the final predicate in separate sub-second cues.  A
    model forced to return one translation per such cue cannot recover natural
    Chinese syntax.  This function keeps coarse, sentence-like sources intact
    and only groups a genuinely granular Japanese timeline.
    """
    from pipeline.parser.srt_parser import Subtitle, timestamp_to_ms

    work = [
        Subtitle(item.id, item.start, item.end, str(item.text or "").strip())
        for item in subtitles
        if str(item.text or "").strip()
    ]
    if source_language not in {"en", "ja", "ko"} or len(work) < 2:
        return work

    # 残句开头回溯合并：必须在早退保护之前执行，否则长 cue 视频中的
    # 残句单元会被 median 时长早退跳过（2026-08-10 JA 真实 Job 实证）。
    work = merge_fragment_openings(
        work, source_language=source_language,
    )
    if len(work) < 2:
        return work

    durations = [
        max(0, timestamp_to_ms(item.end) - timestamp_to_ms(item.start))
        for item in work
    ]
    short_limit = 8 if source_language == "ja" else 14
    short_text_ratio = sum(len(item.text) <= short_limit for item in work) / len(work)
    sentence_end = (
        re.compile(r"[.!?…]+[\"'”’）)\]]*$")
        if source_language == "en" else
        re.compile(
            r"(?:[。！？!?…]+|"
            r"こんにちは|こんばんは|おはようございます|"
            r"(?:です|ます|ません|でした|ました|ください|でしょう|"
            r"となります|になります|できます|できません|ありません|"
            r"しましょう|ましょう|と思います|と思う)(?:ね|よ|か|よね|かね)?)$"
        )
        if source_language == "ja" else
        re.compile(
            r"(?:[.!?！？…]+|(?:습니다|ㅂ니다|입니다|어요|아요|예요|이에요|네요|군요|"
            r"죠|지요|할게요|볼게요|마세요|세요)(?:[.!?！？…]+)?)$"
        )
    )
    if source_language == "en":
        if statistics.median(durations) > 2_500:
            return work
    elif statistics.median(durations) > 2_000 and short_text_ratio < 0.35:
        # YouTube 滚动字幕行以"半句"居多（时长长但未终止），
        # 仅凭时长早退会漏掉它们，必须等语义合并把它们拼成完整句。
        non_terminated_ratio = sum(
            1 for item in work
            if not sentence_end.search(item.text.strip())
        ) / len(work)
        if non_terminated_ratio < 0.5:
            return work
    grouped: list[Subtitle] = []
    current: Subtitle | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        current.id = len(grouped) + 1
        grouped.append(current)
        current = None

    for item in work:
        if current is None:
            current = Subtitle(item.id, item.start, item.end, item.text)
        else:
            gap_ms = timestamp_to_ms(item.start) - timestamp_to_ms(current.end)
            projected_duration = timestamp_to_ms(item.end) - timestamp_to_ms(current.start)
            projected_characters = len(current.text) + len(item.text)
            if (
                gap_ms >= 1_000
                or item.text.lstrip().startswith(">>")
                or projected_duration > max_duration_ms
                or projected_characters > max_characters
            ):
                flush()
                current = Subtitle(item.id, item.start, item.end, item.text)
            else:
                current.text = _dedupe_join(
                    current.text, item.text, source_language=source_language,
                )
                current.end = item.end
        if current is not None and sentence_end.search(current.text.strip()):
            flush()
    flush()
    return grouped


def strip_censored_markers(text: str) -> str:
    """Remove YouTube profanity placeholders while preserving spoken text."""
    cleaned_lines = []
    for line in str(text or "").splitlines():
        line = _CENSORED_MARKER_RE.sub(" ", line)
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def clean_export_subtitles(subtitles: list) -> list:
    """Remove caption noise and collapse adjacent rolling-caption rows."""
    from pipeline.parser.srt_parser import Subtitle, timestamp_to_ms

    def clean_text(text: str) -> str:
        text = strip_censored_markers(text)
        # `>>` 是 YouTube 自动字幕的说话人分隔符（不同说话人的话），
        # 不是 "accumulated context"。丢弃 `>>` 后的内容会静默删除
        # 第二说话人的台词（2026-08-10 KO 实证：22 行丢 21 行）。
        # 分隔符保留为空格分隔的同一显示行，全部说话内容都不丢失。
        if ">>" in text:
            parts = [part.strip() for part in text.split(">>")]
            text = " ".join(part for part in parts if part)
        return re.sub(r"[ \t]+", " ", text).strip()

    prepared = []
    for source in subtitles:
        text = clean_text(source.text)
        if not text:
            continue
        prepared.append(Subtitle(
            id=source.id,
            start=source.start,
            end=source.end,
            text=text,
        ))

    cleaned = []
    for current in coalesce_micro_cues(prepared):
        if cleaned:
            previous = cleaned[-1]
            raw_gap_ms = (
                timestamp_to_ms(current.start)
                - timestamp_to_ms(previous.end)
            )
            gap_ms = max(0, raw_gap_ms)
            minimum_length = 4 if raw_gap_ms < 0 else 6
            relation = _rolling_window_relation(
                previous.text,
                current.text,
                minimum_length=minimum_length,
                minimum_coverage=0.25 if raw_gap_ms < 0 else 0.45,
            )
            if gap_ms <= 3_000 and relation is not None:
                previous.end = max(previous.end, current.end)
                if relation == "right":
                    previous.text = current.text
                continue
        cleaned.append(current)
    return cleaned


def dedupe_youtube_subs(subtitles: list, source_language: str = "en") -> list:
    """YouTube 滚动窗口字幕去重"""
    if len(subtitles) < 2:
        return subtitles

    from pipeline.parser.srt_parser import Subtitle

    source_language = str(source_language or "en").strip().lower()

    # 清理：去除纯空格/空文本
    clean = []
    for s in subtitles:
        text = strip_censored_markers(s.text)
        if not text or (source_language == "en" and len(text) < 3):
            continue
        clean.append(Subtitle(id=s.id, start=s.start, end=s.end, text=text))

    if len(clean) < 2:
        return clean

    # ================================================================
    # Step 1: 仅合并有结构证据的同一窗口，并安全保留更长版本。
    #          时间间隔超过15秒的不算同一窗口（避免误合并歌词）。
    # ================================================================
    MAX_WINDOW_GAP_SEC = 15.0  # 同一窗口最大时间间隔

    def _is_same_window(prev: str, cur: str,
                        prev_end: str = None, cur_start: str = None) -> bool:
        """
        判断两条字幕是否是同一句话的不同窗口。

        新增保护：
        - 时间间隔超过 MAX_WINDOW_GAP_SEC（15秒）→ 不视为同一窗口
        - 内容含 [singing] 标记 → 不合并（歌词重复结构不应被去重）
        """
        minimum_length = 5 if source_language == "en" else 2
        if len(cur) < minimum_length or len(prev) < minimum_length:
            return False

        # 含歌唱标记的行不参与合并（歌词副歌不应被去重）
        if '[singing]' in prev.lower() or '[singing]' in cur.lower():
            return False

        # 时间间隔判断：超过15秒的不算同一窗口
        if prev_end and cur_start:
            gap = _ts_to_sec(cur_start) - _ts_to_sec(prev_end)
            if gap > MAX_WINDOW_GAP_SEC:
                return False

        relation = _rolling_window_relation(
            prev,
            cur,
            minimum_length=minimum_length,
        )
        return relation is not None

    groups = []
    cur_group_text = clean[0].text
    group_start = clean[0].start
    group_end = clean[0].end

    for sub in clean[1:]:
        if _is_same_window(cur_group_text, sub.text,
                          prev_end=group_end, cur_start=sub.start):
            if len(sub.text) > len(cur_group_text):
                cur_group_text = sub.text
            group_end = sub.end
        else:
            groups.append((group_start, group_end, cur_group_text))
            cur_group_text = sub.text
            group_start = sub.start
            group_end = sub.end
    groups.append((group_start, group_end, cur_group_text))

    # 生成 Step1 结果（跳过超短残片）
    merged = []
    for start, end, text in groups:
        dur = _ts_to_sec(end) - _ts_to_sec(start)
        if dur < 0.10 and len(text) < 10:
            continue
        merged.append(Subtitle(id=len(merged) + 1, start=start, end=end, text=text))

    # ================================================================
    # Step 2: 修剪开头重复 — 去掉每条开头与前一条结尾重复的词
    #         保留前句完整，删掉后句开头的重复部分
    # ================================================================
    def _find_head_tail_overlap(prev: str, cur: str) -> int:
        """找到 prev 结尾和 cur 开头重叠的词数"""
        if source_language == "ja":
            prev_text = unicodedata.normalize("NFKC", prev)
            cur_text = unicodedata.normalize("NFKC", cur)
            max_overlap = min(len(prev_text), len(cur_text), 30)
            for count in range(max_overlap, 1, -1):
                if prev_text[-count:] == cur_text[:count]:
                    return count
            return 0
        # 2026-08-11: 用 _window_tokens（剥离标点）替代 raw split()。
        # KO 实证（holdout-a8ZSJPGGbbg 166/167）：`여친입니다.` vs `여친입니다`
        # 标点变体导致 tail superset 未识别 → 字幕重复。
        # 词数在剥离标点前后一致（标点粘连在词上），overlap 可直接用于裁剪索引。
        p_words = _window_tokens(prev)
        c_words = _window_tokens(cur)
        max_overlap = min(len(p_words), len(c_words), 15)
        for n in range(max_overlap, 1, -1):
            if p_words[-n:] == c_words[:n]:
                return n
        return 0

    result = []
    prev_text = merged[0].text
    result.append(merged[0])  # 第一条原样保留

    for i in range(1, len(merged)):
        cur = merged[i]
        # 含歌唱标记的行跳过 Step2 修剪（保留完整歌词）
        if '[singing]' in cur.text.lower() or '[singing]' in prev_text.lower():
            result.append(Subtitle(
                id=len(result) + 1, start=cur.start, end=cur.end, text=cur.text
            ))
            prev_text = cur.text
            continue
        overlap = _find_head_tail_overlap(prev_text, cur.text)
        if overlap > 0:
            # 去掉 cur 开头重复的词，只保留新增部分
            if source_language == "ja":
                new_text = cur.text[overlap:]
            else:
                cur_words = cur.text.split()
                # Codex review fix: _window_tokens 计数不含独立标点 token，
                # raw split 含；需要把前 overlap 个有内容 token 及其间标点
                # 一并跳过，否则索引错位会留下重复词（'wait ! here' 场景）。
                seen = 0
                skip = 0
                for idx, w in enumerate(cur_words):
                    if re.search(r"[A-Za-z0-9\uac00-\ud7af\u3040-\u30ff]", w):
                        seen += 1
                        skip = idx + 1
                        if seen >= overlap:
                            break
                new_text = " ".join(cur_words[skip:])
            if new_text.strip():
                result.append(Subtitle(
                    id=len(result) + 1, start=cur.start, end=cur.end, text=new_text
                ))
                prev_text = new_text
            # 去重后为空（完全重复）→ 跳过
        else:
            result.append(Subtitle(
                id=len(result) + 1, start=cur.start, end=cur.end, text=cur.text
            ))
            prev_text = cur.text

    return result


def _ts_to_sec(ts: str) -> float:
    import re
    m = re.match(r'(\d{2}):(\d{2}):(\d{2}),(\d{3})', ts)
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000.0
