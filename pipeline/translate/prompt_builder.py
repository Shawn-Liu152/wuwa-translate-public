"""
Prompt Builder — 翻译 Prompt 生成器

根据术语表和字幕 batch 动态组装翻译 Prompt。

职责：
- 从 data/prompt_template.txt 读取模板
- 将术语表（glossary）和字幕（batch）注入模板
- 生成结构化的翻译指令
- 规则修改只需编辑模板文件，无需改代码

模板变量：
- {glossary}     — 术语表，格式化为 "English = 中文" 列表
- {subtitles}    — 待翻译字幕，格式化为 "[序号] 原文" 列表
- {format_example} — 动态生成输出格式示例
- {tricky_terms}  — 易错词修正表，从 data/tricky_terms.json 加载
"""
import json
import logging
import os
import re
import unicodedata
from typing import Dict, List

from pipeline.languages import normalize_language_pair
from pipeline.translation_memory import normalize_source

log = logging.getLogger("prompt_builder")

# 韩文 few-shot 匹配用的常见助词/词尾（检索时剥离，避免 스트라이더를 vs 스트라이더 失配）
_KO_FEWSHOT_PARTICLE_TAILS = (
    "습니다", "니까", "는데", "면서", "이라고", "이라는", "라고", "하는",
    "해서", "하며", "이며", "이고", "하고", "까지", "부터", "에게", "에서",
    "으로", "로는", "로도", "라는", "이는", "는", "은", "이", "가", "을",
    "를", "의", "에", "도", "만", "와", "과", "나", "다", "요", "죠",
    "네", "거", "게", "야",
)

# 语言显示标签（few-shot 示例前缀）
_LANG_LABELS = {"en": "EN", "ja": "JA", "ko": "KO"}


class PromptBuilder:
    """
    Prompt 生成器。

    使用方式：
        builder = PromptBuilder("data/prompt_template.txt")
        prompt = builder.build(
            batch=subtitles,
            glossary={"Changli": "长离", "Echo": "声骸"}
        )
    """

    def __init__(
        self, template_path: str, *, support_data_dir: str | None = None,
        source_language: str = "en",
    ):
        """
        Args:
            template_path: prompt 模板文件路径
        """
        with open(template_path, 'r', encoding='utf-8') as f:
            self._template = f.read()
        # Older user-edited templates predate the context-memory placeholders.
        # Insert only missing sections immediately before the subtitle block so
        # upgrades do not silently disable the new evidence path.
        context_placeholders = (
            "{global_context_section}",
            "{dialogue_memory_section}",
            "{translation_memory_section}",
        )
        missing = [
            placeholder for placeholder in context_placeholders
            if placeholder not in self._template
        ]
        if missing and "{subtitles}" in self._template:
            self._template = self._template.replace(
                "{subtitles}", "".join(missing) + "{subtitles}", 1
            )
        # 加载易错词修正表
        self.source_language = normalize_language_pair({
            "source_language": source_language,
        })["source_language"]
        tricky_path = os.path.join(
            support_data_dir or os.path.dirname(template_path),
            'tricky_terms.json',
        )
        self._tricky_terms = []
        if os.path.isfile(tricky_path):
            with open(tricky_path, 'r', encoding='utf-8') as f:
                self._tricky_terms = json.load(f)

    def build(self,
              batch: list,
              glossary: Dict[str, str] = None,
              full_glossary: Dict[str, str] = None,
              all_subs: list = None,
              established_terms: Dict[str, str] = None,
              refine_pairs: list = None,
              global_context: dict = None,
              translation_memory: list[dict] = None) -> str:
        """
        组装完整的翻译 Prompt。

        Args:
            batch: Subtitle 对象列表（含 .id 和 .text 属性）
            glossary: 当前 batch 命中的术语映射表 {english: chinese}
            full_glossary: 完整术语表 {english: chinese}（按需传入以补充覆盖）
            all_subs: 全量字幕列表（用于提供上下文，可选）
            established_terms: 已在前序批次中出现过的术语→译名映射（跨批次术语锚定）
            refine_pairs: 精校模式下的 (原文, 参考译文) 列表，提供 Round 1 译文用于修正
            global_context: 视频标题、频道、游戏和全片锁定术语
            translation_memory: 人工明确批准、允许跨任务复用的历史译法

        Returns:
            完整的 Prompt 字符串，可直接发送给 LLM
        """
        glossary_text = self._format_glossary(glossary)

        # 如果提供了完整术语表，追加到 glossary section
        if full_glossary:
            extra = {k: v for k, v in full_glossary.items()
                     if k not in (glossary or {})}
            if extra:
                glossary_text += "\n" + self._format_glossary(extra)

        # 精校模式：附带 Round 1 参考译文
        if refine_pairs:
            subtitles_text = self._format_subtitles_refine(refine_pairs)
        elif all_subs:
            subtitles_text = self._format_subtitles_with_context(batch, all_subs)
        else:
            subtitles_text = self._format_subtitles(batch)

        # 跨批次术语锚定
        established_section = self._format_established_terms(
            established_terms, exclude=glossary
        )

        format_example = self._format_example(batch)
        tricky_text = self._format_tricky_terms(subtitles_text)
        global_context_section = self._format_global_context(global_context)
        dialogue_memory_section = self._format_dialogue_memory(batch, all_subs)
        translation_memory_section = self._format_translation_memory(
            batch, translation_memory
        )

        prompt = self._template.format(
            glossary=glossary_text,
            established_terms_section=established_section,
            subtitles=subtitles_text,
            format_example=format_example,
            tricky_terms=tricky_text,
            global_context_section=global_context_section,
            dialogue_memory_section=dialogue_memory_section,
            translation_memory_section=translation_memory_section,
        )

        return prompt

    # ---- 格式化辅助方法（可被子类覆写以定制格式） ----

    def _format_glossary(self, glossary: Dict[str, str]) -> str:
        """将术语表格式化为文本"""
        if not glossary:
            return "（本批无术语）"
        lines = []
        for eng, chs in sorted(glossary.items(), key=lambda item: item[0].casefold()):
            lines.append(f"  {eng} = {chs}")
        return '\n'.join(lines)

    def _format_subtitles(self, batch: list) -> str:
        """将字幕 batch 格式化为 [序号] 原文"""
        lines = []
        for sub in batch:
            text = sub.text.replace('\n', ' ')
            lines.append(f"[{sub.id}] {text}")
        return '\n'.join(lines)

    def _format_subtitles_with_context(self, batch: list, all_subs: list,
                                       context_window: int = 6) -> str:
        """将字幕格式化为带上下文的列表，帮助 LLM 理解语境。"""
        if not all_subs or len(all_subs) <= len(batch):
            # 全量字幕就是 batch 本身，无需上下文
            return self._format_subtitles(batch)

        lines = []
        batch_ids = {sub.id for sub in batch}
        positions = {sub.id: index for index, sub in enumerate(all_subs)}
        included_positions: set[int] = set()
        for batch_sub in batch:
            position = positions.get(batch_sub.id)
            if position is None:
                continue
            start = max(0, position - context_window)
            end = min(len(all_subs), position + context_window + 1)
            included_positions.update(range(start, end))

        for position in sorted(included_positions):
            sub = all_subs[position]
            if sub.id in batch_ids:
                text = sub.text.replace('\n', ' ')
                lines.append(f">>> [{sub.id}] {text} <<<")
            else:
                text = sub.text.replace('\n', ' ')
                lines.append(f"    [{sub.id}] {text}")
        return '\n'.join(lines)

    def _format_global_context(self, context: dict | None) -> str:
        if not context:
            return ""
        lines = ["【全局视频记忆】"]
        labels = {
            "title": "视频标题",
            "channel": "频道/主播",
            "game": "游戏",
            "source_synopsis": "全片概要",
            "pronoun_policy": "代词策略",
        }
        for key, label in labels.items():
            value = str(context.get(key, "")).strip()
            if value:
                limit = 800 if key == "source_synopsis" else 300
                lines.append(f"  {label}: {value[:limit]}")
        entities = context.get("entities") or []
        if isinstance(entities, (list, tuple)):
            rendered_entities = "、".join(
                str(item).strip() for item in entities[:40] if str(item).strip()
            )
            if rendered_entities:
                lines.append(f"  已识别实体: {rendered_entities}")
        entity_candidates = context.get("entity_candidates") or []
        if isinstance(entity_candidates, list) and entity_candidates:
            lines.append("  待人工确认的人名候选（只用于消歧，不得自动建立永久译名）:")
            for item in entity_candidates[:20]:
                lines.append(
                    f"    {item.get('surface')} ≈ {item.get('official_source')}"
                    f" → {item.get('official_target')}"
                    f"（置信度 {item.get('confidence')}）"
                )
        terms = context.get("known_terms") or {}
        if isinstance(terms, dict) and terms:
            rendered = "；".join(
                f"{english} = {chinese}"
                for english, chinese in sorted(terms.items())
            )
            lines.append(f"  全片已锁定专名: {rendered}")
        if len(lines) == 1:
            return ""
        lines.append(
            "  以上只用于消歧、称呼和代词衔接，不得凭空补进字幕。"
        )
        return "\n".join(lines) + "\n\n"

    def _format_dialogue_memory(
        self,
        batch: list,
        all_subs: list | None,
        history_window: int = 12,
    ) -> str:
        if not batch or not all_subs:
            return ""
        positions = {sub.id: index for index, sub in enumerate(all_subs)}
        batch_positions = [
            positions[sub.id] for sub in batch if sub.id in positions
        ]
        if not batch_positions:
            return ""
        first = min(batch_positions)
        history = all_subs[max(0, first - history_window):first]
        if not history:
            return ""
        condensed = " / ".join(
            re.sub(r"\s+", " ", str(sub.text)).strip()
            for sub in history if str(sub.text).strip()
        )
        if not condensed:
            return ""
        return (
            "【近期对话记忆（仅供理解，不要输出）】\n"
            f"  {condensed}\n\n"
        )

    def _format_translation_memory(
        self,
        batch: list,
        entries: list[dict] | None,
    ) -> str:
        if not entries or not batch:
            return ""
        target_text = normalize_source(" ".join(
            str(sub.text).replace("\n", " ") for sub in batch
        ), source_language=self.source_language)
        matched = []
        for entry in entries:
            if not entry.get("approved"):
                continue
            source = str(entry.get("source", "")).strip()
            final = str(entry.get("final", "")).strip()
            source_key = str(entry.get("source_normalized", "")).strip()
            source_key = source_key or normalize_source(
                source, source_language=self.source_language,
            )
            if source_key and final and source_key in target_text:
                matched.append((source, final))
        sections = []
        if matched:
            lines = ["【已人工批准的历史译法】"]
            lines.extend(f"  {source} = {final}" for source, final in matched)
            lines.append("  仅在同语言原文完全一致时沿用，不得类推到相似句。")
            sections.append("\n".join(lines))
        # Few-shot 示例：与当前批次有韩文词重叠的已批准句对，
        # 让模型模仿确认过的语气/术语/句式（示例库随人工审计持续积累）。
        few_shot = self._format_few_shot_examples(batch, entries)
        if few_shot:
            sections.append(few_shot)
        if not sections:
            return ""
        return "\n\n".join(sections) + "\n\n"

    def _language_tokens(self, text: str) -> set[str]:
        """语言感知分词：en 空格；ja 字符 bigram；ko 音节+助词剥离。"""
        text = unicodedata.normalize("NFKC", str(text or "")).replace(">>", " ")
        if self.source_language == "ja":
            # 日文无空格：用字符 bigram 覆盖同词不同语尾/变体
            compact = re.sub(r"[\W_]+", "", text)
            if len(compact) < 2:
                return set()
            return {compact[i:i + 2] for i in range(len(compact) - 1)}
        if self.source_language == "ko":
            tokens: set[str] = set()
            for run in re.findall(r"[\uac00-\ud7af]+", text):
                if len(run) < 2:
                    continue
                stripped = run
                for tail in _KO_FEWSHOT_PARTICLE_TAILS:
                    if stripped.endswith(tail) and len(stripped) > len(tail):
                        stripped = stripped[: -len(tail)]
                        break
                if len(stripped) >= 2:
                    tokens.add(stripped)
            return tokens
        # en：空格分词 + casefold
        return {
            tok for tok in text.casefold().split()
            if len(tok) >= 2
        }

    def _few_shot_score(
        self, batch_tokens: set[str], source_tokens: set[str],
    ) -> float:
        """重叠率 = 交集 / 较长方，越强越相关（抗 'wow' 类弱重叠污染）。"""
        if not batch_tokens or not source_tokens:
            return 0.0
        overlap = len(batch_tokens & source_tokens)
        if overlap <= 0:
            return 0.0
        return overlap / max(len(batch_tokens), len(source_tokens))

    def _format_few_shot_examples(
        self, batch: list, entries: list[dict] | None,
    ) -> str:
        diagnostics = self.few_shot_diagnostics(batch, entries)
        examples = diagnostics["selected"]
        if not examples:
            return ""
        log.debug(
            "few-shot retrieval: candidates=%d selected=%d scores=%s lang=%s",
            diagnostics["candidate_count"], diagnostics["selected_count"],
            [item["score"] for item in examples], self.source_language,
        )
        label = _LANG_LABELS.get(self.source_language, self.source_language.upper())
        lines = ["【正确翻译示例（模仿其语气、术语与句式）】"]
        for item in examples:
            lines.append(f"  {label}: {item['source']}")
            lines.append(f"  ZH: {item['final']}")
        return "\n".join(lines)

    def few_shot_diagnostics(
        self, batch: list, entries: list[dict] | None,
    ) -> dict:
        """Return the exact few-shot selection used by :meth:`build`."""
        empty = {
            "language": self.source_language,
            "candidate_count": 0,
            "selected_count": 0,
            "selected": [],
        }
        if not entries or not batch:
            return empty
        cue_token_sets = [
            tokens for tokens in (
                self._language_tokens(str(sub.text)) for sub in batch
            )
            if tokens
        ]
        if not cue_token_sets:
            return empty
        scored: list[tuple[float, str, str]] = []
        seen = set()
        for entry in entries:
            if not entry.get("approved"):
                continue
            source = str(entry.get("source", "")).strip()
            final = str(entry.get("final", "")).strip()
            if not source or not final or source in seen:
                continue
            source_tokens = self._language_tokens(source)
            best_score = 0.0
            for cue_tokens in cue_token_sets:
                overlap = cue_tokens & source_tokens
                if self.source_language == "ja" and len(overlap) < 3:
                    continue
                # Do not combine unrelated weak matches from different cues.
                # A single distinctive token can still retrieve a longer
                # example, but two generic tokens must occur in one cue.
                distinctive_single = (
                    len(overlap) == 1
                    and len(next(iter(overlap))) >= 4
                )
                if (
                    len(source_tokens) > 1
                    and len(overlap) < 2
                    and not distinctive_single
                ):
                    continue
                best_score = max(
                    best_score,
                    self._few_shot_score(cue_tokens, source_tokens),
                )
            if best_score <= 0:
                continue
            seen.add(source)
            scored.append((best_score, source, final))
        scored.sort(key=lambda item: item[0], reverse=True)
        examples = scored[:5]
        reason = {
            "ja": "ja_character_bigram_overlap",
            "ko": "ko_particle_normalized_overlap",
            "en": "en_casefold_token_overlap",
        }.get(self.source_language, "language_token_overlap")
        return {
            "language": self.source_language,
            "candidate_count": len(scored),
            "selected_count": len(examples),
            "selected": [
                {
                    "source": source,
                    "final": final,
                    "score": round(score, 4),
                    "reason": reason,
                }
                for score, source, final in examples
            ],
        }

    def _format_tricky_terms(self, source_text: str = "") -> str:
        """只注入当前字幕实际出现的易错词，避免每批重复发送整张表。"""
        if not self._tricky_terms:
            return "（无）"
        lines = []
        for item in self._tricky_terms:
            if item.get("hard", True) is False:
                continue
            pattern = item.get('pattern', '')
            correct = item.get('correct', '')
            escaped_pattern = re.escape(pattern).replace(r"\ ", r"\s+")
            if pattern and re.search(
                rf"(?<![A-Za-z0-9]){escaped_pattern}(?![A-Za-z0-9])",
                source_text,
                re.IGNORECASE,
            ):
                lines.append(f"  {pattern} → {correct}")
        return '\n'.join(lines) if lines else "（无）"

    def _format_example(self, batch: list) -> str:
        """生成输出格式示例"""
        if not batch:
            return "[1] 翻译内容"
        first_id = batch[0].id
        return f"[{first_id}] 翻译内容"

    def _format_established_terms(
        self,
        established_terms: Dict[str, str],
        exclude: Dict[str, str] = None,
    ) -> str:
        """格式化已在前序批次中出现过的术语→译名映射，用于跨批次术语锚定。"""
        if not established_terms:
            return ""
        excluded = {term.casefold() for term in (exclude or {})}
        locked = {
            eng: chs for eng, chs in established_terms.items()
            if eng.casefold() not in excluded
        }
        if not locked:
            return ""
        lines = ["【已锁定译名】"]
        for eng, chs in sorted(locked.items()):
            lines.append(f"  {eng} = {chs}")
        lines.append("")
        return '\n'.join(lines)

    def _format_subtitles_refine(self, refine_pairs: list) -> str:
        """格式化精校模式的字幕：附带 Round 1 参考译文供修正。"""
        lines = []
        for pair in refine_pairs:
            en_text = pair['en'].replace('\n', ' ')
            ref_text = pair['ref'].replace('\n', ' ')
            sub_id = pair['id']
            lines.append(f">>> [{sub_id}] {en_text} <<<")
            lines.append(f"    参考: {ref_text}")
        return '\n'.join(lines)


def load_prompt_builder(
    data_dir: str = None, source_language: str = "en",
    target_language: str = "zh-CN",
) -> PromptBuilder:
    """
    便捷加载 PromptBuilder。

    Args:
        data_dir: data/ 目录路径，默认使用项目 data/ 目录

    Returns:
        PromptBuilder 实例
    """
    if data_dir is None:
        data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data",
        )
    language_pair = normalize_language_pair({
        "source_language": source_language,
        "target_language": target_language,
    })
    language_template = os.path.join(
        data_dir, "prompts",
        f"{language_pair['source_language']}-{language_pair['target_language']}.txt",
    )
    # Existing English installations remain compatible until their editable
    # template is migrated; Japanese must always use its own template.
    template_path = (
        language_template if os.path.isfile(language_template)
        else os.path.join(data_dir, "prompt_template.txt")
    )
    return PromptBuilder(
        template_path, support_data_dir=data_dir,
        source_language=language_pair["source_language"],
    )
