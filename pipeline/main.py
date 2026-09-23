"""
Subtitle Translation Pipeline — 主入口

完整翻译管道：

    读取 SRT → 解析 → Alias修正 → 日文过滤 → 音乐判断
    → 提取术语 → 生成Prompt → LLM翻译 → 验证 → 输出新SRT

使用方式：
    python main.py input/input.srt -o output/output.srt
    python main.py input/input.srt --model gpt-4o --batch-size 30
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import logging

from pipeline.config import Config
from pipeline.languages import normalize_language_pair
from pipeline.parser.srt_parser import parse_srt, save_srt, Subtitle
from pipeline.preprocess.alias import load_alias_fixer
from pipeline.preprocess.japanese import (
    clean_japanese_source, remove_japanese, remove_non_english,
)
from pipeline.preprocess.music import load_music_detector, strip_music_annotations
from pipeline.preprocess.glossary import load_glossary_db
from pipeline.translate.prompt_builder import load_prompt_builder
from pipeline.translate.llm import LLMAPIError, create_translator, thinking_policy_report
from pipeline.postprocess.validate import (
    ValidationError,
    check_english_residual,
    validate,
)
from pipeline.postprocess.stats import Profiler
from pipeline.pipeline_manifest import PipelineCancelled, raise_if_cancelled
from pipeline.safe_errors import public_error_fields

# 日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)


def _language_name(code: str) -> str:
    return {"en": "英语", "ja": "日语", "ko": "韩语"}.get(code, code)


def _contains_glossary_term(
    text: str, term: str, source_language: str = "en",
) -> bool:
    if source_language in {"ja", "ko"}:
        import unicodedata
        return unicodedata.normalize("NFKC", term) in unicodedata.normalize("NFKC", text)
    return re.search(
        rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
        text,
        re.IGNORECASE,
    ) is not None


def _count_glossary_term(
    text: str, term: str, source_language: str = "en",
) -> int:
    """Count exact glossary occurrences using the production language boundary."""
    import unicodedata

    normalized_text = unicodedata.normalize("NFKC", text)
    normalized_term = unicodedata.normalize("NFKC", term)
    if not normalized_term:
        return 0
    if source_language == "ja":
        return normalized_text.count(normalized_term)
    if source_language == "ko":
        return len(re.findall(
            rf"(?<![\uac00-\ud7af]){re.escape(normalized_term)}",
            normalized_text,
        ))
    return len(re.findall(
        rf"(?<![A-Za-z0-9]){re.escape(normalized_term)}(?![A-Za-z0-9])",
        normalized_text,
        re.IGNORECASE,
    ))


def _glossary_usage_metrics(
    subtitles: list[Subtitle], glossary: dict[str, str], source_language: str,
) -> dict[str, int]:
    """Return observable per-unit glossary usage for future manifests."""
    hit_units = 0
    occurrences = 0
    for subtitle in subtitles:
        unit_occurrences = sum(
            _count_glossary_term(subtitle.text, term, source_language)
            for term in glossary
        )
        if unit_occurrences:
            hit_units += 1
            occurrences += unit_occurrences
    return {
        "translatable_units": len(subtitles),
        "glossary_hit_units": hit_units,
        "glossary_match_occurrences": occurrences,
    }


def _hard_glossary_targets(
    database, game: str, source_language: str,
) -> set[str] | None:
    """Return targets whose omission is strong enough to retry a subtitle.

    The complete glossary is still supplied to the model.  This narrower set
    only controls the deterministic retry gate: common single-word gameplay
    terms are context-sensitive (for example, ``Launch the game`` is not the
    combat effect ``Launch = 击飞``) and must not make an otherwise valid batch
    fail.
    """
    list_all = getattr(database, "list_all", None)
    if not callable(list_all):
        # Minimal/test/plugin adapters may only expose extract_from_text.
        # Without category metadata, preserve the legacy conservative gate.
        return None
    try:
        rows = list_all(
            game=game, source_language=source_language,
        )
    except TypeError:
        rows = list_all(game=game)

    hard_categories = {
        "character", "story_character", "character_alias",
        "location", "faction", "company", "enemy", "lore", "echo",
        "element",
    }
    hard_system_terms = {
        "Echo", "Lament", "Resonator", "Tacet Mark",
        "Waveworn Phenomenon",
    }
    hard_targets = set()
    for item in rows:
        source = str(
            item.get("source_term") or item.get("english") or ""
        ).strip()
        target = str(
            item.get("target_term") or item.get("chinese") or ""
        ).strip()
        category = str(item.get("category") or "").strip().lower()
        if not source or not target:
            continue
        explicit_ui_shape = bool(
            re.search(r"[%0-9]", source)
            or (source.isupper() and len(source) >= 2)
        )
        multiword = bool(re.search(r"[\s·・-]", source))
        category_is_hard = (
            not category
            or category in hard_categories
            or (category == "system" and source in hard_system_terms)
        )
        if category_is_hard or explicit_ui_shape or multiword:
            hard_targets.add(target)
    return hard_targets


def build_adaptive_batches(
    subtitles: list[Subtitle],
    target_size: int,
    token_budget: int | None = None,
    source_language: str = "en",
) -> list[list[Subtitle]]:
    """Build stable batches without cutting a nearby sentence at count limits."""
    if not subtitles:
        return []
    target_size = max(1, int(target_size))
    token_budget = max(
        256,
        int(token_budget or Config.ROUND1_SOURCE_TOKEN_BUDGET),
    )
    if target_size < 8:
        maximum_size = target_size
    else:
        maximum_size = min(32, target_size + 4)

    def estimated_tokens(subtitle: Subtitle) -> int:
        text = subtitle.text.replace("\n", " ")
        characters_per_token = 2 if source_language in {"ja", "ko"} else 4
        return max(1, (len(text) + characters_per_token - 1) // characters_per_token) + 8

    def closes_sentence(subtitle: Subtitle) -> bool:
        text = subtitle.text.strip()
        return bool(re.search(r"[.!?…。！？][\"'”’）)\]]*$", text))

    def closure_is_near(index: int, lookahead: int = 4) -> bool:
        return any(
            closes_sentence(subtitles[position])
            for position in range(index, min(len(subtitles), index + lookahead))
        )

    batches: list[list[Subtitle]] = []
    current: list[Subtitle] = []
    current_tokens = 0
    for index, subtitle in enumerate(subtitles):
        item_tokens = estimated_tokens(subtitle)
        token_limit_reached = (
            current
            and current_tokens + item_tokens > token_budget
        )
        count_limit_reached = len(current) >= maximum_size
        keep_sentence_together = bool(
            count_limit_reached
            and current
            and not closes_sentence(current[-1])
            and closure_is_near(index)
            and len(current) < maximum_size + 4
        )
        if token_limit_reached or (
            count_limit_reached and not keep_sentence_together
        ):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(subtitle)
        current_tokens += item_tokens
    if current:
        batches.append(current)
    return batches


def _rebase_round_one_manifest(
    old_manifest: dict,
    input_path: str,
    options: dict,
    translatable: list[Subtitle],
    batches: list[list[Subtitle]],
) -> tuple[dict, int]:
    """Rebuild a changed Round 1 checkpoint using exact source signatures."""
    from pipeline.pipeline_manifest import (
        create_manifest,
        set_batch,
        summarize_manifest_metrics,
    )

    prior_options = dict(old_manifest.get("options") or {})
    comparable_keys = {"batch_size", "game", "term_anchor", "refine"}
    if any(prior_options.get(key) != options.get(key) for key in comparable_keys):
        raise ValueError("翻译关键参数已变化，不能自动迁移 Round 1")
    for key in ("context_strategy", "context_signature"):
        if key in prior_options and prior_options.get(key) != options.get(key):
            raise ValueError("翻译上下文或记忆已变化，不能自动迁移 Round 1")

    old_entries = {
        int(item["id"]): item
        for item in old_manifest.get("entries", [])
        if item.get("id") is not None
    }
    reusable_by_signature: dict[tuple[str, str, str], dict] = {}
    reusable_by_id_original: dict[tuple[int, str], dict] = {}
    reusable_by_id_cleaned_original: dict[tuple[int, str], dict] = {}
    for batch in (old_manifest.get("batches") or {}).values():
        for result in batch.get("results") or []:
            try:
                old_id = int(result.get("id"))
            except (TypeError, ValueError):
                continue
            entry = old_entries.get(old_id)
            translated = str(result.get("translated", "")).strip()
            if not translated:
                continue
            original = str(result.get("original", ""))
            if original:
                reusable_by_id_original[(old_id, original)] = result
                cleaned_original = strip_music_annotations(original)
                if cleaned_original:
                    reusable_by_id_cleaned_original[
                        (old_id, cleaned_original)
                    ] = result
            if not entry:
                continue
            signature = (
                str(entry.get("start", "")),
                str(entry.get("end", "")),
                str(entry.get("original", "")),
            )
            reusable_by_signature[signature] = result

    entries = [
        {
            "key": f"{sub.id}|{sub.start}|{sub.end}",
            "id": sub.id,
            "start": sub.start,
            "end": sub.end,
            "original": sub.text,
        }
        for sub in translatable
    ]
    manifest = create_manifest(input_path, options, entries)
    migrated = 0
    for batch_index, batch in enumerate(batches):
        results = []
        for sub in batch:
            old_result = reusable_by_signature.get((sub.start, sub.end, sub.text))
            if old_result is None:
                # Early manifests did not persist ``entries``. The input hash
                # has already been verified, so ID + exact preprocessed source
                # is a safe compatibility key and cannot pull in stale text.
                old_result = reusable_by_id_original.get((sub.id, sub.text))
            if old_result is None:
                # Music-cleaning upgrades may remove a standalone ``>>`` line
                # or an inline cue without changing any spoken words. Reuse is
                # still constrained by the stable subtitle ID and by equality
                # after applying that same lossless annotation cleanup.
                old_result = reusable_by_id_cleaned_original.get(
                    (sub.id, strip_music_annotations(sub.text))
                )
            if old_result is None:
                continue
            results.append({
                "id": sub.id,
                "original": sub.text,
                "translated": str(old_result["translated"]),
                "tokens_in": 0,
                "tokens_out": 0,
                "cost": 0.0,
                "cached": True,
            })
            migrated += 1
        set_batch(
            manifest,
            str(batch_index),
            "success" if len(results) == len(batch) else "pending",
            ids=[sub.id for sub in batch],
            results=results,
            metrics={
                "tokens_in": 0,
                "tokens_out": 0,
                "cost": 0.0,
                "elapsed_seconds": 0.0,
                "glossary_terms": 0,
                "response_attempts": 0,
                "migrated_results": len(results),
            },
        )
    manifest["metrics"] = {
        **summarize_manifest_metrics(manifest),
        "migrated_results": migrated,
    }
    return manifest, migrated


def _failure_extra_fields(scope: dict) -> dict:
    """Q-3：失败/取消批次也保留结构化补译原因；events 仅在补译循环启动后才存在。"""
    events = scope.get("validation_events")
    return {"validation_events": list(events)} if events else {}


def run_pipeline(input_path: str,
                 output_path: str = None,
                 model: str = None,
                 api_key: str = None,
                 batch_size: int = None,
                 game: str = "wuwa",
                 dry_run: bool = False,
                 term_anchor: bool = True,
                 refine: bool = False,
                 refine_input: str = None,
                 manifest_path: str = None,
                 resume: bool = False,
                 fresh: bool = False,
                 dedupe: bool = None,
                 preserve_skipped: bool = True,
                 base_url: str = None,
                 proxy: str = None,
                 cancel_check=None,
                 progress_callback=None,
                 data_dir: str = None,
                 video_context: dict | None = None,
                 source_language: str = "en",
                 target_language: str = "zh-CN") -> int:
    """
    运行完整翻译管道。

    Args:
        input_path: 输入 SRT 文件路径
        output_path: 输出 SRT 文件路径（默认 output/<input_basename>.srt）
        model: 模型名称
        api_key: API 密钥
        batch_size: 批次大小
        game: 游戏名称
        dry_run: 试运行模式（跳过 LLM 调用）
        term_anchor: 是否启用跨批次术语锚定（扫描已完成批次锁定译名）
        refine: 二阶段精校模式（以已有译文为参考重新翻译）
        refine_input: 精校模式参考译文路径
        manifest_path: 批处理状态文件路径（长视频断点恢复）
        resume: 从成功的 manifest batch 恢复
        fresh: 忽略已有 manifest 并重建
        dedupe: 覆盖全局去重开关；long-video union 必须为 False
        video_context: 视频标题、频道等只读全局语境

    Returns:
        0 成功, 非0 失败
    """

    # --- 缺省值 ---
    if output_path is None:
        basename = os.path.splitext(os.path.basename(input_path))[0]
        output_path = os.path.join(Config.OUTPUT_DIR, f"{basename}.zh.srt")
    if model is None:
        model = Config.LLM_MODEL
    if api_key is None:
        api_key = Config.LLM_API_KEY
    if batch_size is None:
        batch_size = Config.BATCH_SIZE
    if manifest_path is None:
        manifest_path = f"{output_path}.manifest.json"
    if dedupe is None:
        dedupe = Config.ENABLE_DEDUPE
    language_pair = normalize_language_pair({
        "source_language": source_language,
        "target_language": target_language,
    })
    source_language = language_pair["source_language"]
    target_language = language_pair["target_language"]

    memory_path = os.path.join(data_dir or Config.DATA_DIR, "translation_memory.json")
    try:
        from pipeline.translation_memory import TranslationMemoryStore
        translation_memory = [
            entry for entry in TranslationMemoryStore(memory_path).list_all()
            if entry.get("approved")
            and str(entry.get("game", "")).casefold() == str(game).casefold()
            and entry.get("source_language") == source_language
            and entry.get("target_language") == target_language
        ]
    except (OSError, ValueError, json.JSONDecodeError):
        log.warning("  翻译记忆不可用，已安全跳过")
        translation_memory = []
    context_signature = hashlib.sha256(json.dumps({
        "video": video_context or {},
        "memory": [
            {"id": item.get("id"), "updated_at": item.get("updated_at")}
            for item in translation_memory
        ],
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    manifest_options = {
        "model": model, "batch_size": batch_size,
        "game": game, "term_anchor": term_anchor, "refine": refine,
        "provider": Config.provider_fingerprint(base_url),
        "source_language": source_language,
        "target_language": target_language,
        "batching": "token-adaptive-semantic-v2",
        "context_strategy": "local-6-history-12-global-v2",
        "context_signature": context_signature,
        "source_token_budget": Config.ROUND1_SOURCE_TOKEN_BUDGET,
        # O-2：上游 thinking 关闭策略占位，preflight 探测后用真实生效值覆盖。
        "llm_disable_thinking": {
            "mode": Config.LLM_DISABLE_THINKING,
            "effective": Config.LLM_DISABLE_THINKING == "on",
            "probed": False,
            "unsupported_reason": None,
        },
    }
    if not preserve_skipped:
        manifest_options["preserve_skipped"] = False

    profiler = Profiler("鸣潮字幕翻译")

    log.info("=" * 50)
    log.info("鸣潮字幕翻译管道")
    log.info("  输入: %s", os.path.basename(input_path))
    log.info("  输出: %s", os.path.basename(output_path))
    log.info(f"  模型: {model}")
    log.info(f"  游戏: {game}")
    log.info(f"  语言: {source_language} → {target_language}")
    log.info(f"  批次: {batch_size} 条/批")
    if dry_run:
        log.info("  模式: 试运行（跳过 LLM）")
    log.info("=" * 50)

    # ============================================================
    # Step 1: 读取 + 解析 SRT + 去重合并
    # ============================================================
    log.info("[1/8] 解析 SRT...")
    try:
        subtitles = parse_srt(input_path)
    except FileNotFoundError:
        log.error("输入文件不存在: %s", os.path.basename(input_path))
        return 1
    except ValueError:
        log.error("SRT 格式错误，请检查时间轴和字幕编号")
        return 1

    if not subtitles:
        log.error("SRT 文件中没有字幕")
        return 1

    log.info(f"  解析完成: {len(subtitles)} 条字幕")

    # 1b. YouTube 字幕去重合并
    if dedupe:
        from pipeline.preprocess.dedupe import dedupe_youtube_subs
        original_count = len(subtitles)
        subtitles = dedupe_youtube_subs(
            subtitles, source_language=source_language,
        )
        log.info(f"  去重合并: {original_count} → {len(subtitles)} 条 ({original_count-len(subtitles)} 条合并)")

    # ============================================================
    # Step 2: 预处理
    # ============================================================
    log.info("[2/8] 预处理...")

    # 2a. Alias 修正
    if Config.ENABLE_ALIAS and source_language == "en":
        alias_fixer = load_alias_fixer(data_dir)
        alias_count = 0
        for sub in subtitles:
            original = sub.text
            fixed = alias_fixer.fix(original)
            if fixed != original:
                alias_count += 1
            sub.text = fixed
        log.info(f"  Alias 修正: {alias_count} 条被修改")

    # 2b. English keeps the historical filter. Japanese is normalized without
    # deleting dialogue, names, or meaningful reaction words.
    if source_language in {"ja", "ko"}:
        cleaned_count = 0
        for sub in subtitles:
            original = sub.text
            sub.text = clean_japanese_source(original)
            if sub.text != original:
                cleaned_count += 1
        log.info(f"  {_language_name(source_language)}安全清理: {cleaned_count} 条")
    elif Config.ENABLE_JAPANESE_FILTER:
        jp_removed = 0
        nonen_removed = 0
        for sub in subtitles:
            original = sub.text
            # 先过滤日文
            filtered = remove_japanese(original)
            if filtered != original:
                jp_removed += 1
            # 再过滤其他非英文字符（中文/韩文/阿拉伯文/泰文/西里尔文）
            final = remove_non_english(filtered)
            if final != filtered:
                nonen_removed += 1
            sub.text = final
        log.info(f"  日文过滤: {jp_removed} 条含日文假名")
        log.info(f"  非英文过滤: {nonen_removed} 条含其他语言字符")

    # 2c. 音乐检测
    music_count = 0
    inline_music_cleaned = 0
    music_detector = None
    clean_music = strip_music_annotations
    if Config.ENABLE_MUSIC_DETECTION:
        music_detector = load_music_detector(data_dir)
        clean_music = getattr(
            music_detector, "strip_markers", strip_music_annotations
        )
        for sub in subtitles:
            original = sub.text
            sub.text = clean_music(original)
            if sub.text != original:
                inline_music_cleaned += 1
            if not sub.text.strip() and original.strip():
                music_count += 1
        log.info(f"  音乐检测: {music_count} 条标记为音乐/音效")
        log.info("  行内音乐/音效标记清理: %d 条", inline_music_cleaned)

    # ============================================================
    # Step 3: 加载术语库
    # ============================================================
    log.info("[3/8] 加载术语库...")
    glossary_db = load_glossary_db(data_dir)
    hard_glossary_targets = _hard_glossary_targets(
        glossary_db, game, source_language,
    )
    glossary_text = ' '.join(sub.text for sub in subtitles if sub.text)
    # Keep the historical two-argument call for third-party/legacy glossary
    # adapters.  The language-aware parameter is required only for Japanese.
    if source_language == "en":
        all_glossary = glossary_db.extract_from_text(glossary_text, game=game)
    else:
        all_glossary = glossary_db.extract_from_text(
            glossary_text, game=game, source_language=source_language,
        )

    def extract_glossary(text: str):
        """Call legacy English glossary adapters without a new keyword."""
        if source_language == "en":
            return glossary_db.extract_from_text(text, game=game)
        return glossary_db.extract_from_text(
            text, game=game, source_language=source_language,
        )
    if source_language in {"ja", "ko"} and video_context:
        context_text = " ".join(
            str(video_context.get(key, "")).strip()
            for key in ("title", "channel")
            if str(video_context.get(key, "")).strip()
        )
        if context_text:
            all_glossary.update(extract_glossary(context_text))
    log.info(f"  匹配术语: {len(all_glossary)} 个")
    for eng, chs in sorted(all_glossary.items()):
        log.info(f"    {eng} = {chs}")

    log.info(f"  仅向各批次注入实际命中的术语，共 {len(all_glossary)} 条")

    # ============================================================
    # Step 4: 准备 Prompt Builder
    # ============================================================
    log.info("[4/8] 加载 Prompt 模板...")
    # The English entrypoint stays callable by existing integrations that
    # replace ``load_prompt_builder`` with its historical one-argument form.
    if source_language == "en":
        prompt_builder = load_prompt_builder(data_dir)
    else:
        prompt_builder = load_prompt_builder(
            data_dir, source_language=source_language,
            target_language=target_language,
        )
    # 文档级术语用于跨批次一致性；Prompt 仍只接收当前批次命中项。
    full_glossary_map = all_glossary.copy()

    # ============================================================
    # Step 4b: 提前扫描全文术语清单，用于跨批次锚定
    # ============================================================
    if term_anchor and Config.ENABLE_GLOSSARY:
        if full_glossary_map:
            log.info(f"  全文出现术语: {len(full_glossary_map)} 个，将在跨批次间锚定译名")

    prompt_global_context = {
        "game": game,
        **(video_context or {}),
        "known_terms": full_glossary_map,
        "entities": list(dict.fromkeys(full_glossary_map.values()))[:40],
        "source_synopsis": " / ".join(
            re.sub(r"\s+", " ", sub.text).strip()
            for sub in subtitles[:12] if sub.text.strip()
        )[:800],
        "pronoun_policy": (
            "主语或性别证据不足时不补他/她；人工确认优先"
            if source_language in {"ja", "ko"} else
            "沿用已确认说话人与代词，不根据单句臆测"
        ),
    }
    if source_language in {"ja", "ko"}:
        from pipeline.entity_resolution import resolve_entity_candidates
        list_all = getattr(glossary_db, "list_all", None)
        official_people = {
            str(item["english"]): str(item["chinese"])
            for item in (
                list_all(game=game, source_language=source_language)
                if callable(list_all) else []
            )
            if str(item.get("category", "")) in {
                "character", "character_alias", "story_character",
            }
        }
        prompt_global_context["entity_candidates"] = resolve_entity_candidates(
            glossary_text,
            official_people,
            source_language=source_language,
        )[:20]

    # ============================================================
    # Step 4c: 精校模式 — 解析参考译文
    # ============================================================
    refine_map = {}  # {id: translated_text}
    if refine:
        if refine_input is None:
            # 默认：output_path 替换 .srt → .zh.gpt56.final.srt
            refine_input = output_path
        log.info(f"  精校模式: 加载参考译文 {refine_input}")
        if os.path.exists(refine_input):
            from pipeline.parser.srt_parser import parse_srt as _parse_ref
            ref_subs = _parse_ref(refine_input)
            refine_map = {s.id: s.text for s in ref_subs if s.text.strip()}
            log.info(f"  参考译文: {len(refine_map)} 条")
        else:
            log.warning(f"  参考译文不存在: {refine_input}，回退到普通翻译模式")
            refine = False

    # ============================================================
    # Step 5: 分批
    # ============================================================
    log.info(f"[5/8] 分批翻译 ({batch_size} 条/批)...")

    # 只翻译非音乐、非空的字幕
    translatable = []
    music_subs = []  # 不翻译但保留

    if Config.ENABLE_MUSIC_DETECTION:
        for sub in subtitles:
            if not sub.text.strip():
                music_subs.append(sub)
            else:
                translatable.append(sub)
    else:
        translatable = [s for s in subtitles if s.text.strip()]
        music_subs = [s for s in subtitles if not s.text.strip()]

    log.info(f"  待翻译: {len(translatable)} 条, 跳过: {len(music_subs)} 条")

    # 切分 batch
    batches = build_adaptive_batches(
        translatable, batch_size, source_language=source_language,
    )

    batch_sizes = [len(batch) for batch in batches]
    log.info(
        "  共 %d 批（Token 自适应，每批 %d–%d 条）",
        len(batches),
        min(batch_sizes, default=0),
        max(batch_sizes, default=0),
    )
    if progress_callback:
        progress_callback(0, len(batches))

    # 长视频批次状态：每批结果持久化，进程中断后可从 success 批继续。
    from pipeline.pipeline_manifest import (
        create_manifest, file_sha256, load_manifest, save_manifest, set_batch,
        partial_batch_results, successful_batch_results,
        summarize_manifest_metrics, validate_resume, ThrottledManifestWriter,
    )
    manifest = None
    manifest_lock = threading.RLock()
    if resume and not fresh and os.path.exists(manifest_path):
        try:
            manifest = load_manifest(manifest_path)
            validate_resume(manifest, input_path, manifest_options)
            saved_layout = {
                str(batch_id): [int(item) for item in batch.get("ids", [])]
                for batch_id, batch in (manifest.get("batches") or {}).items()
            }
            current_layout = {
                str(batch_id): [sub.id for sub in batch]
                for batch_id, batch in enumerate(batches)
            }
            if saved_layout != current_layout:
                raise ValueError("字幕预处理或分批结果已变化")
            log.info("  从 manifest 恢复: %s", os.path.basename(manifest_path))
        except (ValueError, OSError) as error:
            try:
                old_manifest = manifest or load_manifest(manifest_path)
                backup = manifest_path + ".before-rebase.bak"
                if not os.path.exists(backup):
                    shutil.copy2(manifest_path, backup)
                manifest, migrated = _rebase_round_one_manifest(
                    old_manifest,
                    input_path,
                    manifest_options,
                    translatable,
                    batches,
                )
                save_manifest(manifest_path, manifest)
                log.info(
                    "  Round 1 manifest 已安全重建，复用 %d 条未变化译文",
                    migrated,
                )
            except (ValueError, OSError):
                log.error("  无法恢复 manifest；请关闭续传后重新开始")
                return 1
    elif not dry_run:
        entries = [
            {"key": f"{sub.id}|{sub.start}|{sub.end}", "id": sub.id,
             "start": sub.start, "end": sub.end, "original": sub.text}
            for sub in translatable
        ]
        manifest = create_manifest(input_path, manifest_options, entries)
        save_manifest(manifest_path, manifest)
        log.info("  manifest: %s", os.path.basename(manifest_path))

    # S-2：长任务 manifest 节流写。每批整份重写是 O(n^2)（实测 2000 条
    # 63 批合计 1229ms 且全程持锁），改为批量合并落盘；失败/取消仍立即写。
    manifest_writer = (
        ThrottledManifestWriter(
            manifest_path, manifest, lock=manifest_lock,
            flush_every=Config.MANIFEST_FLUSH_EVERY,
            flush_interval=Config.MANIFEST_FLUSH_SECONDS,
        )
        if manifest else None
    )

    # ============================================================
    # Step 6: LLM 翻译 (并发)
    # ============================================================
    log.info("[6/8] LLM 翻译...")

    if dry_run:
        log.info("  试运行模式，跳过翻译")
        from pipeline.translate.llm import TranslateResult
        all_results = [
            TranslateResult(id=sub.id, original=sub.text, translated=f"[待翻译] {sub.text}")
            for sub in translatable
        ]
        if progress_callback:
            progress_callback(len(batches), len(batches))
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        translator = create_translator(
            api_key=api_key,
            model=model,
            temperature=Config.LLM_TEMPERATURE,
            max_tokens=Config.LLM_MAX_TOKENS,
            base_url=base_url,
            proxy=proxy,
        )
        preflight = getattr(translator, "preflight", None)
        requires_model = not manifest or any(
            successful_batch_results(manifest, str(batch_id)) is None
            for batch_id in range(len(batches))
        )
        if callable(preflight):
            if not requires_model:
                log.info("  所有批次均已从 manifest 恢复，跳过 API / 模型权限预检")
            elif not Config.LLM_PREFLIGHT_ENABLED:
                log.info("  LLM_PREFLIGHT=off：跳过预检，密钥/模型错误将由第一批暴露")
            else:
                log.info("  API / 模型权限预检...")
                preflight()
                if manifest is not None:
                    manifest["options"]["llm_disable_thinking"] = (
                        thinking_policy_report(translator)
                    )

        # 预计算每个 batch 的术语
        batch_data = []
        for batch in batches:
            batch_text = ' '.join(sub.text for sub in batch)
            batch_glossary = extract_glossary(batch_text)
            retrieval_diagnostics = getattr(
                prompt_builder, "few_shot_diagnostics", None,
            )
            batch_data.append({
                'batch': batch,
                'glossary': batch_glossary,
                'glossary_usage': _glossary_usage_metrics(
                    batch, batch_glossary, source_language,
                ),
                'few_shot_retrieval': (
                    retrieval_diagnostics(batch, translation_memory)
                    if callable(retrieval_diagnostics) else {
                        "language": source_language,
                        "candidate_count": 0,
                        "selected_count": 0,
                        "selected": [],
                        "observable": False,
                    }
                ),
            })

        all_results = [None] * len(batches)  # 保持原始顺序

        # 文档术语已经预扫描，所有并发批次从同一份只读锚点取值。
        _seen_terms = {
            english.lower(): (english, chinese)
            for english, chinese in full_glossary_map.items()
        } if term_anchor else {}

        def _result_payload(results):
            return [{
                "id": item.id, "original": item.original,
                "translated": item.translated, "tokens_in": item.tokens_in,
                "tokens_out": item.tokens_out, "cost": item.cost,
                "cached": item.cached,
            } for item in results]

        def _record_seen_terms(batch, results):
            # Kept as a compatibility hook; anchors are now immutable and
            # precomputed before workers start.
            return

        def _translate_one(idx: int, data: dict):
            raise_if_cancelled(cancel_check)
            batch = data['batch']
            glossary = data['glossary']
            glossary_usage = data['glossary_usage']
            t0 = time.time()

            # 构建当前批次已有锚定译名
            established = {}
            if term_anchor and _seen_terms:
                batch_text = ' '.join(sub.text for sub in batch)
                for eng_lower, (eng_cased, chs) in _seen_terms.items():
                    if _contains_glossary_term(
                        batch_text, eng_cased, source_language,
                    ):
                        established[eng_cased] = chs

            # 精校模式：构建 (原文, 参考译文) 配对
            refine_pairs_list = None
            if refine and refine_map:
                refine_pairs_list = []
                for sub in batch:
                    ref_text = refine_map.get(sub.id, "")
                    if ref_text:
                        refine_pairs_list.append({
                            'id': sub.id,
                            'en': sub.text,
                            'ref': ref_text,
                        })

            # 已成功 batch 从 manifest 直接恢复；只有未完成 batch 才调用 LLM。
            batch_id = str(idx)
            from pipeline.translate.llm import TranslateResult
            partial_by_id = {}
            if manifest:
                with manifest_lock:
                    saved = successful_batch_results(manifest, batch_id)
                    if saved:
                        restored = [TranslateResult(**item) for item in saved]
                        log.info(f"  批次 {idx+1}/{len(batches)} 从 manifest 恢复 ({len(restored)} 条)")
                        return idx, restored, None
                    partial = partial_batch_results(manifest, batch_id)
                    partial_by_id = {
                        item.id: item for item in
                        (TranslateResult(**saved_item) for saved_item in partial)
                    }
                    if partial_by_id:
                        log.info(
                            "  批次 %d/%d 恢复 %d 条，仅补译 %d 条",
                            idx + 1, len(batches), len(partial_by_id),
                            len(batch) - len(partial_by_id),
                        )
                    manifest_writer.set_batch(
                        batch_id, "running",
                        ids=[sub.id for sub in batch],
                        results=_result_payload(partial_by_id.values()),
                    )

            try:
                raise_if_cancelled(cancel_check)
                result_by_id = dict(partial_by_id)
                pending = [sub for sub in batch if sub.id not in result_by_id]
                api_attempts = 0
                batch_tokens_in = 0
                batch_tokens_out = 0
                batch_cost = 0.0
                # Q-3/S-3：语义校验失败原因落盘 + 批次 API 尝试预算
                validation_events: list = []
                transport_attempts_total = 0
                for attempt in range(3):
                    if not pending:
                        break
                    if attempt:
                        if transport_attempts_total >= Config.BATCH_API_ATTEMPT_BUDGET:
                            validation_events.append({
                                "attempt": api_attempts,
                                "kind": "attempt_budget_exhausted",
                                "detail": {
                                    "total_api_calls": transport_attempts_total,
                                    "budget": Config.BATCH_API_ATTEMPT_BUDGET,
                                    "skipped_repair_for": [
                                        sub.id for sub in pending
                                    ],
                                },
                            })
                            log.warning(
                                "  批次 %d/%d 已用 %d 次 API 调用"
                                "（预算 %d），停止补译",
                                idx + 1, len(batches),
                                transport_attempts_total,
                                Config.BATCH_API_ATTEMPT_BUDGET,
                            )
                            break
                        log.warning(
                            "  批次 %d/%d 第 %d 次补译缺失字幕: %s",
                            idx + 1, len(batches), attempt,
                            ", ".join(f"#{sub.id}" for sub in pending),
                        )
                    api_attempts += 1
                    new_results = translator.translate(
                        pending, glossary=glossary,
                        prompt_builder=prompt_builder,
                        full_glossary=None,
                        all_subs=(translatable
                                  if Config.ENABLE_CONTEXT and not refine else None),
                        established_terms=established if established else None,
                        refine_pairs=refine_pairs_list if refine_pairs_list else None,
                        global_context=prompt_global_context,
                        translation_memory=translation_memory,
                    )
                    _stats = (
                        translator.last_call_stats()
                        if hasattr(translator, "last_call_stats") else {}
                    )
                    transport_attempts_total += max(
                        1, int(_stats.get("transport_attempts") or 0)
                    )
                    batch_tokens_in += max(
                        (int(item.tokens_in) for item in new_results),
                        default=0,
                    )
                    batch_tokens_out += sum(
                        int(item.tokens_out) for item in new_results
                    )
                    batch_cost += sum(float(item.cost) for item in new_results)
                    pending_ids = {sub.id for sub in pending}
                    returned_ids = {
                        item.id for item in new_results
                        if item.id in pending_ids
                        and str(item.translated).strip()
                    }
                    missing_response_ids = pending_ids - returned_ids
                    if missing_response_ids:
                        validation_events.append({
                            "attempt": api_attempts,
                            "kind": "missing_ids",
                            "detail": {
                                "ids": sorted(missing_response_ids),
                                "api_calls_so_far": transport_attempts_total,
                            },
                        })
                        log.warning(
                            "  批次 %d/%d 响应缺少显式字幕 %s；"
                            "丢弃本次部分结果并重试整批，避免 ID 错位",
                            idx + 1, len(batches),
                            ", ".join(
                                f"#{subtitle_id}"
                                for subtitle_id in sorted(missing_response_ids)
                            ),
                        )
                        continue
                    source_by_id = {sub.id: sub.text for sub in pending}
                    for item in new_results:
                        if item.id in pending_ids and str(item.translated).strip():
                            has_residual, residual_words = (
                                check_english_residual(item.translated)
                                if source_language == "en" else (False, [])
                            )
                            if has_residual and attempt < 2:
                                validation_events.append({
                                    "attempt": api_attempts,
                                    "kind": "english_residual",
                                    "detail": {
                                        "id": item.id,
                                        "residual_words": list(residual_words),
                                    },
                                })
                                log.warning(
                                    "  字幕 #%d 仍有英文专名残留 %s，将单独补译",
                                    item.id,
                                    residual_words,
                                )
                                continue
                            required_terms = {
                                english: chinese
                                for english, chinese in glossary.items()
                                if _contains_glossary_term(
                                    source_by_id.get(item.id, ""), english,
                                    source_language,
                                )
                                and (
                                    hard_glossary_targets is None
                                    or chinese in hard_glossary_targets
                                )
                            }
                            missing_terms = [
                                f"{english}→{chinese}"
                                for english, chinese in required_terms.items()
                                if chinese not in item.translated
                            ]
                            if missing_terms and attempt < 2:
                                validation_events.append({
                                    "attempt": api_attempts,
                                    "kind": "missing_required_terms",
                                    "detail": {
                                        "id": item.id,
                                        "terms": missing_terms,
                                    },
                                })
                                log.warning(
                                    "  字幕 #%d 未使用必需官方译名 %s，将单独补译",
                                    item.id,
                                    ", ".join(missing_terms),
                                )
                                continue
                            result_by_id[item.id] = item
                    pending = [sub for sub in batch if sub.id not in result_by_id]

                if pending:
                    validation_events.append({
                        "attempt": api_attempts,
                        "kind": "untranslated_after_retries",
                        "detail": {
                            "ids": [sub.id for sub in pending],
                            "api_calls_total": transport_attempts_total,
                        },
                    })
                    raise ValueError(
                        "模型连续漏译字幕: "
                        + ", ".join(f"#{sub.id}" for sub in pending)
                    )
                results = [result_by_id[sub.id] for sub in batch]

                raise_if_cancelled(cancel_check)
                elapsed = time.time() - t0

                if manifest:
                    _success_data = {
                        "metrics": {
                            "tokens_in": batch_tokens_in,
                            "tokens_out": batch_tokens_out,
                            "cost": round(batch_cost, 8),
                            "elapsed_seconds": round(elapsed, 3),
                            "glossary_terms": len(glossary),
                            **glossary_usage,
                            "response_attempts": api_attempts,
                            "transport_attempts": transport_attempts_total,
                            "few_shot_retrieval": data[
                                "few_shot_retrieval"
                            ],
                        },
                    }
                    if validation_events:
                        # Q-3：补译过程的结构化原因（供前端与质量统计展示）
                        _success_data["validation_events"] = validation_events
                    manifest_writer.set_batch(
                        batch_id, "success",
                        ids=[sub.id for sub in batch],
                        results=_result_payload(results),
                        **_success_data,
                    )

                # 更新已见术语表（线程安全：_seen_terms 只在主线程 gather 时被读取）
                _record_seen_terms(batch, results)

                log.info(f"  批次 {idx+1}/{len(batches)} ({len(results)} 条) "
                         f"完成, 耗时 {elapsed:.1f}s, "
                         f"速率 {len(results)/max(elapsed,0.001):.0f} 条/s")
                return idx, results, None
            except PipelineCancelled:
                if manifest:
                    reusable = locals().get("result_by_id", partial_by_id)
                    _fail_extra = _failure_extra_fields(locals())
                    with manifest_lock:
                        manifest["status"] = "cancelled"
                        manifest_writer.set_batch(
                            batch_id, "pending",
                            ids=[sub.id for sub in batch], error="任务已取消",
                            error_code="cancelled",
                            results=_result_payload(reusable.values()),
                            metrics={
                                "tokens_in": locals().get("batch_tokens_in", 0),
                                "tokens_out": locals().get("batch_tokens_out", 0),
                                "cost": round(locals().get("batch_cost", 0.0), 8),
                                "elapsed_seconds": round(time.time() - t0, 3),
                                "glossary_terms": len(glossary),
                                **glossary_usage,
                                "response_attempts": locals().get("api_attempts", 0),
                                "transport_attempts": locals().get(
                                    "transport_attempts_total", 0
                                ),
                            },
                            **_fail_extra,
                        )
                        manifest["metrics"] = summarize_manifest_metrics(manifest)
                        manifest_writer.flush(force=True)
                raise
            except LLMAPIError as e:
                fields = e.public_fields()
                log.error(
                    "  批次 %d/%d API 调用终止: %s",
                    idx + 1, len(batches), fields["message"],
                )
                if manifest:
                    reusable = locals().get("result_by_id", partial_by_id)
                    _fail_extra = _failure_extra_fields(locals())
                    with manifest_lock:
                        manifest["status"] = "failed"
                        manifest_writer.set_batch(
                            batch_id, "failed",
                            ids=[sub.id for sub in batch],
                            error=fields["message"],
                            error_code=fields["error_code"],
                            upstream_status=fields["upstream_status"],
                            results=_result_payload(reusable.values()),
                            metrics={
                                "tokens_in": locals().get("batch_tokens_in", 0),
                                "tokens_out": locals().get("batch_tokens_out", 0),
                                "cost": round(locals().get("batch_cost", 0.0), 8),
                                "elapsed_seconds": round(time.time() - t0, 3),
                                "glossary_terms": len(glossary),
                                **glossary_usage,
                                "response_attempts": locals().get("api_attempts", 0),
                                "transport_attempts": locals().get(
                                    "transport_attempts_total", 0
                                ),
                            },
                            **_fail_extra,
                        )
                        manifest["metrics"] = summarize_manifest_metrics(manifest)
                        manifest_writer.flush(force=True)
                raise
            except Exception as e:
                fields = public_error_fields("translation_batch_error")
                log.error(
                    "  批次 %d/%d 翻译失败: %s（%s）",
                    idx + 1, len(batches), fields["message"], type(e).__name__,
                )
                if manifest:
                    reusable = locals().get("result_by_id", partial_by_id)
                    _fail_extra = _failure_extra_fields(locals())
                    manifest_writer.set_batch(
                        batch_id, "failed",
                        ids=[sub.id for sub in batch],
                        error=fields["message"],
                        error_code=fields["error_code"],
                        upstream_status=fields["upstream_status"],
                        results=_result_payload(reusable.values()),
                        metrics={
                            "tokens_in": locals().get("batch_tokens_in", 0),
                            "tokens_out": locals().get("batch_tokens_out", 0),
                            "cost": round(locals().get("batch_cost", 0.0), 8),
                            "elapsed_seconds": round(time.time() - t0, 3),
                            "glossary_terms": len(glossary),
                            **glossary_usage,
                            "response_attempts": locals().get("api_attempts", 0),
                            "transport_attempts": locals().get(
                                "transport_attempts_total", 0
                            ),
                        },
                        **_fail_extra,
                    )
                fallback = [TranslateResult(id=sub.id, original=sub.text, translated="")
                            for sub in batch]
                return idx, fallback, e

        max_workers = (
            min(Config.MAX_CONCURRENT, Config.STATEFUL_MAX_CONCURRENT)
            if (term_anchor or manifest)
            else Config.MAX_CONCURRENT
        )
        log.info("  实际并发批次: %d", max_workers)
        translation_started = time.time()

        completed_batches = 0
        batch_failures = []

        def record_batch(result):
            nonlocal completed_batches
            idx, results, error = result
            all_results[idx] = results
            if error is None:
                batch_metrics = (
                    manifest.get("batches", {}).get(str(idx), {}).get(
                        "metrics", {}
                    ) if manifest else {}
                )
                profiler.record_from_results(
                    results,
                    elapsed=float(batch_metrics.get("elapsed_seconds", 0.0)),
                )
            else:
                batch_failures.append(error)
                profiler.record(
                    subtitle_count=len(batch_data[idx]['batch']),
                    tokens_in=0, tokens_out=0, cost=0,
                )
            completed_batches += 1
            if progress_callback:
                progress_callback(completed_batches, len(batches))

        if max_workers == 1:
            # Stateful jobs are genuinely sequential. Do not pre-submit every
            # batch, otherwise a fatal 401 still drains the executor queue.
            for idx, data in enumerate(batch_data):
                raise_if_cancelled(cancel_check)
                record_batch(_translate_one(idx, data))
        else:
            pool = ThreadPoolExecutor(max_workers=max_workers)
            futures = {
                pool.submit(_translate_one, i, data): i
                for i, data in enumerate(batch_data)
            }
            try:
                for future in as_completed(futures):
                    raise_if_cancelled(cancel_check)
                    record_batch(future.result())
            except BaseException:
                for future in futures:
                    future.cancel()
                pool.shutdown(wait=True, cancel_futures=True)
                raise
            else:
                pool.shutdown(wait=True)

        # 批处理循环结束：把节流累积的最后几批落盘，
        # 避免验证/输出阶段崩溃时丢失最近的成功批次。
        if manifest_writer is not None:
            manifest_writer.flush(force=True)

        if batch_failures:
            if manifest:
                with manifest_lock:
                    manifest["status"] = "failed"
                    manifest["metrics"] = summarize_manifest_metrics(manifest)
                    save_manifest(manifest_path, manifest)
            log.error("翻译未完成：%d 个批次失败，不会写入最终 SRT", len(batch_failures))
            return 2

        # 展平结果列表。预处理规则可能在断点创建后升级（例如后来
        # 能识别裸 "music" 标记）；旧 manifest 中对应的历史结果仍
        # 可保留作审计，但不能进入当前结果校验。
        all_results = [r for batch_res in all_results for r in batch_res]
        active_ids = {sub.id for sub in translatable}
        stale_results = [result for result in all_results if result.id not in active_ids]
        if stale_results:
            log.info(
                "  忽略 manifest 中 %d 条现已跳过的历史结果: %s",
                len(stale_results),
                ", ".join(f"#{result.id}" for result in stale_results[:20]),
            )
            all_results = [
                result for result in all_results if result.id in active_ids
            ]

    raise_if_cancelled(cancel_check)
    # ============================================================
    # Step 7: 验证
    # ============================================================
    log.info("[7/8] 验证翻译结果...")

    validation_failed = False
    if Config.ENABLE_VALIDATION:
        try:
            report = validate(translatable, all_results, strict=True)
            log.info(f"  验证通过: {report}")
        except ValidationError:
            log.error("  验证失败：翻译结果未通过完整性检查")
            validation_failed = True
    else:
        log.info("  验证已禁用，跳过")

    if validation_failed:
        if manifest:
            manifest["status"] = "failed"
            save_manifest(manifest_path, manifest)
        log.error("翻译未完成：保留已完成批次日志，但不会写入最终 SRT")
        return 2

    # ============================================================
    # Step 8: 组装输出 + 写入 SRT
    # ============================================================
    log.info("[8/8] 生成输出 SRT...")

    # 构建 id → 翻译结果 映射
    result_map = {r.id: r for r in all_results}

    # 生成输出字幕列表
    output_subs = []
    for sub in subtitles:
        if sub.id in result_map:
            translated_text = result_map[sub.id].translated
        else:
            if not preserve_skipped:
                continue
            # 音乐行/空行：兼容单文件模式，保留原文
            translated_text = sub.text

        # Final defence: a model can reintroduce a translated cue even when
        # the source annotation was removed before translation.
        translated_text = clean_music(translated_text)
        if not translated_text.strip():
            if not preserve_skipped:
                continue
            translated_text = sub.text

        output_subs.append(Subtitle(
            id=sub.id,
            start=sub.start,
            end=sub.end,
            text=translated_text,
        ))

    # 写入
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    raise_if_cancelled(cancel_check)
    save_srt(output_path, output_subs)
    log.info("  输出: %s (%d 条)", os.path.basename(output_path), len(output_subs))
    if manifest:
        with manifest_lock:
            manifest["status"] = "completed"
            manifest.setdefault("artifacts", {})["output"] = {
                "path": os.path.basename(output_path),
                "sha256": file_sha256(output_path),
            }
            manifest["metrics"] = {
                **summarize_manifest_metrics(manifest),
                "parallelism": max_workers,
                "wall_seconds": round(time.time() - translation_started, 3),
            }
            save_manifest(manifest_path, manifest)

    # ============================================================
    # 性能报告
    # ============================================================
    if Config.ENABLE_PROFILER:
        log.info("")
        stdout_encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        report = profiler.report().encode(
            stdout_encoding, errors="backslashreplace"
        ).decode(stdout_encoding)
        print(report)

    # ============================================================
    # 完成
    # ============================================================
    log.info("✅ 翻译完成!")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="YouTube 英文 SRT 字幕自动翻译管道 — 鸣潮专用"
    )
    parser.add_argument("input", help="输入 SRT 文件路径")
    parser.add_argument("-o", "--output", default=None,
                        help="输出 SRT 文件路径 (默认: output/<文件名>.zh.srt)")
    parser.add_argument("--model", default=None, help="模型名称")
    parser.add_argument("--api-key", default=None, help="API 密钥")
    parser.add_argument("--proxy", default=None,
                        help="模型请求代理，例如 http://127.0.0.1:7890")
    parser.add_argument("--batch-size", type=int, default=None,
                        help=f"每批字幕条数 (默认: {Config.BATCH_SIZE})")
    parser.add_argument("--game", default="wuwa", help="游戏名称 (默认: wuwa)")
    parser.add_argument(
        "--source-language", choices=("en", "ja", "ko"), default="en",
        help="Source subtitle language (default: en)",
    )
    parser.add_argument(
        "--target-language", choices=("zh-CN",), default="zh-CN",
        help="Target subtitle language (currently fixed to zh-CN)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="试运行模式：跳过 LLM 调用，用占位文本填充")
    parser.add_argument("--no-alias", action="store_true",
                        help="禁用 Alias 修正")
    parser.add_argument("--no-jp-filter", action="store_true",
                        help="禁用日文过滤")
    parser.add_argument("--no-music", action="store_true",
                        help="禁用音乐检测")
    parser.add_argument("--no-validate", action="store_true",
                        help="禁用翻译验证")
    parser.add_argument("--no-profile", action="store_true",
                        help="禁用性能统计")
    parser.add_argument("--no-dedupe", action="store_true",
                        help="禁用去重（Whisper生成的字幕无需去重）")
    parser.add_argument("--no-term-anchor", action="store_true",
                        help="禁用跨批次术语锚定（默认开启）")
    parser.add_argument("--refine", action="store_true",
                        help="二阶段精校模式：以已有中文翻译为参考，重新翻译提高质量")
    parser.add_argument("--refine-input", default=None,
                        help="精校模式的参考译文路径（默认自动从output路径推导）")
    parser.add_argument("--manifest", default=None,
                        help="长视频批处理状态文件路径（默认: 输出文件.manifest.json）")
    parser.add_argument("--resume", action="store_true",
                        help="从 manifest 恢复已成功批次，避免重复调用 LLM")
    parser.add_argument("--fresh", action="store_true",
                        help="忽略已有 manifest，从头创建新的批处理状态")

    args = parser.parse_args()

    # 应用开关
    if args.no_alias:
        Config.ENABLE_ALIAS = False
    if args.no_jp_filter:
        Config.ENABLE_JAPANESE_FILTER = False
    if args.no_music:
        Config.ENABLE_MUSIC_DETECTION = False
    if args.no_validate:
        Config.ENABLE_VALIDATION = False
    if args.no_profile:
        Config.ENABLE_PROFILER = False
    if args.no_dedupe:
        Config.ENABLE_DEDUPE = False
    if args.no_term_anchor:
        Config.ENABLE_TERM_ANCHOR = False

    sys.exit(run_pipeline(
        input_path=args.input,
        output_path=args.output,
        model=args.model,
        api_key=args.api_key,
        proxy=args.proxy,
        batch_size=args.batch_size,
        game=args.game,
        dry_run=args.dry_run,
        term_anchor=Config.ENABLE_TERM_ANCHOR and not args.no_term_anchor,
        refine=args.refine,
        refine_input=args.refine_input,
        manifest_path=args.manifest,
        resume=args.resume,
        fresh=args.fresh,
        source_language=args.source_language,
        target_language=args.target_language,
    ))


if __name__ == "__main__":
    main()
