#!/bin/bash
set -u

URL="http://127.0.0.1:8611/healthz"
STATE_FILE="${XDG_RUNTIME_DIR:-/var/tmp}/qsim-watchdog-${UID}-failures"
umask 077
NTFY_URL="${QSIM_NTFY_URL:-http://127.0.0.1:2586/quant-sim}"
MAX_RESTARTS=3

fail_count=0
[ -f "$STATE_FILE" ] && fail_count=$(cat "$STATE_FILE" 2>/dev/null || echo 0)

if curl -sf -m 10 "$URL" > /dev/null 2>&1; then
    [ "$fail_count" != "0" ] && echo 0 > "$STATE_FILE"
    exit 0
fi

fail_count=$((fail_count + 1))
echo "$fail_count" > "$STATE_FILE"

# Do not enter an unbounded restart loop after the three-attempt budget.
if [ "$fail_count" -le "$MAX_RESTARTS" ]; then
    systemctl --user restart qsim-refactor.service 2>/dev/null || true
fi

if [ "$fail_count" -eq "$MAX_RESTARTS" ]; then
    msg="qsim API 连续 ${fail_count} 次健康检查失败并已重启 ${MAX_RESTARTS} 次，需要人工介入
host: $(hostname)
time: $(date '+%F %T')"
    curl -sf -m 8 -d "$msg" "$NTFY_URL" > /dev/null 2>&1 || true
fi

exit 0
