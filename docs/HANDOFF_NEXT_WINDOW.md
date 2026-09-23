# Public snapshot handoff

Public source snapshot; private development handoffs and task histories are not distributed. See PUBLICATION.md.

语言契约：en → zh-CN、ja → zh-CN、ko → zh-CN。测试包含 dry-run 编排验证，不代表真实模型或下载验收。

## Local compatibility fixes — 2026-09-23

This revision fixes cross-volume direct uploads and legacy-encoded CLI report output. Both have deterministic regressions; isolated public validation: 1123 passed, 6 skipped. See KNOWN-ISSUES.md for scope and test limits. Private task data and historical handoffs are excluded.

## Translation workbench UX — 2026-09-23

The current public worktree adds three user-approved changes only. Completed tasks show the first eight Chinese subtitle cues and the existing gated download action near the top of Overview. The default review list now contains only unresolved `review_required` items; other states remain available via the filter, including a functioning “全部风险” selection. API Key entry offers session-only or Windows Credential Manager storage; existing API clients retain their prior secure-storage default. Session keys stay in process memory, are not placed in settings JSON or job files, and a previously saved secure key remains until the user explicitly forgets it. Translation prompts, dual-source requirements, Round 1/2, risk classification, and delivery gates are unchanged. Verification in a fresh clone with an independent `.git`, empty `PYTHONPATH`, and basetemp inside the clone: 1128 passed, 6 skipped; 28 Node frontend tests passed; JS syntax checks passed. No real model call was made for this UI change.
