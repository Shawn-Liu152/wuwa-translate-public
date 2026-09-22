# 鸣潮视频字幕翻译工作台

**当前版本：0.2.0-beta.3 · Beta 测试版 · 支持英语 / 日语 / 韩语 → 简体中文**

公开源码版：包含最新交付、会话恢复和列表优化；不包含私人任务、翻译记忆、内部报告或旧 Git 历史。便携包暂未在本仓库发布，请使用下方源码安装步骤。

面向 Windows 桌面端的本地字幕翻译工具。它可以下载 YouTube
视频与同语言字幕，按需调用本地 Whisper 补充语音识别，再通过兼容
OpenAI `chat/completions` 的模型完成中文翻译、术语统一和风险复核。

所有任务文件默认保存在项目的 `download/<任务 ID>/` 中。API Base URL、主模型和代理由后端原子保存并在下次启动时恢复；API Key 在 Windows 上保存到 Credential Manager，API 只返回“已配置”状态而不回显原文。安全存储不可用时仅退回当前会话，绝不写入 JSON、localStorage、任务文件或日志。

> [!WARNING]
> 这是面向受邀测试者的 Beta 版本，不是“无需复核”的稳定版。英语流程可用于测试；日语和韩语仍属实验性功能。重要字幕发布前请人工复核，尤其是人名、数字、否定和上下文相关句子。

> [!IMPORTANT]
> 英语使用英语优化的 Distil-Whisper；日语和韩语默认使用多语言 Whisper
> Turbo 并启用 VAD。三种源语言的术语、Prompt 和翻译记忆彼此隔离。

## 主要能力

- 单一路径：填写视频链接和必要 API 配置后下载，下载完成即可直接翻译。
- 自动来源：系统按源语言使用现有来源选择与质量评估；剪映源语言字幕是默认关闭的可选补充。
- Round 1 全量翻译：完整句弹性分组、局部与全局上下文、术语预锚定、有限并发、断点续跑。
- Round 2 风险复核：只处理明确风险，保留有效局部结果并只补请求缺失 ID。
- 人工审校：对比两轮结果、差异高亮、快捷键、批量确认、未保存提醒。
- 防污染翻译记忆：人工改译默认只归档；明确勾选后，才在相同游戏、英文完全一致的新任务中复用。
- 安全导出：原始阶段产物不可变，人工结果单独保存为 `final.reviewed.zh.srt`。

## 系统要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 或 Windows 11 x64 |
| Python | 便携版已内置；源码安装使用 Python 3.11 或 `uv` |
| FFmpeg | 纯字幕获取/翻译不需要；下载视频、音频处理和 Whisper 时需要 |
| 模型接口 | 兼容 OpenAI `POST /v1/chat/completions` 的接口 |
| GPU / CUDA | 本地 Whisper 当前需要兼容的 NVIDIA GPU 与 CUDA；无兼容环境时请仅使用 YouTube 字幕 |
| 网络 | 下载 YouTube 素材、首次获取 Whisper 模型和请求翻译接口时需要 |

## Windows 便携版（推荐给测试者）

1. 下载名称以 `windows-x64-portable.zip` 结尾的文件，并完整解压到桌面、文档等可写目录。
2. 双击 `启动翻译工作台.bat`；不需要预装 Python，也不需要管理员权限。
3. 浏览器打开后填写视频链接、API Base URL、API Key 和主模型；配置保存后可复用，下载完成即可直接翻译。也可以上传已有 SRT 作为主字幕或辅助字幕。
4. 结束服务可关闭启动窗口，或双击 `停止翻译工作台.bat`。

基础便携版不内置 FFmpeg、Whisper、CUDA 或模型。没有 FFmpeg 时会自动关闭
“下载视频”；字幕会从 YouTube 原生 VTT 在本机转换为 SRT，封面保留原始图片格式，
因此字幕获取与翻译仍可使用。没有 Whisper 时会固定使用 YouTube 字幕。
需要处理无字幕视频时，请先提供 SRT 或另行安装可选 Whisper 环境。详见
[`WINDOWS-PORTABLE.md`](WINDOWS-PORTABLE.md)。

## Windows 源码安装（开发者）

1. 安装 Python 3.11，并确保 FFmpeg 可以从命令行执行。
2. 双击 `install_web.bat` 安装网页端、下载器和基础翻译依赖；安装器使用仓库内带哈希
   的二进制锁，不会自动升级到未审计的 `yt-dlp`。
3. 如需本地 Whisper，再双击 `install_whisper.bat`。
4. 双击 `start_web.bat`；启动器会独占一个随机的本机端口并自动打开工作台。
5. 结束服务可双击 `stop_web.bat`。

不要保存旧的 `127.0.0.1:8765` 书签或手动访问本机端口。每次启动都会建立
新的临时安全会话；页面提示会话无效时，请关闭页面和启动窗口后重新双击启动脚本。

名称以 `windows-beta-source.zip` 结尾的文件仍是“安装后运行”的源码包，不是
免安装 EXE；只有 `windows-x64-portable.zip` 已包含 Python 运行时。两种包都必须
先完整解压，不能直接在 ZIP 压缩包内部运行。

页面左下角会显示依赖自检结果。缺失的可选能力会在任务开始前暴露，不需要等到长流程中途才发现。

## 使用流程

1. 粘贴视频链接，填写 API Base URL、API Key 和主模型；代理首次默认为 `http://127.0.0.1:7890`，可修改并记忆。
2. 开始下载。系统自动读取可用字幕并按既有规则评估来源，不需要选择工作流模式。
3. 下载完成后直接开始翻译。正常流程不需要再次填写 API、模型或代理。
4. 如需补充剪映源语言字幕，勾选可选开关并上传 SRT；未勾选时普通流程可直接完成。
5. 在结果页查看、审校并导出最终字幕。

如果中转站依赖 VPN，请在“代理”中填写模型请求实际使用的代理，例如 `http://127.0.0.1:7890`。代理会传递给翻译请求和需要联网的下载阶段。代理地址不得包含用户名或密码；需要认证时请由本机代理工具管理凭据。

## 应该下载哪个文件

- `final.reviewed.zh.srt`：经过人工审校后的首选成品。
- `final.zh.srt`：自动流程生成的成品；尚未进行人工修改时使用它。

页面默认只突出最终字幕。下面这些文件属于可恢复、可追溯的技术产物：

- `final.round1.zh.srt`：不可变的 Round 1 全量译文。
- `final.round2.zh.srt`：不可变的 Round 2 精校译文。
- `final.zh.risk.generated.json`：算法生成的不可变风险队列。
- `final.zh.round2.results.json`：Round 2 模型决定与结果。
- `final.zh.review-state.json`：人工确认、修改和备注。
- `final.zh.srt.manifest.json`：批次状态与断点恢复信息。
- `final.zh.en.union.final.srt`：两路英文证据合并后的翻译输入。

不要手工覆盖技术产物。重新评分、恢复失败批次或继续审校时，系统会根据它们复用已完成工作。

## 审校快捷键

- `1`：采用 Round 1。
- `2`：采用 Round 2。
- `Enter`：保存当前文本。
弱风险（例如普通双源文本差异、阅读速度偏快）只供人工参考，不会自动覆盖 Round 1。只有明确漏译、数字/否定/官方术语错误，且 Round 2 明确选择替换并通过校验时，系统才自动采用新译文。

日语或韩语 Round 1 若确定残留假名/韩文，而 Round 2 给出无源文字但低置信的
中文候选，最终显示可暂用该候选避免源文字残留；它仍标记为
`accepted=false`、`applied=round2_review` 并留待人工复核，不会冒充已解决或
人工确认。其他低置信候选仍回退到 Round 1。

所有已知人物名或可靠人物别名都会进入人工确认，但译名正确本身不会额外
触发 Round 2。这样可以确认 ASR 有没有认错人物，同时避免为正确译名重复付费。

Round 1 默认保留临近的完整句，附带前后六条局部证据、最近十二条对话、
视频标题、频道和全片锁定专名。人工修改会写入本机
`data/translation_memory.json`；该文件不进入发布包，也不会在未批准时注入模型。

## 费用估算

工作台目前只对 DeepSeek 官方仍在售的 `deepseek-v4-flash` 和
`deepseek-v4-pro` 显示人民币费用估算。输入 Token 采用官方缓存未命中价，
因为当前任务指标不区分缓存命中量；其他模型显示“暂不估算”。

使用中转站时，估算只反映 DeepSeek 官方价，不代表中转站最终账单。

## 命令行翻译

基础用法：

```powershell
.venv\Scripts\python.exe -m pipeline.main input.en.srt -o output.zh.srt --model MODEL --api-key KEY
```

常用参数：

- `--proxy http://127.0.0.1:7890`：为模型请求指定代理。
- `--batch-size 20`：手工限制每批字幕数；默认会按 Token 自适应。
- `--manifest path.json --resume`：恢复已成功的批次。
- `--fresh`：明确放弃已有 manifest 并从头运行。
- `--dry-run`：不请求模型，用于验证流程。

## 开发与验收

```powershell
.venv\Scripts\python.exe -m pytest -q
node --check web\static\app.js
```

翻译质量工具默认离线或 dry-run；真实模型调用与数据写入都必须显式开启：

```powershell
# 汇总既有任务，不改写历史产物
.venv\Scripts\python.exe tools\summarize_job_metrics.py download --output-jsonl reports\job_metrics.jsonl --output-csv reports\job_metrics.csv --output-md reports\JOB_METRICS_SUMMARY.md

# 固定 confirmed IDs 的离线 benchmark（不调用模型）
.venv\Scripts\python.exe tools\run_benchmark.py --benchmark benchmarks\en_reaction_benchmark.json --language en --gold-status confirmed --ids-file benchmarks\confirmed_ids_en.json --output reports\benchmark_offline_en.json

# 导出 provisional Gold 供真人审核；导入仍默认 dry-run
.venv\Scripts\python.exe tools\export_benchmark_review.py benchmarks\en_reaction_benchmark.json reports\benchmark_review_en_provisional.csv --gold-status provisional
.venv\Scripts\python.exe tools\import_benchmark_review.py benchmarks\en_reaction_benchmark.json reports\benchmark_review_en_provisional.csv --ledger reports\benchmark_review_en_ledger.jsonl

# 从新真实任务生成去泄漏、未人工确认的候选包与空白审核 CSV
.venv\Scripts\python.exe tools\build_benchmark_candidate_pack.py en download\JOB --benchmark benchmarks\en_reaction_benchmark.json --output-json reports\benchmark_candidates_en.json --output-csv reports\benchmark_review_en.csv

# 官方证据术语裁决默认 dry-run；只有确认报告后才使用 --apply
.venv\Scripts\python.exe tools\adjudicate_glossary.py --evidence-file reports\automated_official_evidence_20260821.json --report reports\AUTOMATED_CANDIDATE_ADJUDICATION.md
```

Benchmark CSV 只有填写真实 reviewer、带时区的 reviewed_at 与 evidence 后，才
能通过显式 `--apply` 升级状态；Codex/ChatGPT/AI/“human”等自动或占位身份会被
拒绝。付费模型 A/B 还需满足 `reports/AB_READINESS_REPORT.md` 的全部门禁。

翻译质量改动应优先使用真实任务中经过人工确认的脱敏样本做前后对比；自动化测试
负责守住字幕解析、术语、人名风险、Round 2 门禁、任务恢复和导出等确定性行为，
不能代替对真实模型译文的人工判断。

核心目录：

- `pipeline/`：字幕解析、双源匹配、翻译、风险评分与 Round 2。
- `web/`：FastAPI 服务和桌面端页面。
- `data/`：提示词、官方术语、别名和易错识别映射。
- `tests/`：算法、任务恢复、Web API 与导出等确定性回归测试。
- `reports/`：任务指标、官方证据、TM/few-shot、Round 2 与 A/B 门禁审计。

原创测试素材：

- [`examples/demo.en.srt`](examples/demo.en.srt)：33.6 秒、10 条英文字幕，
  不包含影视、游戏或视频对白，可用于安装后的基础流程测试。

版本记录：

- [`VERSION`](VERSION)：当前 Demo 版本。
- [`CHANGELOG.md`](CHANGELOG.md)：版本更新与已知限制。

## 维护约定

- 每次修复用户可感知的报错、异常行为或操作障碍，都必须同步更新工作台的
  “常见问题”页面。
- FAQ 必须写清楚：用户看到的现象、发生原因、可以直接执行的解决步骤。
- 如果修复改变了旧操作方式，必须同时更新“使用说明”、删除失效指导，并增加
  对应的 Web 回归断言。

## 已知限制

- 目标语言当前固定为简体中文；源语言支持英语、日语和韩语。
- 模型中转站的稳定性、速率限制和模型权限由对应服务商决定。
- Whisper 首次使用需要联网下载模型，所需时间和磁盘空间取决于模型大小。
- 剪映字幕仍需手工导出，但日语和韩语流程不再强制上传。
- 本地 Whisper 当前固定使用 NVIDIA CUDA，CPU 回退尚未实现；无兼容环境时请仅使用 YouTube 字幕来源。
- 基础便携版面向 Windows 10/11 x64，不包含 FFmpeg 或 Whisper；视频下载和无字幕语音识别需另行安装对应能力。
- YouTube 字幕和自动语音识别质量会直接影响最终翻译上限。

## 隐私与发布注意事项

- 不要提交 `.env`、API 密钥、视频、任务目录或日志；这些内容已被 `.gitignore` 排除。
- 已经在聊天、截图或 Git 历史中暴露过的密钥应立即在服务商后台作废并重新生成。
- 现有私有仓库的旧提交可能保留作者邮箱或历史本机路径，不能直接改成公开仓库。对外分享请只使用下方脚本生成的无历史目录，并在新的空仓库中首次提交。
- 本项目尚未附带开源许可证。公开 GitHub 仓库前应先确定许可证，并确认图片素材和游戏专有名词数据的使用方式。
- YouTube 下载和字幕处理应遵守平台条款、版权与当地法律，只处理有权使用的内容。

## 生成 GitHub 发布目录

在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_github_package.ps1
```

脚本会在 `github-package/` 下生成带版本号的独立目录，并同时生成名称明确的
`windows-beta-source.zip` 与对应 `.sha256` 校验文件。发布目录只包含程序、页面、
术语数据、可选的公开演示图、测试和安装脚本。`docs/` 采用公开文件白名单，
不会整目录复制。发布目录也不会包含 `download/`、`reports/`、
内部交接/审计材料、虚拟环境、运行时依赖、缓存、Cookie、日志或任务数据。
生成前还会拒绝常见密钥、个人邮箱、本机用户目录和当前系统账号标识，最后生成
SHA-256 文件清单。该目录没有原仓库 Git 历史，适合在确认许可证和素材授权后
作为新仓库的首次提交来源。

## 生成 Windows x64 便携包

先提交所有发布输入，再在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_windows_portable.ps1
```

构建器只从 committed `HEAD` 的脱敏白名单取应用文件；它下载并核对固定 SHA-256
的官方 CPython 与 Deno，按带哈希的锁文件安装最小 Python 依赖，随后再次检查
Cookie、密钥、个人路径、任务、报告、翻译记忆、FFmpeg 和 Whisper 均未进入包。
最终在相邻 `release/` 目录生成便携 ZIP、逐文件 manifest、运行时组件清单和独立
`.sha256`。构建机需要 `uv` 和网络；使用便携包的电脑不需要 `uv` 或系统 Python。
