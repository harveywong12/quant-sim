# quant-sim

A 股低频量化研究与本地模拟账户系统。沿用 Python、FastAPI、SQLite 和 systemd；网页使用本地 ECharts 与 Tabler 静态资源。只支持回测和本地模拟，不连接真实券商，不下真实订单。

这是经过脱敏的源码发布，包含策略、数据接口、回测/模拟引擎、账本、网页、数据库结构和离线回归测试。没有行情库存、真实账户、私人回测报告、凭据或运行日志。表结构不代表历史数据覆盖，合成测试不能证明投资收益。

## 主要模块

| 环节 | 实现 |
|---|---|
| 数据 | `qsim/market/`、`qsim/services/native_market.py`：Tushare 接入、交易日历、覆盖检查、供应商修订 |
| 因子与策略 | `qsim/features/`、`qsim/strategies/`、`config/`：多因子版本、红利低波、ETF 动量及既有策略 |
| 回测与模拟 | `qsim/engine/`：订单、费用、成交、现金分配、指标 |
| 风控与账本 | `qsim/risk/`、`qsim/storage/`：风险限制、订单意图、成交与净值 |
| 网页 | `qsim/services/api/`、`qsim/services/web/`：FastAPI、中文工作台、运行/账户/持仓详情 |
| 运维 | `qsim/services/recovery.py`、`deploy/systemd/`：一致性备份、恢复与唯一调度入口示例 |

## 安装与离线测试

需要 Python 3.11 或更新版本。锁定文件记录已使用的依赖环境，不是当前最新版声明。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.runtime.lock.txt
.venv/bin/python tools/check_publication.py
.venv/bin/python tools/run_offline_tests.py
```

测试运行器创建临时工作目录，清空凭据环境，禁止外部网络连接，并暂停调度时钟。测试使用合成数据。需要真实数据库的测试会跳过，测试报告分别列出收集、执行、通过及跳过数量。完整命令与发布验证记录见 [验证说明](docs/VERIFICATION.md)。

## 网页启动

在源码根目录运行，保持采集和任务关闭：

```bash
export QSIM_SCHEDULER_ENABLED=0
export QSIM_SCHEDULE_OWNER=systemd
export QSIM_NATIVE_MARKET=0
export QSIM_PAPER_AUTORUN=0
export QSIM_FORMAL_PAPER=0
export QSIM_AUTH_TOKEN_FILE="$PWD/.secrets/web-token"
.venv/bin/python -m uvicorn qsim.services.api.app:create_app --factory --host 127.0.0.1 --port 8611 --workers 1 --no-access-log
```

此命令仅启动网页，不创建演示收益或采集行情。未准备数据库时，业务详情会显示缺失或无法读取；不能把 HTTP 可访问视为数据链路已经验收。写操作需要本地安全配置的认证，凭据文件不在仓库中。

部署与可选的受限内网只读入口见 [部署说明](docs/DEPLOYMENT.md)。不要直接替换已有运行目录或同时开启多套调度。

## 数据与结果边界

- `schemas/` 仅保存已有 SQLite 结构，没有记录、持仓或历史行情。它们是迁移参考，不是完整初始化流程；当前模块还会创建各自的 `qsim_*` 表。
- `config/strategy-migration.json` 是兼容性状态示例，不包含私人晋级证据，不代表新安装已经获准运行策略。策略版本日期是源码契约的一部分，不能用来推断账户曾经交易。
- 公开配置删除旧收益数字，十年净 CAGR ≥10%、每日组合净收益年化夏普 ≥1 的研究目标为 **NOT_VERIFIED**。需要真实数据、适用成本和可复算证据独立验证。
- 当前资料是既有项目的修复源码导出，并非完整旧生产系统。旧系统适配入口需要使用者自行提供本地依赖；原生数据模式仍需确认 Tushare 权限、HTTPS 接口、覆盖、复权与财务时点。
- 保留历史策略与可选 AI 适配源码不代表启用它们。发布测试不使用付费模型，网页资源不依赖 CDN。

## 公开发布与许可证

下次发布前运行 `tools/check_publication.py` 并检查暂存区。凭据、行情库、账户、截图、私人报告、备份和环境文件均应留在仓库外，参见 [发布边界](docs/PUBLICATION.md)。

第三方 ECharts、Tabler 及其子组件的版权和许可证见 [第三方声明](THIRD_PARTY_NOTICES.md)。本次没有为项目自有代码新增许可证授权；公开可见不等于授予 MIT/Apache 使用许可。
