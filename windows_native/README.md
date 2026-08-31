# ForTest 原生 Windows 桌面端

ForTest 是独立于原网页版的纯原生 Windows x64 工程。界面使用 Qt Widgets，
业务逻辑直接调用项目既有 `backend`、`services` 与 `utils` 模块，不启动
FastAPI、Uvicorn、Streamlit、WebView，也不监听本地端口。

## 当前版本

- 版本号：`0.2.15`
- 后续每次发布固定在前一版本基础上递增 `0.0.1`，例如 `0.2.13`、`0.2.14`、`0.2.15`
- 外观模式：跟随系统、浅色、深色
- 任务模式：持久化任务列表与可配置的并行执行队列
- 本地需求：支持 Markdown、TXT、DOCX、PDF、XLSX，并与在线文档共用生成流程
- 默认模板：用户目录保存可查看副本，程序内置母版可经确认后安全恢复
- 快捷部署：支持分钟间隔或每日 `HH:mm` 定时触发；迭代任务串行，任务内子任务并行跟踪
- 启动响应：品牌启动页先显示并持续处理窗口消息；backend/Codex、Jenkins/Keyring、
  本地任务和首屏快照全部预热并注入完成后才显示主界面
- 控件体验：定时范围统一显示为 `yyyy-MM-dd HH:mm`，全局下拉框按文案自适应并使用
  统一的浅色/深色交互样式
- Codex 运行时：CLI 与 SDK app-server 独立查看、下载、校验和切换版本

## 架构

- `main.py`：原生进程入口、单实例和启动诊断。
- `ui/startup_splash.py`：品牌启动页、统一启动协调器、完整就绪快照与响应性门禁。
- `native_service.py`：把已有业务函数封装为 Qt 可调用门面。
- `task_manager.py`：任务持久化、排队、并行执行和回收站。
- `desktop_preferences.py`：原生端外观与任务调度偏好。
- `process_policy.py`：只在原生进程内隐藏 Codex SDK、CLI、Git 等子进程窗口。
- `ui/`：测试用例生成、集中配置、任务回收站、AI 配置与外观页面。

原生端统一使用 `%LOCALAPPDATA%\ForTest\UserData` 保存配置、任务、日志与输出。
首次启动会优先从 `%LOCALAPPDATA%\QAQ\UserData`，再依次从
`%LOCALAPPDATA%\ForTester\UserData` 和 `%LOCALAPPDATA%\PRDtoCASE`
安全迁移可复用数据；旧目录仍保留，
因此不会改变原网页版的数据或运行状态。

## 构建

在 64 位 Windows PowerShell 中执行：

```powershell
.\windows_native\build.ps1 -Clean
```

最终安装包位于：

```text
windows_native\dist\installer\ForTest-Windows-x64-Setup-0.2.15.exe
```

构建生成的诊断、隐私审计和安装日志保存在本机 `windows_native/.build/`。
日期化验收记录与 `DELIVERY*.md` 属开发过程材料，已由 Git 忽略，不作为产品源码交付。
