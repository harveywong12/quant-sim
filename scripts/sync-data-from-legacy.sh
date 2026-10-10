#!/usr/bin/env bash
# 从旧系统（quant-sim 生产库）导入市场数据到 qsim。
# 说明: 旧库正被生产容器写入；适配器先生成一致 staging 快照，
#       再在 qsim 目标库内事务性导入，保留所有 qsim_* 业务表。
set -euo pipefail

REFACTOR_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$REFACTOR_ROOT/.venv/bin/python" -m qsim.market.legacy_adapter
