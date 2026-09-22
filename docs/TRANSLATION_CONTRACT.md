# TRANSLATION_CONTRACT — 翻译行为契约

> 更新：2026-08-23。这是所有语言翻译行为必须遵守的契约。修改任何翻译逻辑（prompt/仲裁/Reviewer/重建/实体保护）前必须阅读并保持本契约不变量。

## 1. 价值排序（不可违反）

```text
正确语义 > 正确专名 > 不编造 > 上下文正确 > Reaction 自然度 > 中文可读性 > 自动覆盖率
```

- 正确 unresolved > 自信地翻错
- 不知道，比编错更好
- 专名错误 = 严重错误
- Source Truth 的问题必须在翻译之前解决

## 2. 六条核心原则

1. **Source Truth first** — 主原文是翻译依据；辅助证据只用于纠正听写/人名/漏词/数字，不得拼接两路证据。
2. **Canonical first** — 仲裁后的 canonical source 是 source of truth；Reviewer 不得因看到 raw ASR 而重新选源。
3. **Terminology locked** — 专名优先级：已锁定译名 > 本批术语 > ASR 易错词；无官方译名的人名不得擅自创建永久音译。
4. **Entity hallucination forbidden** — 不得凭空创造 glossary/lore 之外的新专名；疑似未确认专名 → 保守处理 + 标 risk。
5. **Context is advisory** — 相邻上下文只用于理解，绝不翻进当前 ID；不得凭空增加原文没有的信息。
6. **Reviewer cannot reselect source** — Reviewer 的 canonical English/source 是权威；replace 必须指出 canonical 中哪部分被 Round1 错译。

## 3. 通用硬规则（三语）

- ID/顺序/数量严格一致；不合并、不增删、不重排。
- 数字、版本号、网址、C6/S6 等保持原样；行末不加句号。
- 纯音乐/SFX 行（[Music]、♪、[음악]、[音楽] 等）输出空文本；有语义的反应词必须翻译。
- 脏话改非直白网络表达；禁止"他妈的/操/牛逼/装逼"；侮辱词一律"混蛋"。
- 除通用缩写（DPS/HP/BOSS）和下述“上下文品牌”外，不残留源语言字符。
- “上下文品牌”只允许任务内保留：必须是赞助/产品提示附近的多词专名，且完整短语证据强于嵌套普通术语；保留后必须进入人工复核，不得自动写入全局术语库、TM 或标为人工确认。
- 禁止解释性扩写。

## 4. 分语言契约

### 韩语（ko → zh-CN）
- 语义单元输入（非滚动碎 cue）；canonical 已仲裁；高风险句已重建（cap 25）
- 韩语省略主语 → 证据不足不得臆造"他/她"；保留否定/条件/数字/关系/态度
- 剧情背景已注入（拉海洛篇：隧者/爱弥斯/达妮娅/绯雪/罗伊族）
- 已验证专名（不得回归）：爱弥斯/绯雪/隧者/隧门/虚质磁暴/拉海洛/达妮娅/罗伊族符文
- 验收：hallucinated entity 0 / source residual 0 / timeline overlap 0

### 日语（ja → zh-CN）
- 同韩语链路；敬语/称呼自然处理；省略主语不臆造
- 已跑通真实任务（608178087aa1）；Round2 状态用 round2_status 跟踪

### 英语（en → zh-CN）— **English V2 契约**
- **语义单元输入**（滚动字幕先修复再聚合再翻译，禁止碎 cue 逐条翻译）
- **canonical 已归一化**：ASR 纠错 + Entity Normalizer 已处理（Jingran→景燃、Qingxiao→清宵 类）；翻译模型不得根据 raw ASR 把 canonical 改回其他名字
- **混合字幕分类**：already_zh（原样保留，不送 LLM）/ english_reaction（翻译）/ music_sfx（空）/ mixed / unknown
- **未知专名保守**：疑似未确认英文专名 → 保持语义保守或标 risk，禁止随意音译（jin Gran → 不得输出"金格兰"）
- **多词上下文优先**：赞助/产品语境中的完整品牌短语优先于其内部单词级普通术语（如品牌 `Buff Buff` 不得被 `Buff→增益` 拆解）；可原样保留并标 task-local manual risk，禁止自动升格为官方译名
- **Reaction 风格**：按情绪自然分布（哇/卧槽/太帅了/这也太离谱了/真的假的/不会吧/绝了），保持 source intensity；可以"太超模了/太逆天了"，但不得自己增加不存在的剧情
- **拉丁缩写保留**：DPS/HP/BOSS/C6/S6/版本号/URL
- **few-shot**：从 approved English memory 动态选 3-8 条最相关（en translation_memory）

## 5. Reviewer 契约

- 输入：canonical source + Round1 译文 + 上下文 + 术语
- 输出：keep（默认，KEEP anchor 最小修正）或 replace（必须给出 reason，指出 canonical 中哪部分被错译）
- 检查项：omission / addition / negation / question / number / proper noun / terminology / subject-object / reaction intensity / literal-Chinese stiffness / hallucinated entity
- 门禁：置信度/残留/术语/数字/过度改写/幻觉防护确定性检查
- 不重新翻译整句；replace 必须证据驱动

## 6. 验收标准（本 EPIC）

- 景燃不再错（jin Gran/jin geon/jing ran → Jingran → 景燃）
- 清宵不再错（Qing Xiao/Qing Qing Xiao → Qingxiao → 清宵）
- 陌生专名不自由音译（possible_entity → review_required）
- 官方中文不重新翻译（already_zh bypass）
- 滚动字幕不串译（语义单元聚合）
- Reaction 自然（风格多样化 + 强度保持）
- ko regression PASS / ja regression PASS / English V2 PASS/FAIL 明确判定
- 交付：overlap 0 / source residual 0 / blocking error 0
