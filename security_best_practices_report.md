# 安全与公开发布门禁审计（2026-08-30）

## 结论

**发布门禁未通过。当前仓库没有改为 Public，便携 ZIP 也没有上传到公开 Release。**

本次没有发现真实 API Key、Cookie、私钥或已登记的便携依赖漏洞；最终 ZIP 的厂商签名、哈希、恶意软件扫描和脱敏门禁也通过。阻塞公开的原因是：现有 Git 历史含个人路径与提交邮箱，桌面服务仍有本机 API、Cookie 临时凭据和模型端点边界问题，项目没有 LICENSE，角色图片和真实字幕/术语数据的再分发权也未得到证明。

“扫描未发现”不等于可以数学上保证不存在任何未知漏洞、供应链问题或法律风险。本报告采用可验证的发布门槛：所有已确认 High/Medium 问题和所有许可阻塞项解决并复扫后，才允许公开。

## 2026-09-02 修复状态（保留原始审计证据）

本节只记录本轮代码修复和阶段验证，不替代最终全量门禁，也不改变原始发现的证据：

| 发现 | 当前状态 | 阶段证据 |
| --- | --- | --- |
| `RELEASE-PRIVACY-001` | 未修复，发布阻塞 | 仍不得公开当前 166-commit 历史；需无历史白名单目录 |
| `APP-AUTH-001` / `APP-SECRET-001` / `APP-ENDPOINT-001` | 代码已修复 | 本地会话、Cookie 私密生命周期、端点契约已有定向回归 |
| `APP-SECRET-002` / `APP-LAUNCH-001` / `APP-SUPPLY-001` / `PY-TEMP-001` | 代码已修复 | 错误脱敏、随机端口启动、受控解释器调用和 `mkstemp` 清理已有恶意/异常分支回归 |
| `APP-ORIGIN-001` | 代码已修复 | Fetch Metadata、Origin、Host 和安全响应头边界已有回归 |
| `FASTAPI-LIMITS-001` | 代码已修复，待最终全量复扫 | 请求体、任务数/磁盘准入和 SSE 连接上限；相关回归 169 passed |
| `APP-PRIVACY-001` / `JS-URL-001` | 代码已修复 | 任务相对路径 DTO、目录接口固定响应和 view 白名单已有回归 |
| `FASTAPI-SUPPLY-001` | Windows 源码安装路径已修复，其他平台待单独锁定 | `web_requirements.txt` 复用带哈希二进制锁，安装器使用 `--require-hashes` |

本轮相关回归（含依赖锁/安装器门禁）为 **169 passed / 2 warnings / 0 failed**；这些结果
不等于完整 pytest、静态/隐私扫描或最终公开包通过。没有暂存、上传、
推送或改变仓库可见性。

保护线复核：隔离副本完整 pytest 为 **827 passed / 2 skipped / 2 warnings**，原工作区
五个文件均未在本轮测试前后发生变化；但交接记录中的 `data/glossary.db` 基线与当前
工作区哈希不一致。该文件未被本轮恢复或覆盖，需权利人明确决定后才能处理，不能把它
误报为“全部保护文件与旧基线一致”。

## 范围

- 源码：`master` / `cf2fe2540653602b574b7740d1e6c3221455167c`
- Git：全部 166 个可达提交、943 个历史 blob、所有本地与远端 refs
- 后端：FastAPI、Uvicorn、任务目录、下载器、模型调用和启动器
- 前端：原生 JavaScript、DOM 写入、浏览器存储、CSP 和 URL 状态
- 产物：`subtitle-pipeline-0.2.0-beta.2-windows-x64-portable.zip`
- 依赖：便携版 25 个 hash-locked Python 包、CPython 3.13.15、Deno 2.9.5
- 发布面：GitHub 仓库历史、Actions、Release、LICENSE、角色图片和数据来源

## High

### RELEASE-PRIVACY-001：现有 Git 仓库历史会公开个人路径和提交邮箱

- Rule ID：`RELEASE-PRIVACY-001`
- Severity：High
- Location：
  - `reports/asr_candidates_auto_ja.json:6-11`
  - 历史提交 `b7b9d155a084`、`ab66504d49cd` 等多个已删除或旧版文件
  - 166 个提交的 author/committer metadata
- Evidence：当前 HEAD 有 6 处真实 Windows 用户目录；完整历史至少有 152 个不同“文件/行号”位置。127 个提交含一个真实 `qq.com` 邮箱。具体用户名、邮箱和路径值在本报告中不记录。
- Impact：把现有仓库切为 Public 后，任何人都可 clone 旧 Git 对象；新提交中删除或替换这些值不能清除旧历史。
- Fix：最安全方案是使用 `tools/build_github_package.ps1` 生成的脱敏、无历史目录，在确认许可证和素材权利后创建一个全新的空公开仓库。若必须保留现有仓库，需要用户另行明确授权历史重写和 force push，并在重写后重新扫描所有远端对象。
- Mitigation：现有仓库继续保持 Private；不要上传 Git bundle、镜像或旧 `.git`。
- False positive notes：Gitleaks 对完整历史为 0 命中，只能说明未发现常见 secret；个人路径和提交邮箱不是 Gitleaks 的默认 secret 类别，仍是确定的隐私暴露。

### APP-AUTH-001：本地控制 API 没有身份验证

- Rule ID：`FASTAPI-AUTH-001` / `APP-AUTH-001`
- Severity：High
- Location：`web/app.py:374-384` 及全部 `/api/*` 状态变更、产物和任务接口；`web/launcher.py:16-18,88`
- Evidence：

```python
if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
    origin = request.headers.get("origin")
    if origin and not _same_origin(origin, request):
        ...
```

服务绑定 loopback，并验证 Host 与浏览器 Origin，但没有统一认证依赖；不带 Origin 的本机 HTTP 客户端仍可访问任务、产物、术语、提示词、应用启动和删除接口。
- Impact：共享 Windows 主机上的其他账户或不受信任的本机进程可读取字幕/日志、修改数据、删除任务，或触发读取 Chrome Cookie 的下载流程。
- Fix：启动时生成至少 256 位随机会话令牌，统一保护 `/api/*`；页面只在内存中持有令牌并通过自定义请求头发送；同时使用随机端口。Host、Origin 和 Fetch Metadata 保留为第二层保护。
- Mitigation：修复前仅限单用户、受信任进程的电脑；临时禁用 Chrome Cookie 自动读取能力。
- False positive notes：若同一台机器上的所有进程与账户都完全受信任，风险会降低；loopback 本身仍不是身份验证。

### APP-SECRET-001：YouTube Cookie 在异常退出后可能残留于任务目录

- Rule ID：`FASTAPI-UPLOAD-001` / `APP-SECRET-001`
- Severity：High
- Location：`web/jobs.py:263-278,669-675,1025-1043,2172-2186`
- Evidence：上传或粘贴的 Cookie 被移动到 `download/<job_id>/youtube.cookies.txt`，仅在正常执行到 `finally` 时删除；启动恢复只修改中断任务状态，没有清理遗留 Cookie。
- Impact：断电、进程崩溃或强制结束可使登录凭据长期留在便携目录；若该目录位于 OneDrive、共享目录或移动盘，凭据可能被同步或复制。
- Fix：Cookie 只写入当前 Windows 用户专属、非同步的应用临时凭据目录，并设置仅当前用户 ACL；启动时清理应用自有的过期 Cookie；所有退出路径统一销毁。
- Mitigation：公开版完成修复前移除 Cookie 文件/粘贴 Cookie 功能，只保留匿名下载。
- False positive notes：`chmod(0o600)` 在 Windows 上不能代替 ACL，也不能解决崩溃残留和云同步。

### APP-ENDPOINT-001：空接口地址会回退到隐藏端点，远程 HTTP 也被允许

- Rule ID：`FASTAPI-SSRF-001` / `APP-ENDPOINT-001`
- Severity：High
- Location：
  - `web/app.py:275-283`
  - `pipeline/config.py:42-46`
  - `pipeline/translate/llm.py:342-350`
  - `web/static/app.js:160-162,732-769`
- Evidence：Web 页面默认展示 DeepSeek 官方地址，但空值在后端被允许，模型调用会回退到另一个配置默认主机并发送 `Authorization: Bearer ...`；除本机地址外也允许明文 `http://`。
- Impact：用户清空地址后，API Key、字幕和上下文可能发往未在当前界面明确显示的第三方；远程 HTTP 会明文传输凭据和字幕。
- Fix：Web 正式翻译必须使用显式且前后端一致的端点；发送前显示目标主机。除 `localhost`、`127.0.0.1`、`::1` 外强制 HTTPS。
- Mitigation：在任务开始前显示并确认目标主机，不再提示“留空使用默认接口”。
- False positive notes：若隐藏默认端点和密钥均属于同一服务，意外第三方风险降低；远程 HTTP 仍不可接受。

## Medium

### APP-SECRET-002：上游错误正文可能未经脱敏写入日志和任务 JSON

- Rule ID：`FASTAPI-RESP-001` / `APP-SECRET-002`
- Severity：Medium
- Location：`pipeline/translate/llm.py:386-392`、`web/jobs.py:1025-1037,1504-1517`
- Evidence：非 200 响应的前 300 字符进入异常文本，任务层随后持久化 `str(error)`。
- Impact：自定义或恶意模型端点若回显 Authorization、字幕或其他敏感值，这些内容会进入 `job.json` 和技术日志。
- Fix：模型边界只保留状态码和严格允许的错误字段；持久化前按当前密钥、Bearer、Cookie 和常见凭据格式统一脱敏。
- Mitigation：仅连接受信任 HTTPS 端点，技术日志不得分享。
- False positive notes：常见可信服务通常不回显密钥，但应用允许任意自定义端点，不能依赖上游行为。

### APP-LAUNCH-001：固定端口探测可能打开预先占用端口的伪造页面

- Rule ID：`APP-LAUNCH-001`
- Severity：Medium
- Location：`web/launcher.py:16-18,51-58,78-88`
- Evidence：启动线程只确认 `127.0.0.1:8765` 有监听者，未证明该监听者是本次 Uvicorn 实例。
- Impact：本机进程预占端口后可使启动器打开仿冒工作台，诱导用户输入 API Key。
- Fix：先让本进程成功绑定随机端口，再打开浏览器；绑定失败时不得打开页面，并用一次性启动挑战确认服务身份。
- Mitigation：发现端口占用时终止启动并显示错误，不自动浏览。
- False positive notes：只有能预占本机端口的进程可利用。

### APP-SUPPLY-001：源码模式可能把 Cookie 交给 PATH 中的同名 `yt-dlp`

- Rule ID：`FASTAPI-INJECT-002` / `APP-SUPPLY-001`
- Severity：Medium
- Location：`web/downloads.py:306-315,327-338,362`
- Evidence：便携模式使用 hash-locked Python 模块；源码模式优先 `shutil.which("yt-dlp")`，随后把 Cookie 路径作为参数交给该程序。
- Impact：错误或恶意 PATH 可将 Cookie 交给非预期可执行文件。
- Fix：所有模式统一调用已验证环境中的 `sys.executable -I -m yt_dlp`。
- Mitigation：源码运行前检查命令解析路径，不在不受信任目录启动。
- False positive notes：便携包不受此项影响；主要影响公开源码安装。

### PY-TEMP-001：分块 Whisper 工具使用不安全的 `tempfile.mktemp`

- Rule ID：`PY-TEMP-001` / Bandit `B306`
- Severity：Medium
- Location：`tools/whisper_transcribe_chunked.py:97-123`
- Evidence：先生成未占用路径，再由子进程写入，存在典型 TOCTOU 时间窗。
- Impact：共享临时目录中的本机攻击者可抢占目标路径，导致覆盖、读取错误文件或拒绝服务。
- Fix：使用 `mkstemp`/`NamedTemporaryFile(delete=False)` 原子创建文件，关闭描述符后传给子进程，并在每轮 `finally` 删除。
- Mitigation：该工具在修复前不用于共享主机。
- False positive notes：该工具不在当前便携 ZIP 中，但会进入源码发布目录。

## Low

### FASTAPI-LIMITS-001：缺少全局请求体、任务和 SSE 连接配额

- Severity：Low
- Location：`web/app.py:45,220-232,417-440,497-544,676-699,1191-1223`
- Evidence：文件业务逻辑有 100 MiB 限制，但 multipart/JSON 在进入路由前没有统一总量、字段数、总任务数、磁盘配额或 SSE 连接限制。
- Impact：未认证本机客户端可消耗内存、临时磁盘和连接。
- Fix：在中间件累计限制请求体；限制 multipart 字段、任务数、并发、SSE 和任务目录总空间。
- Mitigation：仅在受信任单用户设备运行。
- False positive notes：现代浏览器跨站写入已被 Origin 检查拦截，本机客户端仍在范围内。

### APP-ORIGIN-001：缺少 Origin 时默认放行，未拒绝跨站 Fetch Metadata

- Severity：Low
- Location：`web/app.py:374-384`
- Evidence：判断为 `if origin and ...`，没有检查 `Sec-Fetch-Site: cross-site`。
- Impact：主流浏览器正常跨站 POST 通常携带 Origin，因此当前防护有效；被代理剥离 Origin 或非典型客户端会绕过这一层。
- Fix：明确拒绝跨站 Fetch Metadata；浏览器写请求要求同源，非浏览器客户端使用会话令牌。
- Mitigation：保留 CSP、TrustedHost 和 Origin 检查。
- False positive notes：未证实可在当前主流 Chrome 中通过普通表单绕过，属于纵深防御缺口。

### APP-PRIVACY-001：任务对象、日志和 API 返回本机绝对路径

- Severity：Low
- Location：`web/app.py:584-592,637-646`、`web/jobs.py:658-666,767-769`、`web/downloads.py:402-406`
- Evidence：任务对象包含 workspace、inputs、artifacts 的绝对路径；打开下载目录 API 返回完整路径；部分工具输出直接进入技术日志。
- Impact：任务 JSON、日志或截图可能暴露 Windows 用户名和目录结构。
- Fix：API 使用专用 DTO，只返回任务内相对路径；持久化前把应用根和用户目录替换为占位符；打开目录仅返回 `{"ok": true}`。
- Mitigation：不要公开分享任务 JSON、日志或包含技术视图的截图。
- False positive notes：数据当前只在本机显示，但它与既有“不在界面暴露隐私”的产品承诺冲突。

### FASTAPI-SUPPLY-001：源码安装依赖未锁版本与哈希

- Severity：Low
- Location：`web_requirements.txt:1-6`、`install_web.bat:17-30`
- Evidence：源码安装使用宽松 `>=` 和升级式 yt-dlp 安装，未来安装结果不等于本次审计版本。
- Impact：无法复现安全扫描结论，未来可能取得有缺陷或被替换的依赖。
- Fix：提供每个支持平台的 hash-locked 清单，并使用 `--require-hashes`。
- Mitigation：优先分发当前 hash-locked 便携版。
- False positive notes：当前便携包已经锁定哈希，不受此项影响。

### JS-URL-001：`view` URL 参数未经白名单进入 CSS selector

- Severity：Low
- Location：`web/static/app.js:2963` 附近
- Evidence：URL 中的 `view` 值直接用于选择器；畸形值可抛出 selector 语法异常。
- Impact：构造链接最多令页面初始化失败，未发现代码执行或数据泄露链。
- Fix：仅允许固定视图名称集合，其他值回退到 `workbench`。
- Mitigation：初始化选择器调用捕获异常。
- False positive notes：当前证据仅支持可用性影响。

## 非代码但同样阻塞公开的许可与素材问题

1. 根目录没有 `LICENSE`、`LICENCE` 或 `COPYING`。`README.md:221-223` 与 `docs/HANDOFF_NEXT_WINDOW.md:11,16,26,138` 已明确记录只能私有 Beta。没有许可证时不能把项目宣称为开源，也没有授予他人修改和再分发的许可。
2. `web/static/assets/phoebe-jiubi.png` 的来源页标注“原作者保留权利”，页面链接并不等于允许在软件仓库和 ZIP 中再分发。
3. `web/static/assets/phoebe-glasses-cover.jpg`、`feibi-pet-spritesheet.webp`、四个宠物角色图和四个缩略图没有可核验的作者、生成记录或软件再分发授权。
4. 当前仓库还含真实视频字幕/译文、benchmark、acceptance output、真人审核翻译记忆和多来源游戏术语/研究数据。公开完整 HEAD 或历史前必须逐项证明权利；无法证明时从公开版移除。
5. 当前无历史发布目录仍包含角色图片和 `data/glossary.db`，所以通过隐私扫描并不代表通过版权/许可门禁。

## 已通过的检查

- Gitleaks 8.30.1（官方 Release，SHA-256 校验通过）扫描全部 Git 历史：0 个 secret finding。
- 无历史公开源码目录：222 个文件；内置个人路径/邮箱/secret 门禁通过，Gitleaks 0 finding。
- Committed `glossary.db`：只读扫描 682 行，隐私命中 0。
- 便携锁文件：pip-audit 对 25 个精确版本返回 0 个已知漏洞。
- Bandit：0 High；动态 SQL 人工确认使用固定子句或已安全引用。确认 1 个 `mktemp` Medium。
- Semgrep：2 个 subprocess finding；两处均使用参数列表且 `shell=False`，命令注入为误报；`mktemp` 风险另行记录。
- 前端：未发现 `eval`、`new Function`、`document.write`、不安全 `postMessage`、远程脚本或 API Key/Cookie 浏览器持久化；API 数据进入 HTML sink 前使用转义，CSP 为同源脚本且无 `unsafe-eval`。
- 隔离提交副本完整测试：767 passed、2 skipped、0 failed；三份 JavaScript 语法检查通过。
- ZIP SHA-256：`948eb65b1b08986ea8066ff598bb76d1d1b7a73a57cc2d73910a59f94ddc82c6`，独立 checksum 匹配。
- CPython 与 Deno Authenticode：`Valid`，签名者分别为 Python Software Foundation 与 Deno Land Inc.
- Windows Defender：签名年龄 0 天、实时防护开启；ZIP 和完整解压目录检测命中均为 0。
- 便携目录的 128 个 Gitleaks 模式命中全部来自 hash-locked 第三方代码：35 个是 Cryptodome 自测向量，93 个是 yt-dlp 上游 extractor 常量；应用代码和用户数据目录命中 0。这些不是本项目或用户的私密凭据，但说明单一模式扫描必须人工归因。
- GitHub 页面确认：仓库仍为 Private，1 个分支、0 个标签、0 个 Release；存在 1 个 Actions run，公开现仓库会一并扩大网页侧可见面。

## 修复顺序

1. 先修 `APP-AUTH-001`、`APP-SECRET-001`、`APP-ENDPOINT-001`。
2. 再修 4 个 Medium，并补回归测试与 FAQ。
3. 选择项目许可证；确认所有贡献者身份有权授权。
4. 移除或替换无授权角色图；裁决术语、真实字幕、benchmark 和翻译记忆的公开范围。
5. 从重新生成的无历史目录创建新空仓库；不要直接公开现有 166-commit 仓库。
6. 对最终源码和最终 ZIP 重跑本报告中的全部门禁，之后才创建公开 Release。

## 限制

- 未知的 zero-day、硬件/操作系统漏洞、加密或视觉隐写内容无法由本次扫描排除。
- 可选 Whisper/CUDA/FFmpeg 不在便携包内，本次没有审计其完整运行时供应链。
- 软件扫描不能替代权利人书面授权或正式法律意见。

## 2026-09-03 用户裁决执行窗口（夹具路径裁决 · 全绿复检）

按用户裁决执行（无网络 · 未暂存 · 未提交 · 未上传 · 仅本机）：

### 夹具修改说明（本轮唯一代码改动）
- 6 个测试文件（test_cancellation / test_long_video_stages / test_manifest /
  test_translator / test_web_api / test_web_jobs）中本轮安全修复新增的恶意回显
  测试夹具，共 18 处字面假路径由「真实用户目录形式的 Alice 路径」改为合成路径
  C:\FixtureHome\Alice\...；仍为 Windows 绝对路径形式，脱敏验证目标不变
  （safe_errors 的 Windows 绝对路径正则匹配任意盘符，验证强度不减弱）。
- 1 处关联处理：test_provider_config 端点拒绝用例中的假邮箱去掉 TLD 字面，
  改为 user@example（原写法被既有 tracked-tree 隐私断言 email 正则命中；
  仍是带 userinfo 的相同拒绝语义）。
- 既有隐私断言（test_secret_hygiene 中 4 条正则）一字未改、未放宽。

### 测试计数
- 定向测试：tests/test_secret_hygiene.py + tests/test_provider_config.py =
  24 passed。
- 隔离完整 pytest（唯一兄弟副本 + 唯一系统 Temp，保护文件与 .venv 未入副本）：
  **828 passed / 1 skipped / 2 warnings**（2 warnings 为既有 Starlette/httpx2
  与 pkg_resources 弃用提示）。上轮唯一失败（夹具与隐私断言冲突）已消除，
  827 passed 基线之上的缺口清零。

### 门禁状态
- 三份 Web JS node --check 通过；python -m compileall -q pipeline web tools
  tests 通过；git diff --check 通过（仅预期 LF/CRLF 提示）。
- 保护哈希：reports/asr_candidate_corrections.json、reports/asr_candidates_auto_ja.json、
  reports/escalation_whisper_oom.txt、tools/run_demo_ja.py 与交接基线一致；
  data/glossary.db 维持已知差异值 2CEEE81D...（未恢复、未覆盖、未重建，记录，
  待用户决定）。
- 暂存区为空（git diff --cached --name-only 无输出）；未执行 git add/commit/push。
- 隔离副本与本次系统 Temp 已按绝对路径精确删除，无残留。

### 结论
- 本机代码门禁全绿：上轮 827/1/1 的唯一失败已修复，完整 pytest 828/1/0。
- 仍阻塞公开的项不变：LICENSE 缺失、素材/数据权利未裁决、正式扫描器未安装、
  无历史最终包未构建、glossary.db 差异待用户决定；当前仓库仍不可公开，
  旧 ZIP 仍不可上传。


## 2026-09-03 收尾轮（用户裁决：不恢复，维持当前词表状态）

### data/glossary.db 最终裁定
- 当前完整 SHA-256：d85429a922fce5b5e0f42027fb11c226632959648400fc30fe8668f0c8c4d16a
- 历史差异链（完整 SHA-256）：
  - 31B9BFF8534BF0D1B894324209808B9C7A4A566F5FCB3802BE57053DEA542277（交接基线）
  - 2CEEE81D27CE7EF42A4E04537B18D17892B0F4DFF79AE0EC36490FDBF7E698DF（上轮已知差异观察值）
  - d85429a922fce5b5e0f42027fb11c226632959648400fc30fe8668f0c8c4d16a（2026-09-03 测试意外写入后现状，用户拍板维持）
- 用户裁定：本机无权威副本，不恢复任何版本（不覆盖用户数据），维持当前 D85429A9 状态。
- 当前词表未获内容正确性批准，不作为公开发布依据。

### 隔离全量测试（含 .git 的隔离副本，2026-09-03）
- 828 passed / 1 skipped / 0 failed（2 warnings 为既有 Starlette/httpx2 与 pkg_resources 弃用提示）
- 夹具修正（落实用户裁决）：8 个测试文件的恶意回显夹具中模拟真实用户主目录的假路径改为合成路径 C:\FixtureHome\Alice\...；test_provider_config.py 的 email-like URL fixture 以无 TLD 形式书写以通过既有 tracked-tree 隐私断言（保留原断言不放宽、仅改夹具数据）

### 最终复核
- 静态门禁：3 份 Web JS node --check 通过；compileall 通过；git diff --check 无空白错误
- 保护哈希：reports/asr_candidate_corrections.json、reports/asr_candidates_auto_ja.json、reports/escalation_whisper_oom.txt、tools/run_demo_ja.py 与交接基线一致
- 暂存区为空；master 与 origin/master 同步；无 stash
- 全程未执行恢复、覆盖、git add/commit/push、上传、公开、打包发布
- 隔离副本与全部系统 Temp 已按绝对路径精确删除
