# 参与贡献

感谢你愿意改进 ForTest。为便于维护和审查，请遵循以下约定。

## 开始之前

1. 先搜索现有 Issue，确认问题或需求尚未被记录。
2. 对较大的功能或行为变更，先创建 Issue 说明目标、使用场景和兼容性影响。
3. 不要提交 API Key、Token、Cookie、真实业务地址、客户数据、个人绝对路径或本机配置。

## 本地开发

ForTest 面向 Windows x64，建议使用 Python 3.13 和 PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r windows_native\requirements-build.txt -r requirements.txt
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\python.exe -m pytest windows_native\tests -q
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe windows_native\package_privacy.py source --project-root .
```

完整构建和安装包验证：

```powershell
powershell -ExecutionPolicy Bypass -File windows_native\build.ps1
```

## 提交 Pull Request

- 每个 Pull Request 聚焦一个问题，说明修改原因、主要行为和验证结果。
- 新功能或缺陷修复应包含对应测试；界面修改建议附截图。
- `README.md` 与 `README.en.md` 是必须同步维护的中英文版本。修改任一文件时，应在同一次提交中更新另一文件，并同步检查对应语言的截图、链接、版本号和能力边界。
- 保持提交信息简洁明确，例如 `fix: prevent duplicate deployment tasks`。
- 确认测试、隐私门禁和 GitHub Actions 全部通过。

提交贡献即表示你同意按项目的
[PolyForm Noncommercial License 1.0.0](LICENSE) 发布这些修改，并遵守
[行为准则](CODE_OF_CONDUCT.md)。商业使用仍须取得维护者的单独书面许可。
