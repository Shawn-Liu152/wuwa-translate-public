# 公开版安全与隐私状态

> 截至 2026-09-23 的范围说明，不是完整安全审计报告，也不保证不存在未知漏洞。当前源码版本为 `0.2.0-beta.6`；公开可下载版本应以 GitHub Releases 中已核实的标签、资产和校验文件为准。

Beta.6 的 [公开 Release](https://github.com/Shawn-Liu152/wuwa-translate-public/releases/tag/v0.2.0-beta.6) 已核对：标签指向 `17f5c08b083b3976c5f5f699d895ac92979fd156`，main 和标签 CI 均成功；便携 ZIP 及 `.sha256` 已上传，ZIP 的 GitHub 服务端 SHA-256 为 `594ff252ae105a40f85fffd9d3e499d7b54fe119f307ec139b496e1cd370546e`。这些发布核对不等于全面安全或隐私审计通过。

## 公开与私有边界

| 项目 | 当前边界 |
|---|---|
| 仓库 | `wuwa-translate-public` 是单独创建、无原私有 Git 历史的公开仓库；原 `程序文件/subtitle_pipeline` 仍为私有，不能直接公开或推送其历史。 |
| 许可 | 项目原创源码和文档使用 MIT；CPython、Deno、Python wheels、图片、游戏内容和商标须按各自权利与声明处理。 |
| 数据 | 公开快照不含真实任务、个人翻译记忆、内部报告和 benchmark 语料；`data/translation_memory.json` 为供程序使用的空结构，`data/glossary.db` 为脱敏术语副本。历史私有数量见 `DATA_CONTRACT.md`，不代表公开版。 |
| 运行产物 | 用户上传的 SRT、下载的字幕、翻译结果和任务状态在运行机器的 `download/<任务 ID>/` 下形成；用户应自行决定保留或删除。将 SRT 送往所配置的模型接口会向该服务商传送字幕与必要上下文。 |
| API Key | 新输入的 Key 可选本次工作台会话或 Windows Credential Manager；会话 Key 关闭程序后失效，选择安全保存的 Key 属于当前 Windows 用户配置。页面不回显 Key 原文；使用远端模型仍需信任自己选择的接口服务商。 |
| 基础便携包 | 不内置 FFmpeg、Whisper、CUDA、模型、用户任务、Cookie 或 Key；固定哈希依赖与具体资产清单以相应发布包的 manifest、`RUNTIME-COMPONENTS.json` 和 SHA-256 文件为准。 |

## 已有证据及其限制

- 历史 `security_best_practices_report.md` 审的是原私有仓库的旧状态；其中“尚无 LICENSE”“仍为 Private”“没有 Release”不适用于现在的公开仓库。此前修复记录只能作为回归线索。
- `KNOWN-ISSUES.md` 记录跨盘上传和非 UTF-8 CLI 输出的修复及当时隔离测试；它不是完整系统安全结论。
- Beta.5 的三项交互调整和 beta.6 的文案校正在 `CHANGELOG.md` 中记录。Beta.6 文案的 96 项 Web API 定向测试在独立 `.git` 副本内通过；这不等于全量安全扫描或真实模型翻译验收。

## 需要独立复查

1. 完整公开 Git 历史、提交元数据、Actions 日志/资产、Releases ZIP 和文档是否泄露凭据、个人路径、真实字幕或私有报告；发现疑点时只在本机脱敏记录证据。
2. 本机 Web 服务的会话、Origin/Host、CSRF、脚本注入、上传路径、跨盘/符号链接、任务文件访问、日志和异常脱敏边界。
3. 模型 Base URL、代理与第三方数据流；会话 Key 与已保存 Key 的优先级、注销、进程退出和 Windows 账户隔离。
4. 依赖锁、二进制来源/签名、便携包组成和第三方许可；审查第三方素材与游戏数据的再分发权。
5. 在带独立 `.git` 的新隔离副本中运行所需测试，清空 `PYTHONPATH`，把 `--basetemp` 放在副本内。禁止在原私有仓库或真实任务目录运行 pytest；审查不应调用真实模型或使用真实 API Key。

审查结论须按“已验证 / 尚未验证 / 历史记录”区分，并给出可复核的文件位置、影响与最小修复建议；不要把缺少发现写成“安全无漏洞”。
