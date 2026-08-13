# 文档维护边界

`docs/` 只提交对用户和源码公开维护者长期有效的说明，例如架构、安全边界、开发环境和发布流程规范。

当前长期文档：

- [`security/package-privacy.md`](security/package-privacy.md)：运行时数据、安装包隐私门禁和公开仓库边界。

以下内容属于本机开发过程材料，不进入 Git：

- `docs/plans/`：Codex 或人工生成的日期化迭代计划；
- `docs/validation/`：一次性测试、安装和人工验收记录；
- `docs/reports/`：扫描、对比和临时分析报告；
- `docs/superpowers/`：开发助手执行草案；
- `docs/security/open-source-privacy-audit-*.md`：针对某次工作区状态的一次性隐私盘点；
- `windows_native/DELIVERY*.md`：具体版本的本机交付快照。

需要短期保留的历史记录应移动到受忽略的 `预删除/development-records/`；开发助手的草稿和中间文件优先放在 `.codex-work/`。构建、测试和隐私门禁的机器可读结果统一保存在 `windows_native/.build/`。

不要使用 `git add -f` 强制加入上述目录；提交前使用 `git status --ignored` 检查边界。
