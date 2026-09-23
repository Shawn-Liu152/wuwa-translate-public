# 项目与用户目录结构

> 历史私有工作区记录（2026-09-10）：下文的 `cc/程序文件/`、桌面入口和 NTFS Junction 是原私有项目的本机布局，不是当前公开仓库或 Windows 便携包的默认目录。公开版使用方式见 [README](../README.md) 与 [便携版说明](../WINDOWS-PORTABLE.md)。父目录整理不改变当时的 `download/<job_id>`、resume、manifest 或 Web API 契约。

## 用户可见目录

```text
cc/
├─ 启动工作台.bat
├─ 停止工作台.bat
├─ 下载内容/                 # 指向现役 download/ 的目录联接
└─ 程序文件/
```

日常使用只需要前面三个入口。`下载内容`与程序实际任务目录是同一批文件，不是复制副本。

## 程序文件

```text
程序文件/
├─ subtitle_pipeline/        # 现役 Git 仓库和运行环境
├─ 外部工具/
│  ├─ CapCutSRT/             # 独立 Git 仓库
│  └─ Y2A-Auto-audit-20260908/ # 独立 Git 仓库
├─ 历史归档/
│  ├─ 既有归档/
│  └─ 测试快照/              # 旧隔离副本，含需保留的历史素材
└─ 本地配置/
   └─ .claude/
```

## 现役仓库职责

| 目录 | 职责 |
|---|---|
| `web/` | 工作台 UI、API、任务编排与用户配置 |
| `pipeline/` | 字幕解析、来源处理、翻译与审校核心 |
| `download/` | 真实任务材料和结果；父目录“下载内容”直接指向这里 |
| `data/` | 术语、翻译记忆与 Prompt；禁止当缓存清理 |
| `tests/` | 自动化测试 |
| `docs/` | 当前规范、交接与历史说明 |
| `tools/` | 构建、诊断与验收工具 |
| `.venv/`、`.runtime_deps/` | 本机运行依赖，不提交 Git |

## 路径兼容

- 父目录启动和停止脚本调用 `程序文件\subtitle_pipeline\start_web.bat` 与 `stop_web.bat`。
- 仓库自身的启动脚本仍以脚本所在目录为项目根，因此源码 checkout 和便携包行为不变。
- `下载内容`使用 NTFS Junction，移动或删除它不会更改 Git 历史；重建时目标必须指向现役仓库的 `download/`。
- 历史 `.sp_*` 快照只作证据保留，不是现役启动入口。

## 修复用户入口

仓库已经位于 `cc\程序文件\subtitle_pipeline` 时，可在 PowerShell 中运行：

```powershell
.\tools\setup_cc_layout.ps1 -CcRoot <cc目录> -ProjectRoot <现役仓库目录>
```

脚本只会创建或覆盖两个入口批处理文件，并在不存在时创建 `下载内容` Junction；若已有同名路径指向其他位置会立即停止，不会移动或删除其中数据。
