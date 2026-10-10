# systemd 部署示例

这些文件是通用示例，没有在任何使用者的 NAS 上安装或启动。本次公开发布没有修改已有部署。

## 环境与路径

示例代码根目录为 `/srv/qsim`，备份根目录为 `/srv/qsim-backups`，运行用户为专用非 root 的 `qsim`。安装前根据实际目录调整 unit、权限与解释器路径。Python 环境按 README 的锁定依赖建立，静态资源已随源码提供。

`deploy/runtime.env.example` 不含任何密钥。复制到本地 `deploy/runtime.env` 并设为仅运行用户可读；Token 通过权限为 600 的独立文件提供。Tushare HTTPS 地址必须先在本地确认真实认证、证书和接口兼容，示例没有猜测一个可工作的地址。

网页 unit 默认仅监听回环地址。`QSIM_SCHEDULER_ENABLED=0` 和 `QSIM_SCHEDULE_OWNER=systemd` 禁止网页启动内部调度。采集和自动模拟默认关闭，不运行长期开发服务器。

## 数据任务

保留既有 `python -m qsim.services.data_reconcile` 业务入口，timer 是唯一调度责任方。模板不会自动安装或启用。只有在本地完成凭据、权限、数据库、交易日历、HTTPS 与数据覆盖检查后，才配置 `QSIM_NATIVE_MARKET=1`；否则不要启用此 timer，它可能走需要本地旧系统的迁移适配入口。

数据更新和模拟结算共用已有锁、质量检查与幂等流程。首次只设置 `QSIM_PAPER_AUTORUN=0` 采集；正式模拟还需独立账本验收、费用配置与恢复核对，再设置 `QSIM_FORMAL_PAPER=1`、`QSIM_PAPER_AUTORUN=1`。不能用十年收益目标代替工程验收，也不以收益目标作为采集前置条件。

更换运行用户或目录后，用本机 `systemd-analyze verify` 校验模板，并核对所有旧 timer、容器调度及应用调度，确保同一业务只有一个入口。不要复制到另一套 Compose 调度同时执行。

## 可选内网只读网页

已有受限访问包装器 `scripts/lan_api_entry.py` 可替换网页 unit 的 ExecStart，不创建第二个网页进程。它只绑定回环与显式指定的内网地址，只允许指定客户端读取，拒绝内网写操作、AI 请求与跨站读取。

```text
/srv/qsim/.venv/bin/python scripts/lan_api_entry.py --lan-ip <NAS内网IPv4> --client-ip <获准客户端IPv4> --port 8611
```

参数必须由部署者本地填写，不能提交真实内网地址。此入口仍为 HTTP，只适合受控可信内网，不应转发到公网；更复杂访问需求需另行配置经过验收的认证与 TLS 入口。没有 LAN 参数时继续用默认回环网页。

## 备份与回滚

既有备份入口：`scripts/backup-quant-db.sh`，调用 `qsim.services.recovery backup`。先在本地检查容量、目标权限和备份清单。恢复包可能包含凭据和账户，必须保持私有，不上传 GitHub。

升级前保存代码、配置和一致性账本备份，记录当前服务入口。回滚源码时恢复上一版本代码与对应配置，先离线验证，再由管理员按原部署方式切换网页；不要用旧账本覆盖新成交。需要数据恢复时只在隔离目录演练并独立核对，不能直接覆盖运行中的数据库。

示例的 CPU、内存与线程限制需要现场调整，不代表 8GB NAS 的资源实测。日志使用 journald，保留与脱敏策略由本机配置，不能公开上传业务日志。
