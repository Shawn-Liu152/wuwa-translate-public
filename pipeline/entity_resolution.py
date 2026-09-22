"""Conservative task-local proper-name candidate discovery."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


_EN_TITLE_RUN = re.compile(r"\b[A-Z][a-z]{2,}(?:[ -][A-Z][a-z]{2,})*\b")
_EN_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EN_CONTEXTUAL_ENTITY_CUE = re.compile(
    r"(?:sponsored\s+by|sponsor(?:ed)?|brought\s+to\s+you\s+by|"
    r"thanks\s+to|check\s+out|visit|download|try|use(?:\s+code)?)"
    r"[\s:,-]{0,12}$",
    re.IGNORECASE,
)
_EN_STOPWORDS = {
    "A", "Actually", "An", "And", "Because", "Boss", "But", "Check", "Dps",
    "Boy", "Brother", "Dad", "Dude", "Everyone", "Father", "Finally",
    "Friend", "Get", "Girl", "Guy", "He", "Hello", "Here", "How", "Hp",
    "I", "It", "Look", "Make", "Man", "Maybe", "My", "No", "Oh", "Okay",
    "Kid", "Mom", "Mother", "Our", "Please", "See", "She", "Sister", "Someone",
    "Take", "That", "The", "Their", "Then", "There", "Think",
    "These", "They", "This", "Those", "Today", "Tomorrow", "Watch", "We", "What",
    "When", "Where", "Who", "Why", "Woman", "Wow", "Yes", "You", "Your",
    # 2026-08-10 EN 审计（lWgwc_xNzrg）：句首大写普通词误报校准。
    # 感叹词/虚词/常见名在任何位置都不是实体证据，直接进 stopword。
    "Bro", "God", "Hey", "Jesus", "Jim", "Like", "Lily", "Rome",
}

# 单个 Title Case token 的普通词判定（casefolded）：普通英文词 + 常见
# 人名。字幕断句与标点经常产生非句首大写；真实体（Rulah 等 glossary 外、
# 大写且非词典词的 ASR 变体/人名/地名）不在此列，必须保留。
_EN_COMMON_SINGLE_WORDS = frozenset({
    # Conversational starters / interjections / discourse markers
    "absolutely", "alright", "anyway", "anyways", "apparently", "basically",
    "besides", "bye", "certainly", "clearly", "correct", "definitely",
    "exactly", "fine", "fortunately", "frankly", "gee", "geez", "gosh",
    "great", "hi", "honestly", "hopefully", "however", "indeed", "listen",
    "luckily", "meanwhile", "moreover", "nevertheless", "nice", "nonetheless",
    "nope", "obviously", "otherwise", "perhaps", "precisely", "probably",
    "really", "regardless", "right", "sadly", "seriously", "sorry", "sure",
    "thanks", "thank", "therefore", "thus", "unfortunately", "wait",
    "welcome", "well", "yeah", "yep", "whoa", "oops", "ouch", "yikes",
    "aha", "huh", "hmm", "phew", "dang", "damn", "heck", "crap", "shoot",
    "ugh",
    # Common verbs (imperative / declarative sentence starts)
    "agree", "answer", "ask", "attack", "begin", "believe", "break", "bring",
    "build", "buy", "call", "catch", "change", "choose", "come", "cook",
    "count", "create", "cry", "dance", "decide", "defend", "die", "do",
    "draw", "dream", "drink", "drive", "drop", "eat", "end", "enjoy",
    "escape", "explain", "feel", "feels", "fight", "find", "finish", "fix",
    "fly", "follow", "forget", "forgive", "give", "go", "grab", "guess",
    "happen", "happens", "hate", "hear", "help", "hide", "hold", "hope",
    "hurt", "join", "jump", "keep", "kill", "know", "laugh", "learn",
    "leave", "let", "live", "looks", "lose", "love", "meet", "move", "need",
    "notice", "open", "pay", "pick", "play", "point", "pray", "prepare",
    "protect", "pull", "push", "put", "question", "quit", "reach", "read",
    "realize", "relax", "remember", "return", "run", "save", "say", "search",
    "sell", "send", "seems", "share", "shout", "show", "sing", "sit",
    "sleep", "smile", "sounds", "speak", "spend", "stand", "start", "stay",
    "stop", "study", "suppose", "swim", "talk", "teach", "tell", "throw",
    "touch", "travel", "try", "turns", "understand", "use", "visit", "wake",
    "walk", "want", "warn", "wear", "wish", "win", "wonder", "work", "worry",
    "write",
    # Common nouns / pronouns / determiners (sentence-initial)
    "anybody", "anyone", "anything", "back", "bottom", "chance", "child",
    "children", "city", "day", "death", "door", "earth", "everybody",
    "everything", "family", "fire", "floor", "front", "game", "ground",
    "group", "half", "hand", "head", "heart", "home", "hour", "house",
    "idea", "joy", "kind", "king", "land", "life", "light", "line", "lot",
    "money", "month", "morning", "mountain", "music", "name", "night",
    "nobody", "nothing", "number", "ocean", "one", "part", "peace", "people",
    "person", "place", "plan", "point", "power", "problem", "question",
    "rain", "reason", "rest", "road", "room", "sea", "side", "sky",
    "something", "son", "song", "soul", "sound", "space", "star", "stars",
    "story", "street", "sun", "sword", "table", "team", "thing", "things",
    "time", "top", "town", "tree", "trouble", "truth", "turn", "voice",
    "war", "water", "way", "week", "wind", "window", "winter", "word",
    "words", "world", "year", "yesterday",
    # Common adjectives / adverbs
    "afraid", "again", "ago", "almost", "alone", "along", "already",
    "always", "amazing", "angry", "another", "any", "around", "away",
    "awesome", "bad", "beautiful", "better", "big", "black", "blue",
    "brave", "bright", "busy", "calm", "careful", "certain", "cheap",
    "clean", "clear", "close", "cold", "cool", "crazy", "cute", "dark",
    "dead", "different", "difficult", "dirty", "down", "dry", "dumb",
    "early", "easy", "empty", "enough", "every", "excited", "expensive",
    "fair", "false", "fast", "few", "final", "first", "foreign", "free",
    "full", "fun", "funny", "future", "gentle", "glad", "golden", "good",
    "green", "happy", "hard", "healthy", "heavy", "high", "holy", "hot", "huge",
    "hungry", "important", "impossible", "interesting", "large", "last",
    "late", "lazy", "loud", "lovely", "low", "lucky", "main", "many",
    "merry", "modern", "more", "most", "much", "narrow", "near",
    "necessary", "new", "normal", "old", "only", "other", "own", "perfect",
    "plain", "possible", "pretty", "proud", "quick", "quiet", "rare",
    "ready", "real", "red", "rich", "round", "safe", "same", "scared",
    "second", "serious", "sharp", "short", "shy", "sick", "silent",
    "simple", "slow", "small", "smart", "soft", "special", "strange",
    "strong", "stupid", "sweet", "tall", "terrible", "thick", "thin",
    "third", "tired", "together", "tough", "true", "ugly", "united",
    "unknown", "unusual", "up", "useful", "usual", "warm", "weak", "weird",
    "wet", "white", "whole", "wide", "wild", "wise", "wonderful", "wrong",
    "yellow", "young",
    # Number words
    "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "hundred", "thousand", "million", "billion",
    # Days / months
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "june", "july",
    "august", "september", "october", "november", "december",
    # Common given names (sentence-initial vocatives; never game entities)
    "jim", "john", "tom", "bob", "jack", "joe", "bill", "dan", "sam",
    "max", "ben", "ken", "tim", "ron", "don", "phil", "rick", "nick",
    "mark", "mike", "dave", "steve", "frank", "george", "henry", "harry",
    "peter", "paul", "mary", "jane", "ann", "amy", "sue", "beth", "kate",
    "lucy", "lily", "emma", "anna", "sarah", "mia", "zoe", "ella", "nora",
    "ivy", "ruby", "rose", "grace", "claire", "alice", "olivia", "sophia",
    "emily", "hannah", "molly", "holly", "sally", "nancy", "linda", "karen",
    "laura", "julie", "amanda", "jessica", "ashley", "heather", "melissa",
    "rebecca", "stephanie", "nicole", "jennifer", "michelle", "tiffany",
    "vanessa", "danielle", "erica", "lauren", "megan", "rachel", "samantha",
    "taylor", "victoria", "allison", "brooke", "chelsea", "jasmine",
    "morgan", "natasha", "shannon", "veronica",
})
_EN_COMMON_WORDS = frozenset(
    word.casefold() for word in _EN_STOPWORDS
) | _EN_COMMON_SINGLE_WORDS


def _normalise(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", value))


def resolve_entity_candidates(
    source_text: str,
    official_terms: dict[str, str],
    *,
    source_language: str,
    minimum_similarity: float = 0.78,
) -> list[dict]:
    """Return review-only near-name mappings; never mutate source text."""
    if source_language == "en":
        return _resolve_english_candidates(source_text, official_terms)
    if source_language not in {"ja", "ko"}:
        return []
    compact = _normalise(source_text)
    candidates = []
    seen = set()
    for official_source, official_target in official_terms.items():
        term = _normalise(str(official_source))
        if len(term) < 3 or not official_target:
            continue
        if term in compact:
            continue
        minimum = max(2, len(term) - 1)
        maximum = min(len(compact), len(term) + 1)
        best_surface = ""
        best_score = 0.0
        for width in range(minimum, maximum + 1):
            for start in range(0, len(compact) - width + 1):
                surface = compact[start:start + width]
                score = SequenceMatcher(None, surface, term).ratio()
                if score > best_score:
                    best_surface, best_score = surface, score
        if best_score < minimum_similarity:
            continue
        key = (best_surface, term, str(official_target))
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "surface": best_surface,
            "official_source": str(official_source),
            "official_target": str(official_target),
            "confidence": round(best_score, 3),
            "evidence": "task_source_near_match",
            "requires_review": True,
            "auto_replace": False,
        })
    return sorted(candidates, key=lambda item: (-item["confidence"], item["surface"]))


def _resolve_english_candidates(
    source_text: str, official_terms: dict[str, str],
) -> list[dict]:
    """Discover glossary-backed and conservative unknown English entities."""
    text = unicodedata.normalize("NFKC", source_text or "")
    searchable = _EN_URL.sub(lambda match: " " * len(match.group(0)), text)
    candidates: list[dict] = []
    occupied: list[tuple[int, int]] = []

    # Sponsor/product copy is a strong task-local signal that a repeated
    # Title Case phrase is a brand, even when one token also exists as a
    # generic glossary term (for example Buff -> 增益 versus Buff Buff).
    # Protect the full phrase before exact glossary matching.  It remains
    # unverified and review-only; no global glossary entry is created.
    seen_contextual = set()
    for match in _EN_TITLE_RUN.finditer(searchable):
        surface = match.group(0)
        words = surface.replace("-", " ").split()
        repeated_phrase = (
            len(words) >= 2
            and len({word.casefold() for word in words}) == 1
        )
        context_before = searchable[max(0, match.start() - 48):match.start()]
        if not repeated_phrase or not _EN_CONTEXTUAL_ENTITY_CUE.search(context_before):
            continue
        occupied.append(match.span())
        key = surface.casefold()
        if key in seen_contextual:
            continue
        seen_contextual.add(key)
        candidates.append({
            "surface": surface,
            "official_source": "",
            "official_target": "",
            "canonical_entity": surface,
            "canonical_target": "",
            "authority": "source_context",
            "classification": "contextual_entity",
            "risk": "contextual_entity_unverified",
            "confidence": 0.8,
            "evidence": "sponsor_context_repeated_title_phrase",
            "requires_review": True,
            "review_required": True,
            "auto_replace": False,
        })

    # Glossary exact matches are evidence, not risks. Prefer longer terms so a
    # multi-word official name is not split into smaller discoveries.
    for official_source, official_target in sorted(
        official_terms.items(), key=lambda item: len(str(item[0])), reverse=True,
    ):
        source = str(official_source).strip()
        target = str(official_target).strip()
        if len(source) < 3 or not target:
            continue
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(source)}(?![A-Za-z0-9])", re.IGNORECASE)
        for match in pattern.finditer(searchable):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            occupied.append(match.span())
            candidates.append({
                "surface": match.group(0),
                "official_source": source,
                "official_target": target,
                "canonical_entity": source,
                "canonical_target": target,
                "authority": "glossary",
                "classification": "known_entity",
                "confidence": 1.0,
                "evidence": "glossary_exact",
                "requires_review": False,
                "review_required": False,
                "auto_replace": False,
            })

    seen_unknown = set()
    for match in _EN_TITLE_RUN.finditer(searchable):
        if any(match.start() < end and match.end() > start for start, end in occupied):
            continue
        surface = match.group(0)
        words = surface.replace("-", " ").split()
        if any(word.upper() in {"DPS", "HP", "BOSS"} for word in words):
            continue
        if any(word in _EN_STOPWORDS for word in words):
            continue
        # A single ordinary capitalized word is not entity evidence by itself.
        # ASR punctuation often capitalizes mid-sentence words too, so apply
        # the common-word filter at every position. Keep novel tokens — they
        # may be real entities or ASR variants (e.g. Rulah → Rupa 露帕).
        letters = re.sub(r"[^A-Za-z]", "", surface).lower()
        if len(words) == 1 and letters in _EN_COMMON_WORDS:
            continue
        key = surface.casefold()
        if key in seen_unknown:
            continue
        seen_unknown.add(key)
        candidates.append({
            "surface": surface,
            "official_source": "",
            "official_target": "",
            "canonical_entity": surface,
            "canonical_target": "",
            "authority": None,
            "classification": "possible_entity",
            "risk": "unknown_entity",
            "confidence": 0.5,
            "evidence": "english_title_case",
            "requires_review": True,
            "review_required": True,
            "auto_replace": False,
        })
    return sorted(
        candidates,
        key=lambda item: searchable.casefold().find(item["surface"].casefold()),
    )
