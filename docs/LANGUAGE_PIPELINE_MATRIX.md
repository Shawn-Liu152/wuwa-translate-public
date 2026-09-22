# LANGUAGE_PIPELINE_MATRIX — 三语真实流水线（以当前代码为准）

> 更新：2026-08-10
> 依据：`pipeline/long_video.py`（run_long_video 主流程）、`pipeline/main.py`（run_pipeline）、
> `pipeline/preprocess/*`、`pipeline/translate/*`、`pipeline/risk_queue.py`、`pipeline/display_cues.py`
> HEAD: 100b2b9（master）
> 本文件描述**当前代码实际执行**的流程，旧文档描述不再作为依据。

## 1. 总览

所有语言共用主流程 `run_long_video()`（pipeline/long_video.py:2144）：

```
输入 SRT(主源) [+ 次源] → _write_union → [ja/ko: 仲裁+重建] → [en: cue 分类+实体归一]
→ Round1 翻译(run_pipeline) → [en: 分类合并] → risk queue → Round2 Reviewer → display-map 回投 → final
```

| Stage | EN | JA | KO | 实现位置 |
| ----- | -- | -- | -- | ---- |
| raw source | YES (YouTube en + Whisper) | YES (YouTube ja 单源) | YES (YouTube ko 单源) | job.json artifacts |
| cleanup | PARTIAL (clean_export_subtitles 导出清理) | YES (clean_japanese_source) | YES (clean_japanese_source 同函数) | pipeline/main.py:415-424; dedupe.py:365 |
| rolling repair | YES | YES | YES | dedupe.py:repair_rolling_overlaps (42) |
| semantic units | YES | YES | YES | dedupe.py:coalesce_semantic_cues (258) |
| Whisper evidence | YES (reference.whisper.en.srt) | NO (empty secondary) | NO (empty secondary) | _write_union |
| alignment | PARTIAL (youtube.aligned.en.srt 存在但 union 未用) | NO | NO | — |
| ASR normalization | PARTIAL (asr_corrections_en.json 仅 3 条) | YES (asr_corrections.json ja) | YES (asr_corrections.json 28 条 ko) | preprocess/asr_normalize.py; _write_union |
| arbitration | NO (by design, en 单源语义) | YES | YES | preprocess/source_arbitration.py:arbitrate_candidates (484) |
| canonical | PARTIAL (union 文本 + en_entity_normalize) | YES | YES | _write_union; en_entity_normalize.py |
| reconstruction | NO | YES | NO (本 Job 无 trace) | preprocess/source_reconstruction.py; long_video.py:2184-2242 |
| Entity Guard | PARTIAL (Entity Guard Lite) | YES (entity_resolution) | YES | long_video.py:_merge_english_entity_guard_risks (867); entity_resolution.py |
| glossary | YES (dynamic relevant) | YES | YES | prompt_builder._format_glossary; long_video._relevant_glossary |
| lore/context | YES (en prompt 剧情背景) | YES (ja prompt) | YES (ko prompt) | data/prompts/*-zh-CN.txt |
| few-shot | **NO（approved=0）** | PARTIAL (7 条, 空格分词匹配对日语失效) | YES (23 条) | translation_memory.json; prompt_builder:_format_few_shot_examples |
| Round1 | YES (deepseek-v4-flash) | YES | YES | main.py:run_pipeline |
| Reviewer | YES (T7-D English Reviewer) | YES | YES | long_video.py:_round_two (1652) |
| risk queue | YES | YES | YES | risk_queue.py:build_risk_queue |
| display-map | YES | YES | YES | display_cues.py:redistribute_subtitles; _publish_final |
| final publish | YES | YES | YES | long_video.py:_publish_final (308) |

## 2. EN 实际链路（English Translation V2）

```
lWgwc_xNzrg.en.srt (YouTube 343)
  + reference.whisper.en.srt (Whisper 268)
→ _write_union (long_video.py:331)
    repair_rolling_overlaps → asr_normalize(3条) → dedupe_youtube_subs → coalesce_semantic_cues
    → build_source_union → union (226)
    → normalize_en_entities (en_entity_normalize.py:209) — 高置信自动替换 + 低置信 advisory
→ _prepare_en_translation_input (long_video.py:450) — cue_classifier 分 5 类，只翻 ENGLISH_REACTION
→ run_pipeline (main.py:254) — alias fix (main.py:404) → japanese filter (en) → Round1 翻译
→ _merge_en_classified_round1 — already_zh/music 原样回填
→ build_risk_queue → _merge_cue_classification_risks (mixed/unknown → manual)
→ _round_two (English Reviewer, canonical=source of truth, keep 锚定)
→ _publish_final → display-map 回投 → final.zh.srt
```

EN 特有：
- cue_classifier.py:classify_cue — 静态分类（CJK 比例阈值），无 LLM
- en_entity_normalize.py — 4 层恢复：evidence-alias(3条) → glossary 精确/大小写/标点 → affix 剥离 → similarity advisory
- 无仲裁、无重建（by design）

## 3. JA 实际链路

```
gl8vjOL1xWI.ja-orig.srt (YouTube 1145)
  + reference.secondary.empty.ja.srt (空次源 → 单源)
→ _write_union → 滚动修复 → ASR 归一(ja) → dedupe → coalesce → union (171)
→ arbitrate_candidates (仲裁: single_primary, canonical=YouTube) → arbitration.json (171 units)
→ reconstruct_sources (高风险重建 cap 25) → reconstruction.json
→ run_pipeline Round1 (deepseek-v4-flash, prompt=prompts/ja-zh-CN.txt)
→ build_risk_queue (103 items)
→ _round_two Reviewer (ja 规则: 否定/疑问/人名逐条确认, canonical 锚定)
→ display-map → final.zh.srt (282 条切分)
```

## 4. KO 实际链路

```
p0bHZydkDrA.ko-orig.srt (YouTube 877)
  + reference.secondary.empty.ko.srt (空次源 → 单源)
→ _write_union → 滚动修复 → ASR 归一(28条 ko) → dedupe → coalesce → union (143)
→ arbitrate_candidates → arbitration.json (143 units)
→ (无 reconstruction trace 文件 → 本 Job 无重建)
→ run_pipeline Round1 (deepseek-v4-flash, prompt=prompts/ko-zh-CN.txt)
→ build_risk_queue (148 items)
→ _round_two Reviewer (round2_results: 144 deferred_weak_risk + 4 keep)
→ display-map → final.zh.srt (217 条切分)
```

## 5. 共享层关键实现索引

| 功能 | 位置 |
| ---- | ---- |
| union 构建（滚动修复→归一→语义合并） | long_video.py:_write_union (331) |
| 滚动重叠修复 | dedupe.py:repair_rolling_overlaps (42) |
| 语义单元聚合 | dedupe.py:coalesce_semantic_cues (258) |
| YouTube 滚动去重 | dedupe.py:dedupe_youtube_subs (435) |
| 微 cue 合并 | dedupe.py:coalesce_micro_cues (103) |
| ASR 归一化 | preprocess/asr_normalize.py |
| EN 实体归一 | preprocess/en_entity_normalize.py:normalize_en_entities (209) |
| EN cue 分类 | preprocess/cue_classifier.py:classify_cue (36) |
| ja/ko 仲裁 | preprocess/source_arbitration.py:arbitrate_candidates (484) |
| ja/ko 重建 | preprocess/source_reconstruction.py:reconstruct_sources |
| 风险队列 | risk_queue.py:build_risk_queue (281) |
| Round2 Reviewer | long_video.py:_round_two (1652), _evaluate_round_two_decision (975) |
| display 回投 | display_cues.py:redistribute_subtitles (175), redistribute_translation (107) |
| prompt 构建 | translate/prompt_builder.py:PromptBuilder.build (78) |
| prompt 模板加载 | prompt_builder.py:load_prompt_builder (405) — prompts/<lang>-zh-CN.txt 优先，fallback prompt_template.txt |
| LLM 客户端 | translate/llm.py:OpenAICompatibleTranslator |

## 6. 已知缺口（代码级证据）

1. **EN few-shot = 0**：translation_memory.json 中 en approved 0 条（仅 1 条未审核）→ Round1 无英语示例。
2. **EN ASR corrections 仅 3 条**（Jingrana/Chingxiao/Gingran）→ `Froolova`(Phrolova 弗洛洛) 等变体不受保护；
   alias.json 180 条在 main.py:404 的 AliasFixer 中执行，但 **alias 表不含 jin Gran/jin geon 等变体**。
3. **similarity advisory 不自动替换**：en_entity_normalize._similarity_candidates 只返回 advisory，
   不会 canonicalize；medium 置信不落入翻译输入。
4. **JA/KO few-shot 检索对日语失效**：_format_few_shot_examples 用空格分词做 token 重叠，
   日语无空格 → 7 条 ja 样本几乎永不命中。
5. **JA round1 产物缺失**：df93e43d0c47 无 final.round1.zh.srt（manifest 指向根目录路径但文件不在）→
   中间态不可审计（NOT OBSERVABLE）。
6. **显示层回投**：redistribute_translation 的 desired 计算有 300ms 合并与虚词边界保护，但仍需
   逐语义单元验证 previous/next-line bleed（见三语审计）。
7. **reviewer keep 对 unresolved 强制回滚**：_evaluate_round_two_decision 对 keep + auto-replace
   原因 → applied=round1，可能推翻正确的 keep（需审计统计 harmful）。

## 7. 数据现状（2026-08-10 实测）

| 数据 | 值 |
| ---- | -- |
| glossary.db | 664 条（en 289 / ja 152 / ko 223），表 glossary(english, chinese, game, category, source_language, source_term, target_language, target_term) |
| asr_corrections.json | ko/ja 28 条（主要 ko） |
| asr_corrections_en.json | 3 条 |
| alias.json | 180 条（EN 人名变体，含 Jing ran→Jingran, Qing Xiao→Qingxiao） |
| translation_memory.json | 31 条（ko 23 / ja 7 / en 1 未批准）→ approved 30 |
| benchmarks | ko_reaction_benchmark.json 193 条（51 gold_zh）；EN/JA benchmark 不存在 |
| prompts | data/prompts/{en,ja,ko}-zh-CN.txt + source-reconstruct-{ja,ko}.txt |
| model | deepseek-v4-flash（用户钦定不换）；base_url=opencode.ai/zen/go/v1（Job options 确认） |
