"""逐语义单元证据仲裁 + 未知专名防护（Source Arbitration + Entity Guard）。

设计目标（对应项目结构性升级提示词 P0）：
- 不再"整个视频选一个主源"，而是对每个 semantic unit 比较多源证据，
  综合 glossary / ASR corrections / 人物表 / 完整度 / 未知专名惩罚打分，
  选出 canonical source text。
- 两路严重冲突且无可靠证据时，不武断选 primary：标记 source_conflict，
  低置信（unresolved）进人工审校。
- 未知专名只标记、不生成"像真的"中文人名；翻译后二次检查幻觉实体。

本模块为纯确定性规则，不调用 LLM；en 源语言不进入仲裁（保持旧链路）。
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional

from pipeline.languages import normalize_language_pair
from pipeline.parser.srt_parser import timestamp_to_ms

# ---------------------------------------------------------------- 归一化

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_SEMANTIC_TOKEN_RE = re.compile(
    r"[A-Za-z]+|[\uac00-\ud7af]+|[\u3040-\u30ff]+|[\u3400-\u9fff]+"
)

_NEGATION_MARKERS = {
    "ko": ("안", "못", "않", "없", "아니", "말"),
    "ja": ("ない", "無い", "ぬ", "ません", "じゃない", "ではない"),
}
_CJK_NEGATION_MARKERS = ("不", "没", "未", "无", "無", "别", "別")

# 韩文/日文连续音节（专名候选形态）
_KO_RUN = re.compile(r"[\uac00-\ud7af]{2,6}")
_JA_KATAKANA_RUN = re.compile(r"[\u30a1-\u30f6]{2,6}")

# 韩文高频功能词/感叹词（非专名，过滤用）
_KO_STOPWORDS = {
"은", "는", "이", "가", "을", "를", "의", "에", "도", "만", "와", "과",
"나", "다", "지", "죠", "네", "요", "거", "게", "군", "구나", "잖아",
"거든", "니까", "지만", "는데", "면서", "라도", "조차", "부터", "까지",
"하고", "라고", "라는", "하는", "하다", "된다", "있다", "없다", "한다",
"봐", "보자", "봐요", "뭐", "뭐야", "응", "그래", "맞아", "아니",
"진짜", "그냥", "같이", "이제", "지금", "정말", "완전", "너무", "약간",
"또", "왜", "저기", "이거", "그거", "여기", "거기", "뭐지", "어때",
"됐다", "했다", "좋다", "좋아", "싫다", "싫어", "헐", "미쳤다", "미친",
"대박", "존나", "좆", "씨발", "개", "엄청", "되게", "이렇게", "저렇게",
"이런", "저런", "어떻게", "그래서", "근데", "그런데", "하지만", "그리고",
"그럼", "그러면", "그니까", "있잖아", "알지", "알아", "몰라", "괜찮아",
"괜찮다", "가자", "가요", "하지", "하지마", "이야기", "사람", "친구",
"언니", "오빠", "누나", "형", "동생", "아이", "여자", "남자", "캐릭터",
"게임", "스토리", "영상", "화면", "버전", "업데이트", "가챠", "뽑기",
"레벨", "스킬", "대사", "장면", "캐릭", "퀘스트", "맵", "보스",
"에너지", "공격", "방어", "회복", "속도", "시간", "처음",
"다음", "마지막", "솔직히", "확실히", "아마", "어쩌면",
# P1 新增：代词/指示词
"이게", "이건", "이것", "이분", "이곳", "이때", "이번",
"저게", "저건", "저것", "저분", "저곳", "저때",
"그게", "그건", "그것", "그분", "그곳", "그때",
"본인", "자기", "자신", "누구", "무엇", "무슨", "어느",
"어디", "언제", "어떤", "모든", "모두", "아무", "누구나",
"여기서", "거기서", "저기서", "어디서",
# P1 新增：高频普通名词
"것", "수", "일", "때", "곳", "이름", "세상", "인류", "미래",
"희망", "슬픔", "기쁨", "울음", "웃음", "반응", "사람들", "길이",
"생각", "마음", "느낌", "문제", "이유", "방법", "결과", "과정",
"상황", "내용", "부분", "경우", "사실", "정도", "편", "쪽",
"앞", "뒤", "위", "아래", "옆", "안", "밖", "사이",
"오늘", "내일", "어제", "지금", "이제", "방금", "아까",
# P1 新增：高频动词/形容词基本形
"있다", "없다", "되다", "하다", "가다", "오다", "보다", "듣다",
"말하다", "알다", "모르다", "살다", "죽다", "만들다", "같다",
"다르다", "좋다", "싫다", "크다", "작다", "많다", "적다",
"있다", "없다", "않다", "되다", "이다", "아니다",
# P1 新增：副词/接续词
"완전히", "수많은", "같은", "다른", "이런", "저런", "그런",
"아무튼", "어쨌든", "그래도", "오히려", "도리어", "마침",
"마찬가지", "비슷한", "정확한", "분명한", "확실한",
"있어", "있는", "없는", "없어", "같은데", "같이",
"달라", "따라", "관한", "않지만", "않고", "않는",
"사라졌지만", "지속될", "따라간", "무너질", "만든", "죽은",
    # 口语高频（人称+助词 / 动词语尾 / 副词）
    "내가", "네가", "제가", "우리가", "너희가", "우리", "너희", "당신",
    "갈게", "할게", "볼게", "줄게", "올게", "가게", "하게", "될게",
    "이미", "다시", "아직", "먼저", "나중", "함께", "혼자", "계속", "자꾸",
    "어디", "언제", "누가", "이러다", "저러다", "그러다", "어쩌다",
    "보는데", "하는데", "왔는데", "갔는데", "했는데", "있는데",
    "하고싶", "하고파", "하고싶다", "보고싶", "보고싶다",
    # SFX / 舞台提示（非对白）
    "음악", "웃음", "박수", "비명", "한숨", "콧방귀", "노래", "환호",
    "함성", "탄성", "기침", "숨소리", "헉", "신음", "고함", "외침",
    "중얼거림", "휘파람", "박수소리", "웃음소리",
    # 更多口语高频
    "거야", "보여", "제발", "있었던", "설마", "와씨", "아니야", "잠깐만",
    "거구나", "우와", "모든", "이건", "저건", "뭔가", "뭔지", "알겠",
    "알았", "그게", "그건", "여기서", "거기서", "이제야", "이젠", "아까",
    "방금", "오늘", "내일", "어제", "처음에", "나중에", "다음에", "이번에",
    "그때", "이때", "저때", "언젠가", "누구", "무엇", "무슨", "어느",
    "하나", "둘", "셋", "넷", "다섯", "여섯", "일곱", "여덟", "아홉",
    "열", "스무", "몇", "얼마", "많이", "조금", "적게", "더", "덜",
    "가장", "제일", "더욱", "더더욱", "한번", "한번씩", "가끔", "자주",
    "항상", "늘", "언제나", "때때로", "대부분", "전부", "모두", "아무",
    "누구나", "무엇이든", "뭐든", "그냥저냥", "이리저리", "이곳저곳",
"가만히", "조용히", "빨리", "천천히", "갑자기", "느닷없이", "이미",
"벌써", "겨우", "간신히", "어쩌다가", "어쩌면", "아마도", "분명히",
"틀림없이", "절대", "결코", "반드시", "꼭", "한번", "다시한번",
"처음부터", "끝까지", "도중에", "한동안", "그동안", "요즘", "최근",
# P1 新增：活用形（动词/形容词修饰形高频词）
"있는", "없는", "하는", "되는", "만든", "죽은", "같은", "다른",
"있어", "없어", "해", "돼", "가", "와", "봐", "봐야",
"봤어", "봤다", "했어", "했다", "갔어", "갔다", "왔어", "왔다",
"있었다", "없었다", "되었다", "했다", "됐다", "갔다",
"습니다", "입니다", "습니다", "비니다", "답니다",
"것입니다", "겁니다", "습니까", "입니까",
"거야", "거네", "군요", "네요", "더라", "더라고",
"같아", "같다", "같은데", "같으면",
"수가", "수를", "수도", "수는",
"때문", "때문에", "때문이다", "덕분", "덕분에",
"대해", "대한", "대하여", "관해", "관하여",
"통해", "통하여", "의해", "의하여",
"않고", "않는", "않아", "않았", "않으면", "않을",
"못하고", "못하는", "못해", "못했다", "못할",
"하고", "하며", "하면", "할", "한",
"되고", "되며", "되면", "될", "된",
"있고", "있으며", "있으면", "있을",
"없고", "없으며", "없으면", "없을",
}

_JA_STOPWORDS = {
    "これ", "それ", "あれ", "ここ", "そこ", "あそこ", "この", "その", "あの",
    "なん", "なに", "なぜ", "どう", "そう", "もう", "まだ", "でも", "だから",
    "そして", "しかし", "けど", "から", "まで", "より", "ほど", "だけ",
    "ばかり", "くらい", "ぐらい", "さん", "ちゃん", "くん", "さま",
    "です", "ます", "ました", "でした", "ですね", "ですよ", "だよ", "だね",
    "ある", "いる", "する", "なる", "できる", "やばい", "すごい", "やば",
    "マジ", "まじ", "めっちゃ", "ちょっと", "すごく", "かなり", "本当",
    "ほんと", "やっぱ", "やはり", "おい", "ねえ", "うわ", "ああ", "ええ",
    "はい", "いいえ", "うん", "んー", "あのさ", "ちょ", "いや", "やめ",
}

_KO_PARTICLE_SUFFIX = ("은", "는", "이", "가", "을", "를", "의", "에", "도",
                       "만", "고", "지", "네", "죠", "요", "과", "와")
_JA_PARTICLE_SUFFIX = ("です", "ます", "だよ", "だね", "かも", "けど", "から",
                       "まで", "より", "って", "の", "は", "が", "を", "に")


def _normalise_for_compare(text: str, source_language: str = "ko") -> str:
    if source_language in {"ja", "ko"}:
        return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", text or ""))
    return " ".join(re.findall(r"[a-z0-9']+", (text or "").lower()))


def _same_semantic_unit(left: str, right: str, source_language: str) -> bool:
    """两源是否指向同一句话。

    韩语语序自由（"에이메스 진짜 내가 갈게" == "진짜 내가 갈게 에이메스"），
    词序无关比较 + 字面一致双重判定；日语/英语只做字面一致。
    """
    if source_language != "ko":
        return (
            _normalise_for_compare(left, source_language)
            == _normalise_for_compare(right, source_language)
            and _is_question(left, source_language)
            == _is_question(right, source_language)
        )
    left_compact = _normalise_for_compare(left, "ko")
    right_compact = _normalise_for_compare(right, "ko")
    if left_compact == right_compact:
        return _is_question(left, "ko") == _is_question(right, "ko")
    if not left_compact or not right_compact:
        return False
    # 词序无关：同一组词的不同排列视为一致。必须在去空格前分词，
    # 否则 compact 文本永远只有一个 Hangul run，这条规则实际不会生效。
    left_words = sorted(re.findall(r"[\uac00-\ud7af]+", left or ""))
    right_words = sorted(re.findall(r"[\uac00-\ud7af]+", right or ""))
    if not left_words or not right_words:
        return False
    if left_words == right_words and len(left_words) >= 2:
        return _semantic_invariants_match(left, right, "ko")
    # 高相似度只能作为字面小差异兜底；语义关键内容必须完整保留。
    if SequenceMatcher(None, left_compact, right_compact).ratio() >= 0.85:
        if not _semantic_similarity_guard(left, right, "ko"):
            return False
        return True
    return False


def _semantic_similarity_guard(left: str, right: str, source_language: str) -> bool:
    """守住高相似兜底中的否定、数字、疑问、实体和信息覆盖率。"""
    left_nfkc = unicodedata.normalize("NFKC", left or "")
    right_nfkc = unicodedata.normalize("NFKC", right or "")
    left_compact = _normalise_for_compare(left_nfkc, source_language)
    right_compact = _normalise_for_compare(right_nfkc, source_language)
    if not left_compact or not right_compact:
        return False

    # 长度/覆盖率：不把漏词或新增信息当成同一句。
    length_ratio = min(len(left_compact), len(right_compact)) / max(
        len(left_compact), len(right_compact)
    )
    matcher = SequenceMatcher(None, left_compact, right_compact)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    char_coverage = matched / min(len(left_compact), len(right_compact))
    if length_ratio < 0.85 or char_coverage < 0.9:
        return False

    # 否定、数字、疑问是不可丢失的命题特征。
    markers = _NEGATION_MARKERS.get(source_language, ()) + _CJK_NEGATION_MARKERS
    if {m for m in markers if m in left_nfkc} != {
        m for m in markers if m in right_nfkc
    }:
        return False
    if _NUMBER_RE.findall(left_nfkc) != _NUMBER_RE.findall(right_nfkc):
        return False
    if _is_question(left_nfkc, source_language) != _is_question(
        right_nfkc, source_language
    ):
        return False

    left_tokens = _semantic_tokens(left_nfkc, source_language)
    right_tokens = _semantic_tokens(right_nfkc, source_language)
    if left_tokens or right_tokens:
        matched_tokens = sum(
            1 for token in left_tokens
            if any(
                token == other
                or (len(token) <= 2 and SequenceMatcher(None, token, other).ratio() >= 0.5)
                for other in right_tokens
            )
        )
        token_coverage = matched_tokens / max(len(left_tokens), len(right_tokens), 1)
        if token_coverage < 0.8:
            return False

    # 长而非语法化的词很可能是实体/术语；已知 correction 会在调用本函数
    # 前归一化，因此这里宁可把未确认的实体拼写差异留给仲裁。
    if _probable_entity_tokens(left_nfkc, source_language) != \
            _probable_entity_tokens(right_nfkc, source_language):
        return False
    return True


def _semantic_invariants_match(
    left: str, right: str, source_language: str,
) -> bool:
    markers = _NEGATION_MARKERS.get(source_language, ()) + _CJK_NEGATION_MARKERS
    return (
        {m for m in markers if m in left} == {m for m in markers if m in right}
        and _NUMBER_RE.findall(left) == _NUMBER_RE.findall(right)
        and _is_question(left, source_language) == _is_question(
            right, source_language,
        )
    )


def _is_question(text: str, source_language: str) -> bool:
    if "?" in text or "？" in text:
        return True
    compact = re.sub(r"[\s.!。！]+$", "", text)
    if source_language == "ko":
        return bool(re.search(r"(?:까|나요|니|냐|습니까)$", compact))
    if source_language == "ja":
        return compact.endswith(("か", "の", "かな", "でしょうか"))
    return False


def _semantic_tokens(text: str, source_language: str) -> List[str]:
    tokens = []
    for raw in _SEMANTIC_TOKEN_RE.findall(text):
        token = raw.lower()
        if source_language == "ko":
            token = _strip_particle_tail(token)
            if token in _KO_STOPWORDS:
                continue
        elif source_language == "ja" and token in _JA_STOPWORDS:
            continue
        if token:
            tokens.append(token)
    return tokens


def _probable_entity_tokens(text: str, source_language: str) -> set[str]:
    probable = set()
    for token in _semantic_tokens(text, source_language):
        if len(token) < 3:
            continue
        if source_language == "ko" and any(
            token.endswith(suffix) for suffix in _KO_VERBAL_SUFFIXES
        ):
            continue
        probable.add(token)
    return probable


# ---------------------------------------------------------------- 知识库


@dataclass
class ArbitrationKnowledge:
    """仲裁需要的知识：glossary / ASR corrections / 翻译记忆 / 人物表。"""

    source_language: str = "ko"
    game: str = "wuwa"
    asr_corrections: Dict[str, str] = field(default_factory=dict)
    known_source_terms: set = field(default_factory=set)      # glossary 源词
    known_target_terms: set = field(default_factory=set)      # glossary 中文
    source_to_target: Dict[str, str] = field(default_factory=dict)  # glossary 映射
    person_terms: set = field(default_factory=set)            # 人物表源词
    memory_sources: set = field(default_factory=set)          # 翻译记忆原文

    @classmethod
    def from_data_dir(cls, data_dir: str = None, game: str = "wuwa",
                      source_language: str = "ko") -> "ArbitrationKnowledge":
        root = data_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        knowledge = cls(source_language=source_language, game=game)

        # asr_corrections.json
        ac_path = os.path.join(root, "asr_corrections.json")
        if os.path.isfile(ac_path):
            try:
                knowledge.asr_corrections = json.loads(
                    open(ac_path, encoding="utf-8-sig").read())
            except (OSError, ValueError):
                knowledge.asr_corrections = {}

        # glossary.db
        try:
            from pipeline.preprocess.glossary import load_glossary_db
            database = load_glossary_db(root)
            items = database.list_all(game=game, source_language=source_language)
            for item in items:
                src = str(item.get("source_term", "")).strip()
                tgt = str(item.get("target_term", "")).strip()
                if src:
                    knowledge.known_source_terms.add(src)
                if tgt:
                    knowledge.known_target_terms.add(tgt)
                if src and tgt:
                    knowledge.source_to_target[src] = tgt
        except Exception:
            pass

        # 人物表（ko_person_terms.json / ja_person_terms.json）
        person_file = os.path.join(root, f"{source_language}_person_terms.json")
        if os.path.isfile(person_file):
            try:
                payload = json.loads(open(person_file, encoding="utf-8-sig").read())
                for item in payload.get("terms", []):
                    src = str(item.get("source", "")).strip()
                    if src:
                        knowledge.person_terms.add(src)
                        knowledge.known_source_terms.add(src)
                    tgt = str(item.get("target", "")).strip()
                    if tgt:
                        knowledge.known_target_terms.add(tgt)
            except (OSError, ValueError):
                pass

        # 翻译记忆（source 原文）——只用于精确匹配，不参与子串过滤
        tm_path = os.path.join(root, "translation_memory.json")
        if os.path.isfile(tm_path):
            try:
                payload = json.loads(open(tm_path, encoding="utf-8-sig").read())
                for entry in payload.get("entries", []):
                    if not entry.get("approved"):
                        continue
                    if entry.get("source_language") != source_language:
                        continue
                    src = str(entry.get("source", "")).strip()
                    if src:
                        knowledge.memory_sources.add(src)
                    final = str(entry.get("final", "")).strip()
                    if final:
                        knowledge.known_target_terms.add(final)
            except (OSError, ValueError):
                pass

        return knowledge

    def normalize_via_corrections(self, text: str) -> str:
        """长词优先替换 ASR 变体（与 asr_normalize 同策略，供打分用）。"""
        if not text or not self.asr_corrections:
            return text
        pattern = re.compile("|".join(
            re.escape(k) for k in sorted(
                self.asr_corrections, key=len, reverse=True)))
        return pattern.sub(lambda m: self.asr_corrections[m.group(0)], text)


# ---------------------------------------------------------------- 专名候选


# 动词/形容词活用尾（非专名形态特征）
_KO_VERBAL_SUFFIXES = (
    # 连接词尾 / 修饰词尾
    "되는", "하는", "이고", "하고", "되고", "니까", "는데", "면서",
    "어서", "아서", "으면", "거든", "잖아", "라서", "거니", "건가",
    "는지", "은지", "을까", "는걸", "은걸", "을게", "는게", "은게",
    # 过去/未来修饰
    "였던", "었던", "았던", "였다", "었다", "았다", "이라서", "니까요",
    # 引用/推测
    "다니", "다면서", "다고", "라고요", "나보다", "나봐", "는구나",
    "었구나", "았구나", "네요", "지요", "죠", "요", "거야", "거죠",
    # 惯用
    "것 같", "것같", "수 있", "수없", "수 없",
    # 否定/接续
    "져서", "았는데", "었는데", "있는데", "있지도", "않은데", "않고",
    "않아서", "다가", "아서", "고요", "구요", "다니", "더라", "더라고",
    # 疑问
    "았나", "었나", "는지", "나요", "았죠", "었죠", "는걸요", "은걸요",
    # 终结
    "는다", "ㄴ다", "는다고", "ㄴ다고", "라고", "이라", "이라면", "이면",
    "었네", "았네", "였네", "었어", "았어", "였어", "었지", "았지", "였지",
    "었나", "었죠", "었는데", "었구나", "었던데", "네요", "네",
    # P1 新增：修饰词尾（관형사형）—— 있는, 죽은, 만든, 사라진, 따라간
    "는", "은", "던", "ㄴ",
    # P1 新增：终结词尾（종결 어미）—— 습니다, 입니다, 비니다
    "습니다", "ㅂ니다", "입니다", "비니다", "습니까", "ㅂ니까",
    # P1 新增：连接词尾补充
    "지만", "며", "도록", "려고", "라고도", "다가는", "다가도",
    "어야", "아야", "으니", "으로", "이야",
    "텐데", "ㄹ게", "ㄹ까", "ㄹ래", "ㄹ까요",
    # P1 新增：副词形接尾
    "히", "게", "처럼", "마냥",
)

_KO_PARTICLE_TAIL = ("은", "는", "이", "가", "을", "를", "의", "에", "도",
                     "만", "과", "와", "로", "으로", "에서", "에게", "한테",
                     "보다", "처럼", "대로", "만큼", "쯤",
                     # P1 新增：复合助词
                     "에게도", "한테도", "에서는", "에서도", "으로는", "으로도",
                     "으로써", "로써", "로는", "로도", "에게는", "한테는",
                     "관한", "관하여", "대한", "대하여", "를 통해", "을 통해",
                     "라는", "라면", "라도", "에서부터", "부터", "까지",
                     "마저", "조차", "뿐", "뿐만", "및")


def _strip_particle_tail(word: str) -> str:
    """去掉尾部助词（게이트를 → 게이트）。"""
    for tail in sorted(_KO_PARTICLE_TAIL, key=len, reverse=True):
        if word.endswith(tail) and len(word) > len(tail):
            return word[: -len(tail)]
    return word


def _extract_entity_candidates(text: str, knowledge: ArbitrationKnowledge) -> List[str]:
    """提取疑似未知专名（不在已知集合、形态像专名）。只标记，不阻断。"""
    if not text:
        return []
    lang = knowledge.source_language
    runs = (_KO_RUN if lang == "ko" else _JA_KATAKANA_RUN).findall(text or "")
    known = knowledge.known_source_terms
    stopwords = _KO_STOPWORDS if lang == "ko" else _JA_STOPWORDS
    corrections = knowledge.asr_corrections
    candidates = []
    seen = set()
    for run in runs:
        if run in seen:
            continue
        seen.add(run)
        if run in stopwords:
            continue
        # 先剥离助词（게이트를 → 게이트, 인류가 → 인류）
        stripped = _strip_particle_tail(run) if lang == "ko" else run
        if stripped in stopwords:
            continue
        # 再检查动词活用尾（在助词剥离后检查，避免 게이드는 被误跳过）
        if lang == "ko" and any(stripped.endswith(s) for s in _KO_VERBAL_SUFFIXES):
            continue
        # 词干太短 → 非专名（있, 죽, 만 等 1 字符词干是动词/形容词词干）
        if lang == "ko" and len(stripped) < 2:
            continue
        if any(stripped in known_term or known_term in stripped
               for known_term in known):
            continue
        if run in corrections or run in corrections.values():
            continue
        if stripped in corrections or stripped in corrections.values():
            continue
        if lang == "ko" and len(run) < 2:
            continue
        candidates.append(run)
    return candidates


# ---------------------------------------------------------------- 打分


def _is_fragment(text: str, knowledge: ArbitrationKnowledge) -> bool:
    """明显截断/滚动残片：很短且以助词结尾。"""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if len(stripped) <= 4:
        return True
    if len(stripped) > 8:
        return False
    suffixes = (_KO_PARTICLE_SUFFIX if knowledge.source_language == "ko"
                else _JA_PARTICLE_SUFFIX)
    return any(stripped.endswith(s) for s in suffixes)


def _score_evidence(text: str, knowledge: ArbitrationKnowledge,
                    context_known_entities: set,
                    repeated_unknowns: set | None = None) -> tuple[float, List[str]]:
    """对一路源文本打分：可靠修正/核心实体为强证据，glossary 为弱证据。

    repeated_unknowns：全视频出现 >= 2 次的未知候选（真专名特征）；
    单次出现的未知词视为普通词/噪音，不参与惩罚，避免压垮普通句。
    """
    if not text or not text.strip():
        return 0.0, ["empty"]
    score = 0.55
    reasons: List[str] = []

    # 1) ASR corrections 命中（归一化为已知形式 → 可信）
    normalised = knowledge.normalize_via_corrections(text)
    if normalised != text:
        score += 0.2
        reasons.append(f"asr_correction")

    # 残片先判（短文本必须重罚，避免残片赢过完整句）
    if _is_fragment(text, knowledge):
        score -= 0.65 if len(text.strip()) <= 4 else 0.4
        reasons.append("fragment")

    # 2) glossary 仅能弱支持拼写/术语形态，不能单独决定 source truth。
    glossary_hits = {term for term in knowledge.known_source_terms if term in text}
    if glossary_hits:
        score += min(0.04 * len(glossary_hits), 0.12)
        reasons.append(f"glossary:{len(glossary_hits)}")

    # 3) 人物表命中
    person_hits = [t for t in knowledge.person_terms if t and t in text]
    if person_hits:
        score += 0.18
        reasons.append(f"person:{len(person_hits)}")

    # 4) 未知专名惩罚（只惩罚全视频重复出现的候选：真专名特征）
    unknowns = _extract_entity_candidates(text, knowledge)
    if repeated_unknowns:
        repeated = [u for u in unknowns if u in repeated_unknowns]
        if repeated:
            penalty = min(0.15 * len(repeated), 0.3)
            score -= penalty
            reasons.append(f"unknown_entity:{len(repeated)}")


    # 6) 邻近语境：未知候选不在视频级已确认实体集 → 轻微降信
    if unknowns and context_known_entities:
        foreign = [u for u in unknowns
                   if not any(u in e or e in u for e in context_known_entities)]
        if foreign:
            score -= 0.1
            reasons.append("context_unknown")

    return max(0.0, score), reasons


def _pairwise_quality_adjustments(
    primary: str,
    secondary: str,
    primary_coverage: float,
    secondary_coverage: float,
    source_language: str,
) -> tuple[float, float, List[str], List[str]]:
    """加入只有同时看到两路证据才能判断的完整度、coverage 与 agreement。"""
    primary_compact = _normalise_for_compare(primary, source_language)
    secondary_compact = _normalise_for_compare(secondary, source_language)
    longest = max(len(primary_compact), len(secondary_compact), 1)
    primary_adjustment = 0.4 * len(primary_compact) / longest
    secondary_adjustment = 0.4 * len(secondary_compact) / longest
    primary_reasons = [
        f"completeness:{len(primary_compact) / longest:.2f}"
    ]
    secondary_reasons = [
        f"completeness:{len(secondary_compact) / longest:.2f}"
    ]

    def _coverage(value: float) -> float:
        try:
            return max(0.0, min(float(value), 1.0))
        except (TypeError, ValueError):
            return 0.0

    primary_adjustment += 0.25 * _coverage(primary_coverage)
    secondary_adjustment += 0.25 * _coverage(secondary_coverage)
    primary_reasons.append(f"coverage:{_coverage(primary_coverage):.2f}")
    secondary_reasons.append(f"coverage:{_coverage(secondary_coverage):.2f}")

    agreement = SequenceMatcher(
        None, primary_compact, secondary_compact,
    ).ratio()
    agreement_bonus = 0.1 * agreement
    primary_adjustment += agreement_bonus
    secondary_adjustment += agreement_bonus
    primary_reasons.append(f"agreement:{agreement:.2f}")
    secondary_reasons.append(f"agreement:{agreement:.2f}")
    return (
        primary_adjustment,
        secondary_adjustment,
        primary_reasons,
        secondary_reasons,
    )


# ---------------------------------------------------------------- 主仲裁


@dataclass
class ArbitratedUnit:
    """仲裁后的语义单元（不破坏现有 SourceCandidate）。"""

    key: str
    start_ms: int
    end_ms: int
    source_language: str
    primary_evidence: str = ""
    secondary_evidence: str = ""
    canonical_source_text: str = ""
    source_decision: str = "unresolved"      # identical|primary|secondary|single_primary|single_secondary|unresolved
    source_confidence: float = 0.0
    source_conflict: bool = False
    reasons: List[str] = field(default_factory=list)
    unknown_entities: List[str] = field(default_factory=list)


def _strip_stage_marks(text: str) -> str:
    """去掉 [음악]/[웃음] 等舞台提示；纯标记行视为无对白。"""
    return re.sub(r"\[[^\]]*\]", "", text or "").strip()


def _strip_evidence_prefix(text: str) -> str:
    """去掉证据前缀标记（如 YouTube union 的 '>> '），canonical 保持干净。"""
    return re.sub(r"^\s*>>\s*", "", text or "").strip()


def arbitrate_candidates(
    candidates: Iterable[SourceCandidate],
    *,
    source_language: str = "ko",
    data_dir: str = None,
    game: str = "wuwa",
) -> Dict[str, ArbitratedUnit]:
    """对每个 semantic unit 仲裁双源，生成 canonical source text。

    en 源语言返回空（不进入仲裁，保持旧链路）。
    """
    pair = normalize_language_pair({"source_language": source_language})
    lang = pair["source_language"]
    if lang == "en":
        return {}

    knowledge = ArbitrationKnowledge.from_data_dir(data_dir, game=game, source_language=lang)
    candidates = list(candidates)

    # 视频级已确认实体：所有证据中出现的已知术语
    context_known = set()
    # 全视频未知词频统计：>= 2 次 → 真专名特征（参与选路惩罚）
    unknown_freq = Counter()
    for cand in candidates:
        for text in (cand.primary_evidence, cand.secondary_evidence):
            if not text:
                continue
            for entity in _extract_entity_candidates(text, knowledge):
                unknown_freq[entity] += 1
            try:
                from pipeline.preprocess.glossary import load_glossary_db
                database = load_glossary_db(data_dir)
                context_known.update(database.extract_from_text(
                    text, game=game, source_language=lang).keys())
            except Exception:
                pass
    repeated_unknowns = {e for e, count in unknown_freq.items() if count >= 2}

    results: Dict[str, ArbitratedUnit] = {}
    for cand in candidates:
        primary = (cand.primary_evidence or "").strip()
        secondary = (cand.secondary_evidence or "").strip()
        # 证据前缀（>>）只影响仲裁文本，不进入 canonical；evidence 保留原始
        primary_clean = _strip_evidence_prefix(primary)
        secondary_clean = _strip_evidence_prefix(secondary)
        # 纯舞台提示行视为无对白
        if primary_clean and not _strip_stage_marks(primary_clean):
            primary_clean = ""
        if secondary_clean and not _strip_stage_marks(secondary_clean):
            secondary_clean = ""
        primary, secondary = primary_clean, secondary_clean
        unit = ArbitratedUnit(
            key=cand.key,
            start_ms=timestamp_to_ms(cand.start),
            end_ms=timestamp_to_ms(cand.end),
            source_language=lang,
            primary_evidence=primary,
            secondary_evidence=secondary,
        )

        if not primary and not secondary:
            unit.source_decision = "unresolved"
            unit.source_confidence = 0.0
            unit.source_conflict = True
            unit.reasons = ["no_evidence"]
            results[cand.key] = _finalize_unit(unit, knowledge)
            continue

        # 先应用 ASR corrections（长词优先）——canonical 永远输出修复后的标准形式
        primary_norm = knowledge.normalize_via_corrections(primary)
        secondary_norm = knowledge.normalize_via_corrections(secondary)

        if not secondary:
            unit.canonical_source_text = primary_norm
            unit.source_decision = "single_primary"
            unit.source_confidence = 0.7
            unit.reasons = ["single_source"]
            results[cand.key] = _finalize_unit(unit, knowledge)
            continue

        if not primary:
            unit.canonical_source_text = secondary_norm
            unit.source_decision = "single_secondary"
            unit.source_confidence = 0.7
            unit.reasons = ["single_source"]
            results[cand.key] = _finalize_unit(unit, knowledge)
            continue

        # 两源一致（应用 ASR 修复后归一化比较，韩语词序无关）→ 高置信，不仲裁
        if _same_semantic_unit(primary_norm, secondary_norm, lang):
            unit.canonical_source_text = primary_norm
            unit.source_decision = "identical"
            unit.source_confidence = 0.95
            unit.reasons = ["identical"]
            results[cand.key] = _finalize_unit(unit, knowledge)
            continue

        # 打分仲裁
        primary_score, primary_reasons = _score_evidence(
            primary, knowledge, context_known, repeated_unknowns)
        secondary_score, secondary_reasons = _score_evidence(
            secondary, knowledge, context_known, repeated_unknowns)

        (
            primary_adjustment,
            secondary_adjustment,
            primary_quality_reasons,
            secondary_quality_reasons,
        ) = _pairwise_quality_adjustments(
            primary_norm,
            secondary_norm,
            getattr(cand, "primary_coverage", 0.0),
            getattr(cand, "secondary_coverage", 0.0),
            lang,
        )
        primary_score += primary_adjustment
        secondary_score += secondary_adjustment
        primary_reasons += primary_quality_reasons
        secondary_reasons += secondary_quality_reasons

        diff = abs(primary_score - secondary_score)
        pair_similarity = SequenceMatcher(
            None,
            _normalise_for_compare(primary_norm, lang),
            _normalise_for_compare(secondary_norm, lang),
        ).ratio()
        strong_primary = any(
            reason.startswith(("asr_correction", "person:"))
            for reason in primary_reasons
        )
        strong_secondary = any(
            reason.startswith(("asr_correction", "person:"))
            for reason in secondary_reasons
        )
        coverage_gap = abs(
            float(getattr(cand, "primary_coverage", 0.0) or 0.0)
            - float(getattr(cand, "secondary_coverage", 0.0) or 0.0)
        )
        # 两路语义几乎无交集时，单纯“更长”不是正确性证据。
        if (
            pair_similarity < 0.5
            and not strong_primary
            and not strong_secondary
            and coverage_gap < 0.3
        ):
            if primary_score >= secondary_score:
                unit.canonical_source_text = primary_norm
                unit.reasons = primary_reasons
            else:
                unit.canonical_source_text = secondary_norm
                unit.reasons = secondary_reasons
            unit.source_decision = "unresolved"
            unit.source_confidence = 0.35
            unit.source_conflict = True
            unit.reasons.append("severe_disagreement")
            results[cand.key] = _finalize_unit(unit, knowledge)
            continue
        # 残片不优先：高分路是残片而另一路明显更完整时，选完整路并标记冲突
        primary_frag = _is_fragment(primary_norm, knowledge)
        secondary_frag = _is_fragment(secondary_norm, knowledge)
        if primary_frag and not secondary_frag and len(secondary_norm) >= 3 * len(primary_norm):
            unit.canonical_source_text = secondary_norm
            unit.source_decision = "secondary"
            unit.source_confidence = round(max(0.45, secondary_score), 3)
            unit.source_conflict = True
            unit.reasons = secondary_reasons + ["fragment_overridden"]
            results[cand.key] = _finalize_unit(unit, knowledge)
            continue
        if secondary_frag and not primary_frag and len(primary_norm) >= 3 * len(secondary_norm):
            unit.canonical_source_text = primary_norm
            unit.source_decision = "primary"
            unit.source_confidence = round(max(0.45, primary_score), 3)
            unit.source_conflict = True
            unit.reasons = primary_reasons + ["fragment_overridden"]
            results[cand.key] = _finalize_unit(unit, knowledge)
            continue

        # 明显 completeness/coverage 优势是强证据；glossary 弱加分不得逆转它。
        primary_length = len(_normalise_for_compare(primary_norm, lang))
        secondary_length = len(_normalise_for_compare(secondary_norm, lang))
        primary_coverage = float(getattr(cand, "primary_coverage", 0.0) or 0.0)
        secondary_coverage = float(getattr(cand, "secondary_coverage", 0.0) or 0.0)
        if (
            secondary_length >= max(primary_length * 1.8, primary_length + 4)
            and secondary_coverage >= primary_coverage + 0.3
            and not secondary_frag
        ):
            unit.canonical_source_text = secondary_norm
            unit.source_decision = "secondary"
            unit.source_confidence = round(max(0.55, secondary_score), 3)
            unit.source_conflict = True
            unit.reasons = secondary_reasons + ["completeness_overridden"]
            results[cand.key] = _finalize_unit(unit, knowledge)
            continue
        if (
            primary_length >= max(secondary_length * 1.8, secondary_length + 4)
            and primary_coverage >= secondary_coverage + 0.3
            and not primary_frag
        ):
            unit.canonical_source_text = primary_norm
            unit.source_decision = "primary"
            unit.source_confidence = round(max(0.55, primary_score), 3)
            unit.source_conflict = True
            unit.reasons = primary_reasons + ["completeness_overridden"]
            results[cand.key] = _finalize_unit(unit, knowledge)
            continue

        if diff >= 0.2:
            if primary_score >= secondary_score:
                unit.canonical_source_text = primary_norm
                unit.source_decision = "primary"
                unit.reasons = primary_reasons
            else:
                unit.canonical_source_text = secondary_norm
                unit.source_decision = "secondary"
                unit.reasons = secondary_reasons
            unit.source_confidence = round(max(primary_score, secondary_score), 3)
            unit.source_conflict = False
        else:
            # 分差小：仍有冲突 → 标记 conflict，canonical 取高分路供翻译，
            # 但低置信进风险队列；两路都低 → unresolved。
            if primary_score >= secondary_score:
                unit.canonical_source_text = primary_norm
                unit.reasons = primary_reasons
            else:
                unit.canonical_source_text = secondary_norm
                unit.reasons = secondary_reasons
            unit.source_confidence = round(max(primary_score, secondary_score), 3)
            unit.source_conflict = True
            # 两路差异极大（归一化后相似度 < 0.5）→ 无法仲裁，必须人工
            if (unit.source_confidence < 0.45
                    or SequenceMatcher(
                        None, primary_norm, secondary_norm,
                    ).ratio() < 0.5):
                unit.source_decision = "unresolved"
            else:
                unit.source_decision = "primary" if primary_score >= secondary_score else "secondary"
            unit.reasons.append("conflict_low_margin")

        results[cand.key] = _finalize_unit(unit, knowledge)

    return results


def _finalize_unit(unit: ArbitratedUnit,
                   knowledge: ArbitrationKnowledge) -> ArbitratedUnit:
    """统一出口：从 canonical 提取未知专名候选。"""
    unit.unknown_entities = _extract_entity_candidates(
        unit.canonical_source_text, knowledge,
    )
    return unit


# ---------------------------------------------------------------- Entity Guard


def classify_entity(entity: str, source_language: str,
                    knowledge: ArbitrationKnowledge) -> str:
    """实体分类：known / asr_variant / unknown。"""
    entity = (entity or "").strip()
    if not entity:
        return "unknown"
    if (entity in knowledge.known_source_terms
            or entity in knowledge.person_terms
            or entity in knowledge.memory_sources):
        return "known"
    normalised = knowledge.normalize_via_corrections(entity)
    if normalised != entity and (
        normalised in knowledge.known_source_terms
        or normalised in knowledge.person_terms
        or normalised in knowledge.memory_sources
    ):
        return "asr_variant"
    return "unknown"


# 中文虚词/常见词（幻觉检测过滤用）
_ZH_COMMON = {
    "的了在是我有就都也不很把被让给对从向和与或但而因为所以如果虽然但是",
    "这个", "那个", "什么", "怎么", "为什么", "他们", "我们", "你们", "自己",
    "已经", "正在", "可以", "应该", "可能", "觉得", "知道", "看到", "听到",
    "真的", "非常", "特别", "完全", "终于", "突然", "然后", "但是", "不过",
    "还有", "就是", "不是", "没有", "不要", "不行", "不会", "不能", "应该",
    "现在", "刚才", "马上", "以后", "这里", "那里", "这样", "那样", "这么",
    "那么", "一下", "一点", "一个", "两个", "一起", "一直", "一下", "一定",
    "卧槽", "我靠", "靠", "草", "特么", "牛批", "牛逼", "厉害", "太强", "好帅",
    "离谱", "夸张", "疯狂", "疯了", "搞笑", "无语", "绝了", "可以", "完了",
    "来了", "冲了", "秒了", "赢了", "输了", "死了", "活了", "走了", "来了",
    "确实", "其实", "反正", "总之", "比如", "例如", "部分", "所有", "很多",
    "很快", "很久", "很好", "不好", "不错", "一般", "普通", "简单", "复杂",
}

# 高频功能字（2026-08-11 Phase 7）：专名（人名/地名）几乎不含这些字。
# 用于幻觉实体检测的组合信号——普通中文短语必然含功能字，
# 而音译名（梅卡乌特/蕾姆塔洛斯）不含，可据此区分。
_ZH_FUNCTION_CHARS = set(
    "的了在是我有就都也不很把被让给对从向和与或但而因为所以"
    "如果虽然但是这那什怎么么们自己已经正在可以应该可能觉得"
    "知道看到听到真的非常特别完全终于突然然后不过还有就是不是"
    "没有不要不行不会不能现在刚才马上以后这里那里这样那样那么"
    "一下一点一个两个一起一直一定确实其实反正总之比如例如部分"
    "所有很多很快很久很好不好不错一般普通简单复杂"
)

# 中文音译常用字（2026-08-11 Phase 7）：外来专名音译几乎只由这些字构成。
# unknown 场景下，译文新出现的专名 = 音译形态（梅卡乌特 4/4 是音译字）；
# 普通中文短语必然夹杂功能字（"也就是说" 0/4 音译字）。
_ZH_TRANSLITERATION_CHARS = set(
    "阿埃艾安奥巴拜班贝比波布卡凯坎科克拉莱兰莉丽里利洛卢露鲁"
    "玛梅米莫穆娜奈妮宁诺帕佩普奇琪乔青萨赛森莎斯塔泰特提瓦"
    "维温乌西希夏香肖谢亚雅伊尤扎泽兹尔恩姆蕾姆塔洛蒂娅茜薇"
    "迪娅娜尼卡尔特斯菲萝蜜黛芭婕芭妮莲娜伊芙凯尔"
)

# 音译形态判定：run 中 ≥50% 字符是音译字 → 可能是外来专名音译
def _is_zh_transliteration(run: str) -> bool:
    if not run:
        return False
    hits = sum(1 for ch in run if ch in _ZH_TRANSLITERATION_CHARS)
    return hits / len(run) >= 0.5


def detect_hallucinated_entities(
    chinese_text: str,
    unit: ArbitratedUnit,
    knowledge: ArbitrationKnowledge,
) -> List[str]:
    """翻译后二次检查：canonical 有未知专名时，中文输出出现不在已知
    中文实体集的新 2~4 字词 → 疑似幻觉实体（risk 列表）。

    2026-08-11 Phase 7 泛化（JA-2 残留根因）：纯静态词表过滤会把
    "也就是说/这个锚点/涌过来了" 等普通中文短语误报为幻觉实体。
    新逻辑：unknown 场景下只把"音译形态"的 run 当候选——
      - 已知词子串命中（拉海洛 在 "连拉海洛" 内）→ 跳过
      - 非音译形态（功能字占比高）→ 跳过（普通短语）
      - 音译形态且非已知 → 标记（人工确认，如 梅卡乌特=메카우터）
    """
    if not chinese_text:
        return []
    if unit.source_language == "en":
        return _detect_english_hallucinated_entities(chinese_text, unit, knowledge)
    if not unit.unknown_entities:
        return []
    known_zh = knowledge.known_target_terms
    # 提取连续 2~4 汉字串
    runs = re.findall(r"[\u4e00-\u9fff]{2,4}", chinese_text)
    risks = []
    seen = set()
    for run in runs:
        if run in seen:
            continue
        seen.add(run)
        # 已知词子串命中（如 "连拉海洛" 内含 "拉海洛"）→ 跳过
        if any(k and (k in run or run in k) for k in known_zh):
            continue
        if any(common and (common in run or run in common) for common in _ZH_COMMON):
            continue
        if run in unit.unknown_entities:
            continue
        # 非音译形态 → 普通短语，不是专名 → 跳过
        if not _is_zh_transliteration(run):
            continue
        risks.append(run)
    return risks


_EN_ZH_TRANSLITERATION = re.compile(
    r"[阿埃艾安奥巴拜班贝比波布卡凯坎科克拉莱兰莉丽里利洛卢露鲁"
    r"玛梅米莫穆娜奈妮宁诺帕佩普奇琪乔青萨赛森莎斯塔泰特提瓦"
    r"维温乌西希夏香肖谢亚雅伊尤扎泽兹尔恩姆]{3,6}"
)
_EN_ZH_NAMED_CONTEXT = re.compile(
    r"(?:名为|叫作|叫|前往|来自|加入|使用|装备)"
    r"([一-鿿]{2,6}?)(?=的|了|中|里|，|。|！|？|$)"
)
_EN_ZH_QUOTED = re.compile(r"[《「『“](?P<name>[一-鿿]{2,8})[》」』”]")


def _detect_english_hallucinated_entities(
    chinese_text: str,
    unit: ArbitratedUnit,
    knowledge: ArbitrationKnowledge,
) -> List[str]:
    """Conservative English-only check for newly invented Chinese names."""
    source = unit.canonical_source_text or unit.primary_evidence
    authorised = set(knowledge.known_target_terms)
    for source_term, target_term in knowledge.source_to_target.items():
        if re.search(
            rf"(?<![A-Za-z0-9]){re.escape(source_term)}(?![A-Za-z0-9])",
            source,
            re.IGNORECASE,
        ):
            authorised.add(target_term)

    discoveries = []
    discoveries.extend(match.group(0) for match in _EN_ZH_TRANSLITERATION.finditer(chinese_text))
    discoveries.extend(match.group(1) for match in _EN_ZH_NAMED_CONTEXT.finditer(chinese_text))
    discoveries.extend(match.group("name") for match in _EN_ZH_QUOTED.finditer(chinese_text))
    risks = []
    seen = set()
    for candidate in discoveries:
        if candidate in seen:
            continue
        seen.add(candidate)
        if any(
            candidate == term
            or (len(term) >= 2 and (candidate in term or term in candidate))
            for term in authorised if term
        ):
            continue
        if any(candidate in common for common in _ZH_COMMON):
            continue
        risks.append(candidate)
    return risks
