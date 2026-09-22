"""
Glossary Extractor

从字幕文本中识别游戏专有术语，查询 SQLite 术语库获取中文翻译。

职责：
- 连接并查询 data/glossary.db（SQLite）
- 从字幕 batch 中提取所有出现的术语
- 返回 {english_term: chinese_translation} 映射表
- 支持多游戏术语共存（通过 game 字段区分）
"""
import os
import json
import re
import shutil
import sqlite3
import unicodedata
from contextlib import contextmanager
from difflib import SequenceMatcher
from typing import Dict, Iterator, List, Optional, Set, Tuple

from pipeline.languages import normalize_language_pair


_FUZZY_NAME_CATEGORIES = {
    "character", "story_character", "character_alias",
}
_FUZZY_NAME_STOPWORDS = {
    "about", "after", "again", "almost", "because", "before", "being",
    "better", "coming", "could", "every", "first", "going", "great",
    "brand", "cover", "drifter", "flora", "hello", "marina", "maybe",
    "mortify", "morty", "never", "other", "pebble", "people", "really",
    "right", "sweetsweet",
    "should", "still", "their", "there", "these", "thing", "think",
    "those", "through", "today", "wanderer", "where", "which", "would",
}


def _normalise_name(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _edit_distance(left: str, right: str) -> int:
    """Damerau-Levenshtein distance, including adjacent ASR transpositions."""
    if left == right:
        return 0
    if not left:
        return len(right)
    matrix = [
        [0] * (len(right) + 1)
        for _ in range(len(left) + 1)
    ]
    for index in range(len(left) + 1):
        matrix[index][0] = index
    for index in range(len(right) + 1):
        matrix[0][index] = index
    for left_index in range(1, len(left) + 1):
        for right_index in range(1, len(right) + 1):
            substitution = (
                left[left_index - 1] != right[right_index - 1]
            )
            matrix[left_index][right_index] = min(
                matrix[left_index - 1][right_index] + 1,
                matrix[left_index][right_index - 1] + 1,
                matrix[left_index - 1][right_index - 1] + substitution,
            )
            if (
                left_index > 1
                and right_index > 1
                and left[left_index - 1] == right[right_index - 2]
                and left[left_index - 2] == right[right_index - 1]
            ):
                matrix[left_index][right_index] = min(
                    matrix[left_index][right_index],
                    matrix[left_index - 2][right_index - 2] + 1,
                )
    return matrix[-1][-1]


class GlossaryDB:
    """
    SQLite 术语数据库查询器。

    使用方式：
        db = GlossaryDB("data/glossary.db")
        result = db.extract(["Changli", "Echo", "Scar"], game="wuwa")
        # → {"Changli": "长离", "Echo": "声骸", "Scar": "伤痕"}
    """

    def __init__(self, db_path: str):
        """
        Args:
            db_path: glossary.db 文件路径
        """
        self._db_path = db_path
        # A pre-language database is user data.  Keep a one-time, adjacent
        # backup before adding columns so migration is recoverable even if a
        # later application version cannot read the new schema.
        if os.path.exists(db_path):
            with sqlite3.connect(db_path) as probe:
                existing_columns = {
                    row[1] for row in probe.execute(
                        "PRAGMA table_info(glossary)"
                    ).fetchall()
                }
                schema_row = probe.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='glossary'"
                ).fetchone()
                schema_sql = str(schema_row[0] if schema_row else "").upper()
            language_columns = {
                "source_language", "source_term", "target_language",
                "target_term",
            }
            backup_path = db_path + ".pre-language-migration.bak"
            legacy_primary_key = "PRIMARY KEY (ENGLISH, GAME)" in schema_sql
            if existing_columns and (
                not language_columns.issubset(existing_columns)
                or legacy_primary_key
            ):
                if not os.path.exists(backup_path):
                    shutil.copy2(db_path, backup_path)
        # 打开时确保表存在
        with self._connect() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS glossary (
                    english TEXT NOT NULL,
                    chinese TEXT NOT NULL,
                    game TEXT NOT NULL DEFAULT 'wuwa',
                    category TEXT DEFAULT '',
                    source_language TEXT NOT NULL DEFAULT 'en',
                    source_term TEXT NOT NULL DEFAULT '',
                    target_language TEXT NOT NULL DEFAULT 'zh-CN',
                    target_term TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (source_language, source_term, target_language, game)
                )
            ''')
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_game ON glossary(game)'
            )
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_category ON glossary(category)'
            )
            columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(glossary)"
                ).fetchall()
            }
            for name in (
                "source_language", "source_term", "target_language",
                "target_term",
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE glossary ADD COLUMN {name} TEXT")
            # 幂等回填：仅当存在 NULL 时才执行 UPDATE。
            # 无条件 UPDATE+commit 会在每次打开时重写数据库页面，
            # 即使逻辑内容不变也会改变文件字节哈希（2026-09-03 实证）。
            needs_backfill = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM glossary WHERE "
                "source_language IS NULL OR source_term IS NULL "
                "OR target_language IS NULL OR target_term IS NULL)"
            ).fetchone()[0]
            if needs_backfill:
                conn.execute(
                    "UPDATE glossary SET source_language=COALESCE(source_language, 'en'), "
                    "source_term=COALESCE(source_term, english), "
                    "target_language=COALESCE(target_language, 'zh-CN'), "
                    "target_term=COALESCE(target_term, chinese)"
                )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_glossary_language_term "
                "ON glossary(source_language, source_term, target_language, game)"
            )
            schema_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='glossary'"
            ).fetchone()
            schema_sql = str(schema_row["sql"] if schema_row else "").upper()
            if "PRIMARY KEY (ENGLISH, GAME)" in schema_sql:
                # The legacy primary key cannot represent an English acronym
                # and the same Japanese source spelling at once.  Rebuild in
                # place after the on-disk backup above, preserving every row.
                conn.execute('''
                    CREATE TABLE glossary_language_v2 (
                        english TEXT NOT NULL,
                        chinese TEXT NOT NULL,
                        game TEXT NOT NULL DEFAULT 'wuwa',
                        category TEXT DEFAULT '',
                        source_language TEXT NOT NULL,
                        source_term TEXT NOT NULL,
                        target_language TEXT NOT NULL,
                        target_term TEXT NOT NULL,
                        PRIMARY KEY (source_language, source_term, target_language, game)
                    )
                ''')
                conn.execute('''
                    INSERT INTO glossary_language_v2
                    (english, chinese, game, category, source_language, source_term, target_language, target_term)
                    SELECT english, chinese, game, category, source_language, source_term, target_language, target_term
                    FROM glossary
                ''')
                conn.execute("DROP TABLE glossary")
                conn.execute("ALTER TABLE glossary_language_v2 RENAME TO glossary")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_game ON glossary(game)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON glossary(category)")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_glossary_language_term "
                    "ON glossary(source_language, source_term, target_language, game)"
                )
            conn.commit()
        self._all_terms: Optional[Dict[str, str]] = None
        self._all_terms_game: Optional[str] = None
        self._all_terms_language: Optional[str] = None
        self._character_terms: Optional[List[Tuple[str, str]]] = None
        self._character_terms_game: Optional[str] = None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """创建数据库连接"""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ---- 写入 ----

    def insert(self, english: str, chinese: str,
               game: str = "wuwa", category: str = "",
               source_language: str = "en", target_language: str = "zh-CN") -> None:
        """
        插入或更新一条术语。

        Args:
            english: 英文术语
            chinese: 中文翻译
            game: 所属游戏
            category: 分类 (character/system/currency/enemy/lore/location)
        """
        pair = normalize_language_pair({
            "source_language": source_language,
            "target_language": target_language,
        })
        source = unicodedata.normalize("NFKC", english.strip())
        target = chinese.strip()
        with self._connect() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO glossary '
                '(english, chinese, game, category, source_language, source_term, target_language, target_term) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (source, target, game.strip(), category.strip(),
                 pair["source_language"], source, pair["target_language"], target)
            )
            conn.commit()
        self._invalidate_cache()

    def insert_batch(self, terms: List[Tuple[str, str]],
                     game: str = "wuwa", category: str = "",
                     source_language: str = "en", target_language: str = "zh-CN") -> int:
        """
        批量插入术语。

        Args:
            terms: [(english, chinese), ...] 列表
            game: 所属游戏
            category: 分类

        Returns:
            插入的条数
        """
        pair = normalize_language_pair({
            "source_language": source_language,
            "target_language": target_language,
        })
        with self._connect() as conn:
            count = conn.executemany(
                'INSERT OR REPLACE INTO glossary '
                '(english, chinese, game, category, source_language, source_term, target_language, target_term) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                [(unicodedata.normalize("NFKC", e.strip()), c.strip(), game.strip(), category.strip(),
                  pair["source_language"], unicodedata.normalize("NFKC", e.strip()),
                  pair["target_language"], c.strip())
                 for e, c in terms]
            ).rowcount
            conn.commit()
        self._invalidate_cache()
        return count

    def delete(self, english: str, game: str = None,
               source_language: str = "en", target_language: str = "zh-CN") -> bool:
        """删除一条术语，返回是否删除成功"""
        pair = normalize_language_pair({
            "source_language": source_language,
            "target_language": target_language,
        })
        source = unicodedata.normalize("NFKC", english.strip())
        with self._connect() as conn:
            if game:
                cur = conn.execute(
                    'DELETE FROM glossary WHERE source_term=? AND game=? '
                    'AND source_language=? AND target_language=?',
                    (source, game.strip(), pair["source_language"],
                     pair["target_language"])
                )
            else:
                cur = conn.execute(
                    'DELETE FROM glossary WHERE source_term=? '
                    'AND source_language=? AND target_language=?',
                    (source, pair["source_language"], pair["target_language"])
                )
            conn.commit()
        self._invalidate_cache()
        return cur.rowcount > 0

    def update(self, english: str, new_english: str, chinese: str,
               game: str = "wuwa", category: str = "",
               source_language: str = "en", target_language: str = "zh-CN") -> bool:
        """Update or rename one term in a single SQLite transaction."""
        pair = normalize_language_pair({
            "source_language": source_language,
            "target_language": target_language,
        })
        old_source = unicodedata.normalize("NFKC", english.strip())
        new_source = unicodedata.normalize("NFKC", new_english.strip())
        target = chinese.strip()
        clause = "LOWER(source_term)=LOWER(?)" if pair["source_language"] == "en" else "source_term=?"
        with self._connect() as conn:
            cur = conn.execute(
                'UPDATE glossary SET english=?, chinese=?, category=?, '
                'source_term=?, target_term=? WHERE ' + clause
                + ' AND game=? AND source_language=? AND target_language=?',
                (new_source, target, category.strip(), new_source, target,
                 old_source, game.strip(), pair["source_language"],
                 pair["target_language"]),
            )
            conn.commit()
        self._invalidate_cache()
        return cur.rowcount > 0

    # ---- 查询 ----

    def lookup(self, english: str, game: str = None,
               source_language: str = "en", target_language: str = "zh-CN") -> Optional[str]:
        """
        查询单个术语的中文翻译。

        Args:
            english: 英文术语（大小写不敏感）
            game: 限定游戏，None 则返回最先匹配的
        """
        pair = normalize_language_pair({
            "source_language": source_language,
            "target_language": target_language,
        })
        source = unicodedata.normalize("NFKC", english.strip())
        with self._connect() as conn:
            clause = "LOWER(source_term)=LOWER(?)" if pair["source_language"] == "en" else "source_term=?"
            params = [source, pair["source_language"], pair["target_language"]]
            sql = (
                "SELECT target_term FROM glossary WHERE " + clause
                + " AND source_language=? AND target_language=?"
            )
            if game:
                sql += " AND game=?"
                params.append(game.strip())
            row = conn.execute(sql + " ORDER BY source_term", params).fetchone()
        return row['target_term'] if row else None

    def extract(self, terms: List[str], game: str = "wuwa",
                source_language: str = "en") -> Dict[str, str]:
        """
        从术语列表中提取存在的翻译映射。

        对每个 term 做大小写不敏感查询，同时保持返回字典的 key 为原始英文形式。

        Args:
            terms: 英文术语列表（可能包含不在库中的词）
            game: 限定游戏

        Returns:
            {原始英文: 中文翻译} 映射表，只包含在库中存在的术语
        """
        result = {}
        if not terms:
            return result

        for term in dict.fromkeys(term.strip() for term in terms if term.strip()):
            translated = self.lookup(
                term, game=game, source_language=source_language,
            )
            if translated:
                result[term] = translated

        return result

    def extract_from_text(self, text: str, game: str = "wuwa",
                          source_language: str = "en") -> Dict[str, str]:
        """
        从字幕文本中自动识别术语。

        将文本中的每个单词与术语库比对（大小写不敏感）。

        Args:
            text: 字幕文本（单条或多行）
            game: 限定游戏

        Returns:
            {匹配到的英文术语: 中文翻译}
        """
        if not text.strip():
            return {}

        # 从文本中提取所有单词（保留含连字符/撇号的复合词）
        # Match database terms directly so multi-word names such as
        # "Tower of Adversity" are not lost. Boundaries prevent short terms
        # from matching inside unrelated words.
        source_language = normalize_language_pair({
            "source_language": source_language,
        })["source_language"]
        if (self._all_terms is None or self._all_terms_game != game
                or self._all_terms_language != source_language):
            self._all_terms = {
                item["source_term"]: item["target_term"]
                for item in self.list_all(game=game, source_language=source_language)
            }
            self._all_terms_game = game
            self._all_terms_language = source_language
        searchable_text = text
        if source_language == "en":
            # A contextually identified multiword brand outranks a nested
            # generic term.  Mask only for this extraction call: the phrase
            # stays task-local and is never written into the glossary.
            from pipeline.entity_resolution import resolve_entity_candidates

            contextual_surfaces = {
                item["surface"]
                for item in resolve_entity_candidates(
                    text, self._all_terms, source_language="en",
                )
                if item.get("classification") == "contextual_entity"
            }
            for surface in sorted(contextual_surfaces, key=len, reverse=True):
                escaped_surface = re.escape(surface).replace(r"\ ", r"\s+")
                searchable_text = re.sub(
                    rf"(?<![A-Za-z0-9]){escaped_surface}(?![A-Za-z0-9])",
                    lambda match: " " * len(match.group(0)),
                    searchable_text,
                    flags=re.IGNORECASE,
                )
        matched = {}
        selected_non_english_terms: list[str] = []
        selected_english_spans: list[tuple[int, int]] = []
        for english, chinese in sorted(
            self._all_terms.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if (source_language in {"ja", "ko"} and any(
                    english in existing for existing in selected_non_english_terms
            )):
                continue
            escaped_term = re.escape(english).replace(r"\ ", r"\s+")
            if source_language == "ja":
                matched_term = (
                    unicodedata.normalize("NFKC", english)
                    in unicodedata.normalize("NFKC", text)
                )
            elif source_language == "ko":
                normalised_term = unicodedata.normalize("NFKC", english)
                normalised_text = unicodedata.normalize("NFKC", text)
                # Korean particles attach on the right, so a right word
                # boundary would hide valid names (스카는).  A left Hangul
                # boundary is still required to prevent short names from
                # matching inside unrelated words (메카스카, 당신이).
                matched_term = re.search(
                    rf"(?<![\uac00-\ud7af]){re.escape(normalised_term)}",
                    normalised_text,
                )
            else:
                matched_term = False
                for term_match in re.finditer(
                    rf"(?<![A-Za-z0-9]){escaped_term}(?![A-Za-z0-9])",
                    searchable_text,
                    re.IGNORECASE,
                ):
                    if any(
                        term_match.start() < end and term_match.end() > start
                        for start, end in selected_english_spans
                    ):
                        continue
                    selected_english_spans.append(term_match.span())
                    matched_term = True
            if matched_term:
                matched[english] = chinese
                if source_language in {"ja", "ko"}:
                    selected_non_english_terms.append(english)
        if source_language == "en":
            matched.update(self._extract_fuzzy_character_terms(
                searchable_text, game=game, exact_matches=matched
            ))
        return matched

    def _extract_fuzzy_character_terms(
        self,
        text: str,
        *,
        game: str,
        exact_matches: Dict[str, str],
    ) -> Dict[str, str]:
        """Match only unambiguous, near-edit ASR spellings of proper names."""
        if (
            self._character_terms is None
            or self._character_terms_game != game
        ):
            self._character_terms = [
                (item["english"], item["chinese"])
                for item in self.list_all(game=game)
                if item.get("category") in _FUZZY_NAME_CATEGORIES
                and "(" not in item["english"]
            ]
            self._character_terms_game = game
        if not self._character_terms:
            return {}

        words = re.findall(r"[A-Za-z][A-Za-z'’-]*", text)
        exact_normalised = {
            _normalise_name(term) for term in exact_matches
        }
        candidates: dict[str, str] = {}
        evaluated: set[str] = set()
        for word_count in sorted({
            len(re.findall(r"[A-Za-z]+", english))
            for english, _ in self._character_terms
        }):
            if word_count < 1 or word_count > 3:
                continue
            for index in range(0, len(words) - word_count + 1):
                parts = words[index:index + word_count]
                surface = " ".join(parts)
                normalised = _normalise_name(surface)
                has_name_casing = any(
                    part[:1].isupper() for part in parts
                )
                if (
                    len(normalised) < 4
                    or normalised in exact_normalised
                    or normalised in evaluated
                    or normalised in _FUZZY_NAME_STOPWORDS
                    or (not has_name_casing and len(normalised) < 6)
                ):
                    continue
                evaluated.add(normalised)

                ranked = []
                for official, chinese in self._character_terms:
                    official_words = re.findall(r"[A-Za-z]+", official)
                    if len(official_words) != word_count:
                        continue
                    official_normalised = _normalise_name(official)
                    length = max(len(normalised), len(official_normalised))
                    if abs(
                        len(normalised) - len(official_normalised)
                    ) > (2 if has_name_casing else 1):
                        continue
                    if (
                        not has_name_casing
                        and normalised[0] != official_normalised[0]
                    ):
                        continue
                    distance = _edit_distance(normalised, official_normalised)
                    if has_name_casing:
                        maximum_distance = 1 if length <= 5 else 2
                        minimum_ratio = 0.80 if length <= 5 else 0.82
                    else:
                        maximum_distance = 1
                        minimum_ratio = 0.85
                    ratio = SequenceMatcher(
                        None, normalised, official_normalised
                    ).ratio()
                    if distance <= maximum_distance and ratio >= minimum_ratio:
                        ranked.append(
                            (ratio, -distance, official, chinese)
                        )
                ranked.sort(reverse=True)
                if not ranked:
                    continue
                best = ranked[0]
                if (
                    len(ranked) > 1
                    and best[3] != ranked[1][3]
                    and best[0] - ranked[1][0] < 0.08
                ):
                    continue
                candidates[surface] = best[3]
        return candidates

    def extract_batch(self, subtitles: list, game: str = "wuwa",
                      source_language: str = "en") -> Dict[str, str]:
        """
        从多条字幕中提取术语。

        Args:
            subtitles: Subtitle 对象列表（含 .text 属性）
            game: 限定游戏

        Returns:
            所有字幕中匹配到的术语映射
        """
        combined = ' '.join(sub.text for sub in subtitles if sub.text)
        return self.extract_from_text(
            combined, game=game, source_language=source_language,
        )

    def list_all(self, game: str = None, category: str = None,
                 source_language: str = None,
                 target_language: str = "zh-CN") -> List[Dict]:
        """列出术语库中的术语"""
        with self._connect() as conn:
            sql = ('SELECT source_language, source_term, target_language, target_term, '
                   'game, category FROM glossary WHERE 1=1')
            params = []
            if game:
                sql += ' AND game=?'
                params.append(game)
            if category:
                sql += ' AND category=?'
                params.append(category)
            if source_language:
                sql += ' AND source_language=? AND target_language=?'
                params.extend([source_language, target_language])
            sql += ' ORDER BY game, category, source_term'
            rows = conn.execute(sql, params).fetchall()
        return [{
            **dict(row),
            "english": row["source_term"],
            "chinese": row["target_term"],
        } for row in rows]

    def stats(self) -> Dict:
        """返回术语库统计信息"""
        with self._connect() as conn:
            total = conn.execute('SELECT COUNT(*) FROM glossary').fetchone()[0]
            games = conn.execute(
                'SELECT game, COUNT(*) as cnt FROM glossary GROUP BY game ORDER BY cnt DESC'
            ).fetchall()
            categories = conn.execute(
                'SELECT category, COUNT(*) as cnt FROM glossary GROUP BY category ORDER BY cnt DESC'
            ).fetchall()
        return {
            'total': total,
            'by_game': [(r['game'], r['cnt']) for r in games],
            'by_category': [(r['category'], r['cnt']) for r in categories],
        }

    def _invalidate_cache(self):
        """清除缓存"""
        self._all_terms = None
        self._all_terms_game = None
        self._all_terms_language = None
        self._character_terms = None
        self._character_terms_game = None

    def close(self):
        """释放数据库资源（调用后实例不可再用）"""
        pass  # SQLite 连接由 _connect() 托管，每次用完即关


_SHIPPED_MULTILINGUAL_SEEDS = {
    "ja_person_terms.json": ("ja", {
        "character", "character_alias", "story_character",
    }),
    "ja_place_terms.json": ("ja", {"location"}),
    "ko_person_terms.json": ("ko", {
        "character", "character_alias", "story_character",
    }),
}


def _seed_shipped_multilingual_terms(db: GlossaryDB, data_dir: str) -> None:
    """Add shipped non-English terms without replacing user-maintained entries."""

    rows = []
    for filename, (expected_language, allowed_categories) in _SHIPPED_MULTILINGUAL_SEEDS.items():
        source = os.path.join(data_dir, filename)
        if not os.path.isfile(source):
            continue
        try:
            with open(source, encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            payload.get("source_language") != expected_language
            or payload.get("target_language") != "zh-CN"
            or not isinstance(payload.get("terms"), list)
        ):
            continue
        for item in payload["terms"]:
            if not isinstance(item, dict):
                continue
            source_term = unicodedata.normalize(
                "NFKC", str(item.get("source", "")).strip(),
            )
            target_term = str(item.get("target", "")).strip()
            category = str(item.get("category", "")).strip()
            if source_term and target_term and category in allowed_categories:
                rows.append((
                    source_term, target_term,
                    str(payload.get("game") or "wuwa"), category,
                    expected_language, source_term, "zh-CN", target_term,
                ))
    if not rows:
        return
    with db._connect() as conn:
        conn.executemany(
            'INSERT OR IGNORE INTO glossary '
            '(english, chinese, game, category, source_language, source_term, target_language, target_term) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            rows,
        )
        conn.commit()
    db._invalidate_cache()


def load_glossary_db(data_dir: str = None) -> GlossaryDB:
    """
    便捷加载 GlossaryDB。

    Args:
        data_dir: data/ 目录路径，默认使用项目 data/ 目录

    Returns:
        GlossaryDB 实例
    """
    if data_dir is None:
        data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data",
        )
    db_path = os.path.join(data_dir, "glossary.db")
    database = GlossaryDB(db_path)
    _seed_shipped_multilingual_terms(database, data_dir)
    return database
