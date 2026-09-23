# DATA_CONTRACT — 数据职责与来源真相

> 历史私有数据目录记录，更新于 2026-08-24。下列数量、`benchmarks/`、`reports/`、`download/` 和验收产物属于当时私有环境，不能当作公开仓库自带文件或当前数量。公开快照的数据边界见 [PUBLICATION](../PUBLICATION.md) 与 [公开版安全与隐私状态](PUBLIC_SECURITY_PRIVACY_STATUS.md)。职责边界和禁止自动晋升的原则仍供维护参考，实际实现以当前源码为准。

## 公开快照的数据边界

| 路径 | 公开版情况 |
|---|---|
| `data/glossary.db` | 经脱敏的术语副本；不等于原私有数据库的数量或内容 |
| `data/translation_memory.json` | 空结构；不含原用户的翻译记忆 |
| `data/prompts/` 等程序数据 | 经白名单选取的运行所需文件 |
| `benchmarks/`、`reports/`、`download/` | 不随公开源码和基础便携包分发；运行后生成的任务文件留在用户本机 |


## 1. 数据职责表

| 数据文件 | 职责 | 写入者 | 数量（2026-08-21） |
|---|---|---|---|
| `data/glossary.db` | 官方/确认术语（三种源语言→中文） | 人工/证据裁决工具 | 682 条（ko 224 / ja 156 / en 302） |
| `data/asr_corrections.json` | ja/ko source normalization（ASR 听错纠错） | 人工/审计 | 56 条；本轮未自动新增 |
| `data/asr_corrections_en.json` | en source normalization | 人工/审计 | 26 条；本轮未自动新增 |
| `data/translation_memory.json` | approved few-shot（防污染：人工改译默认只归档） | 人工确认 | 81 条（approved 80：ko 23 + ja 7 + en 50；unapproved en 1） |
| `data/prompts/*.txt` | 行为模板（ko/ja：剧情背景+脏话+重建模板） | 开发者 | 4 个（ko/ja 翻译 + 重建 x2） |
| `data/prompt_template.txt` | 英语行为模板（UTF-8） | 开发者 | 1 个 |
| `data/alias.json` | 别名映射 | 开发者 | — |
| `data/tricky_terms.json` | ASR 易错词提示 | 开发者 | — |
| `data/ja_person_terms.json` / `ja_place_terms.json` / `ko_person_terms.json` | 人物/地名表（仲裁用） | 开发者 | — |
| `data/ww_ko_zh_terms.json` | ko-zh 术语表 | 导入 | — |
| `data/music.txt` | 音乐行识别 | 开发者 | — |
| `docs/WUWA_LORE_GUIDE.md` | lore/参考（剧情时间线 + 53 角色四语对照） | 人工调研 | 1 个 |
| `benchmarks/{en,ja,ko}_reaction_benchmark.json` | 三语回归真相 | 金标构建/真人审核 | en 360 / ja 236 / ko 303；confirmed 各 10 |
| `benchmarks/confirmed_ids_{en,ja,ko}.json` | 固定 A/B/泄漏审计样本 | 工具 | 每语种 10 个 confirmed ID |
| `benchmarks/final9-audit.json` | final9 审计快照 | 工具 | — |
| `download/<job_id>/` | 任务产物（job.json/过程文件/final.srt） | 运行时 | 15 现役 + archive |
| `output/acceptance/` | 历史验收成品（final6~final9） | 运行时 | — |

## 2. 冲突处理优先级（从高到低）

```text
用户确认 / 官方数据
>
项目已确认术语（glossary.db）
>
历史 translation memory（approved）
>
ASR guess（asr_corrections 推断）
>
LLM guess
```

**不允许多个来源互相冲突**。发现同一 source term → 多个中文时：
1. 记录到 `docs/TERM_CONFLICT_AUDIT.md`
2. 不自动覆盖
3. 按上述优先级裁决；无法裁决 → unresolved 人工

## 3. 禁止事项

- 重建 glossary.db（用户数据，丢失不可恢复）
- 覆盖 translation_memory（除非人工明确批准）
- 删除 gold benchmark / 历史验收成品
- 把 asr_corrections 与 glossary 职责混用（ASR 纠错 ≠ 术语翻译）

## 4. English V2 数据规划（本 EPIC）

| 数据 | 计划 |
|---|---|
| `data/asr_corrections_en.json` | 新建（或泛化 schema 为 `{"en":{}, "ja":{}, "ko":{}}`，以兼容性为先，不做无必要 migration） |
| English Entity Normalizer 数据 | 复用 glossary.db（en 302 条）+ lore 角色名 + 本地上下文 |
| `data/translation_memory.json` en 段 | 50 条 approved；新增仍必须来自显式真人确认，不接受 provisional/source-damaged/unresolved 自动写入 |
| `benchmarks/en_reaction_benchmark.json` | 360 条真实样本；10 confirmed、124 provisional、226 无 Gold 状态 |

## 5. 任务内上下文实体

- `risk_queue.json` 的可选 `contextual_entities` 只记录当前字幕中由赞助/产品语境支持的完整多词表面形式。
- `contextual_entity_preserved` / `contextual_entity_unverified` 都是人工复核状态，不是官方译名、Gold、approved TM 或人工确认。
- 该字段不得反向写入 `data/glossary.db`、`data/translation_memory.json` 或 benchmark；确认后的长期译名仍须走人工/官方证据裁决。

## 6. 真实任务人工审核批次与指标

| 数据 | 职责 | 状态与边界 |
|---|---|---|
| `reports/realflow_*/benchmark_candidates_*_v2.json` | 从真实任务保存 Round 1、Round 2、是否改写及 Round 2 决策证据 | 本机完整字幕；Git 忽略；始终 provisional/unreviewed |
| `reports/realflow_*/human_review_batch_*.{json,csv}` | 已退役的离线审核工具兼容格式 | 禁止由 Web/API 读取或写入；仍保持 Git 忽略，防止旧文件误提交 |
| `reports/HUMAN_REVIEW_BATCH_READINESS_*.md` | 不含全文的抽样数量、分层和泄漏检查摘要 | 可提交；不代表质量结论 |
| `reports/HUMAN_REVIEW_METRICS_*.{json,md}` | 只汇总通过真人身份、证据和时间门禁的标签 | 无真实标签时准确率与有效修复率必须为 `null/not_observable` |

- `tools/prepare_review_batch.py` 会再次排除现有 benchmark、approved language-specific TM 和批内规范化重复；输出仍明确标为 machine-generated/unreviewed。
- `tools/summarize_review_batch.py` 不推断空白判断，不接受 AI/Codex/模型/占位 reviewer，不接受与两版译文不一致的 `machine_changed`；`both_wrong` 和 `source_error` 还必须补齐各自证据字段。
- 浏览器真人审核面已于 2026-08-25 因隐私风险退役：`/review/english` 与 `/api/review-batch/english*` 必须保持 404，应用不得收集 reviewer 姓名、证据声明或逐条判断。离线兼容工具不得被接回 Web。
- Web 保存要求真实姓名、显式视频上下文确认和受支持判断；没有可链接的 YouTube `video_id` 时拒绝保存证据声明。时间由本机自动写入带时区 ISO-8601，CSV 原子替换，旧决策写入追加式 audit 后才生成本机 live metrics。
- 审核完成和指标生成都不会自动升级 Gold，也不会写入 `data/glossary.db` 或 `data/translation_memory.json`。任何后续晋升仍须使用既有人工导入门禁并由用户明确执行。
