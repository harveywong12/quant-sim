# 公开发布边界

本发布采用独立副本和新的 Git 历史。发布准备不修改现有 NAS 服务、账本、任务或凭据。

保留：既有修复源码、策略算法、后端接口、网页静态资源、回归测试、无记录的数据库结构，以及依赖锁定文件。

适配：个人主目录改为通用备份路径；固定旧容器网段改为本机回环默认值，已有环境变量仍可覆盖；网络测试改用文档示例地址；私人成绩与晋级记录移出配置。迁移状态仅保留兼容性示例，并在文档中说明不能作为新账户验收证据。

排除：真实行情、持仓、净值、成交、私人研究报告、运行摘要、NAS 操作与管理员脚本、截图、日志、SSH 信息、环境文件、备份及旧审计输出。删除旧的自动切换脚本，因为它会尝试开启应用内部调度，与此版本的 systemd 唯一调度示例冲突。

新增：通用部署示例、离线测试入口、发布检查器，以及第三方静态库的完整许可证。

## 后续上传

```bash
python tools/check_publication.py
git add README.md docs qsim tests config scripts migrations schemas audit_tools tools deploy pyproject.toml requirements*.txt THIRD_PARTY_NOTICES.md .gitignore
python tools/check_publication.py --staged
git diff --cached --stat
```

检查器检查文件类型、个人主目录、私人网段、疑似凭据及私钥，并只输出文件名与问题类别。`--staged` 检查 Git 暂存的实际内容，也能发现被强制加入的忽略文件。检查器不替代人工审阅，不能保证发现所有个人信息或所有凭据。

个人标识可通过 `--deny-file` 提供一个仅在本地保存的 JSON 字符串数组，不把该文件提交到仓库。不要将密码、Token 或私钥填入命令行、报告或公开 Issue。Git 提交身份也应使用通用名称和非个人邮箱。

第三方版权姓名与许可证必须保留；它们不属于本项目拥有者的私人信息。仓库拥有者的公开 GitHub 用户名仍会出现在仓库地址中。
