#!/bin/sh
# clawbot-companion 自检总入口：逐套顺序运行（不并行，避免计时类用例互相干扰），
# 汇总每套结果；任一失败整体退出码非 0。
# 用法：sh tests/run_all.sh
set -u
cd "$(dirname "$0")"
export TZ=Asia/Shanghai
export PYTHONUNBUFFERED=1

SUITES="
selftest_audio_screen.py
selftest_env_smoke.py
selftest_tune.py
selftest_jailbreak_guard.py
selftest_stage5.py
selftest_daily_look.py
selftest_brief_battery.py
selftest_burst.py
selftest_calendar.py
selftest_proactive.py
selftest_life.py
selftest_worker.py
selftest_pressure.py
selftest_channel.py
selftest_round28.py
"

LOGDIR="$(mktemp -d)"
trap 'rm -rf "$LOGDIR"' EXIT
PASS_N=0
FAIL_N=0
FAILED=""
for s in $SUITES; do
    log="$LOGDIR/$s.log"
    printf '== %s ... ' "$s"
    if timeout 1800 python3 "$s" >"$log" 2>&1; then
        summary="$(grep -E 'PASS|通过|共 [0-9]+ 项' "$log" | tail -n 1)"
        echo "PASS  $summary"
        PASS_N=$((PASS_N + 1))
    else
        rc=$?
        echo "FAIL (exit=$rc)"
        echo "---- $s 输出尾部 ----"
        tail -n 30 "$log"
        echo "----------------------"
        FAIL_N=$((FAIL_N + 1))
        FAILED="$FAILED $s"
    fi
done
echo
echo "==== run_all: $PASS_N 套通过 / $FAIL_N 套失败 ===="
if [ "$FAIL_N" -gt 0 ]; then
    echo "失败套件:$FAILED"
    exit 1
fi
exit 0
