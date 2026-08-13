# ForTest

ForTest 是集成 PRD 测试用例生成与 Jenkins 快捷部署的纯原生 Windows x64 桌面端。
桌面端复用 `backend`、`services` 与 `utils` 中的业务核心，不启动 Streamlit、
FastAPI 或本地 Web 端口。旧 Web 入口与 PRD-to-CASE 插件均已退出活跃源码和交付链。

## 目录说明

- `windows_native/`：当前 ForTest 原生 Qt 桌面端、测试、打包及安装器脚本。
- `backend/`、`services/`、`utils/`：原生桌面端复用的业务核心。
- `tests/`：业务核心回归测试。
- `docs/`：面向开源维护者的长期文档；开发计划、验收记录和一次性交付报告不纳入 Git。

本机可在受忽略的 `预删除/` 中保留退役代码和过程记录的恢复副本。Codex 或其他开发助手的临时工作优先写入 `.codex-work/`；`docs/plans/`、`docs/validation/` 和 `windows_native/DELIVERY*.md` 也已显式屏蔽，避免过程材料误入提交。

## 开发与验证

```powershell
# 业务核心回归
windows_native\.build-venv\Scripts\python.exe -m pytest tests -q

# 原生端回归（首次完整构建后会生成隔离环境）
windows_native\.build-venv\Scripts\python.exe -m pytest windows_native\tests -q

# 生成 Windows x64 程序与安装包
powershell -ExecutionPolicy Bypass -File windows_native\build.ps1
```

完整构建会自动验证原生端和业务核心，检查源码/产物隐私与 PE 架构，并对打包后的程序执行
无端口启动诊断。安装包输出到 `windows_native/dist/installer/`。

运行时配置、任务记录、日志和生成文件保存在 `%LOCALAPPDATA%\ForTest\UserData`，
不写入源码目录，也不纳入 Git。

## 源码公开与协作

- 问题与建议：使用仓库的 GitHub Issues，并在提交前移除隐私数据。
- 代码贡献：参见 [`CONTRIBUTING.md`](CONTRIBUTING.md) 和
  [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。
- 安全漏洞：请按 [`SECURITY.md`](SECURITY.md) 使用 GitHub 私密报告入口，不要公开披露。
- 自动验证：推送和 Pull Request 会在 Windows 环境运行源码隐私门禁及两组回归测试。

ForTest 按 [PolyForm Noncommercial License 1.0.0](LICENSE) 公开源码：允许个人学习、
研究、测试及其他非商业用途，也允许在相同非商业边界内修改和分发。任何商业用途均须
事先取得 `wiksongkim-arch` 的单独书面许可。因此本项目属于 **source-available（源码公开）**，
不是 OSI 定义的开源软件。
