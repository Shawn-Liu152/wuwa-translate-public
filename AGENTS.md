# 项目维护规则

> 项目：`<PROJECT_ROOT>`
> 阅读入口：先读 `PUBLICATION.md`（公开版边界）→ `docs/TRANSLATION_CONTRACT.md`（翻译契约）→ `docs/WUWA_LORE_GUIDE.md`（剧情/专名参考）→ `docs/DATA_CONTRACT.md`（数据职责）。

## 工程规则（必须遵守）

1. **所有改动必须同步写入交接文档**：`docs/HANDOFF_NEXT_WINDOW.md` 只保留当前事实、验证结果和剩余风险；超过 50 KB 或出现过期结论时，先完整归档到 `docs/archive/`，再更新当前交接。不要无限追加互相冲突的历史段落。没有同步交接文档时，不视为完成。
2. **问题修复必须同步 FAQ**：面向用户的报错/异常/操作流程调整，必须更新 `web/index.html` 的"常见问题"（现象/原因/解决步骤），并在 `tests/test_web_api.py` 增加/更新回归断言。
3. **不要覆盖用户未提交修改**：working tree 有用户改动时先分析、分离，必要时 checkpoint/新分支。
4. **删除文件前必须证明**：0 运行时引用 + 0 测试引用 + 0 文档契约引用 + 非数据 + 非历史验收证据（见 `docs/DEAD_FILE_AUDIT.md`）；未知文件先归档不删除。
5. **不破坏**：web API 的 `download/<job_id>` 假设、resume/manifest、ko/ja 已验收主链、用户术语数据。

## 关键测试命令

```bash
# pytest（Git Bash，必须 unset PYTHONPATH；用项目 .venv，testenv 已失效）
unset PYTHONPATH && .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=/tmp/pytest-$(date +%s)

# JS 语法
node --check web/static/app.js && node --check web/static/pet.js && node --check web/static/workbench-core.js
```

## 数据安全（永不违反）

- `data/glossary.db`、`data/translation_memory.json`、真实素材、人工审核结果、gold benchmark 一律不删。
- `.env`、`youtube_cookies.txt`、API Key 严禁提交。
- 不重建 glossary.db、不覆盖用户术语。

## Git 原则

- 逻辑提交，禁止一个超级 commit；每个主要 commit 前跑相关测试。
- 不 force push；不删除未知分支；用户未提交改动先分离。
