#!/usr/bin/env bash
# check.sh —— 一条命令跑完这个项目的检查；也负责把服务拉起来给人手动点。
#
# 分工：acceptance.py 判「算得对不对」（业务），这里判「写得对不对」（工程）。
# 用法: ./check.sh            全部检查
#       ./check.sh --fast     跳过验收，只跑工程检查
#       ./check.sh --seal     重封考卷指纹（只该需求方执行）
#       ./check.sh --run [端口]  后台起服务，默认 8080
#       ./check.sh --stop     停掉 --run 起的服务
# 退出码: 0 通过 / 1 有失败 / 2 用法错误

set -u
cd "$(dirname "$0")" || exit 2

FAST=0
SEAL=0
MODE="check"
PORT=8080
PIDFILE=".app.pid"
LOG=".app.log"

case "${1:-}" in
    --fast) FAST=1 ;;
    --seal) SEAL=1 ;;
    --run)
        MODE="run"
        PORT="${2:-8080}"
        case "$PORT" in
            ''|*[!0-9]*) echo "端口必须是数字: $PORT"; exit 2 ;;
        esac
        ;;
    --stop) MODE="stop" ;;
    "") ;;
    *) echo "用法: $0 [--fast|--seal|--run [端口]|--stop]"; exit 2 ;;
esac

running() {
    [ -f "$PIDFILE" ] || return 1
    pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    # 防 pid 复用：确认这个 pid 确实是我们的 app.py，不是别的进程
    ps -o args= -p "$pid" 2>/dev/null | grep -q 'app\.py' || return 1
    return 0
}

start_app() {
    if running; then
        echo "已经在运行: pid $(cat "$PIDFILE")"
        echo "先 ./check.sh --stop 再起新的"
        exit 1
    fi
    rm -f "$PIDFILE"
    : >"$LOG"
    setsid nohup python3 project/app.py --port "$PORT" >"$LOG" 2>&1 </dev/null &
    pid=$!
    echo "$pid" >"$PIDFILE"

    for _ in $(seq 1 50); do
        grep -q '^LISTENING' "$LOG" 2>/dev/null && break
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
    done

    if ! grep -q '^LISTENING' "$LOG" 2>/dev/null; then
        if grep -q 'Address already in use' "$LOG" 2>/dev/null; then
            echo "启动失败: 端口 $PORT 已被占用，换一个，例如 ./check.sh --run 18081"
        else
            echo "启动失败，日志："
            sed 's/^/  /' "$LOG" 2>/dev/null | tail -10
        fi
        rm -f "$PIDFILE"
        exit 1
    fi

    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -z "$ip" ] && ip=127.0.0.1
    echo "已启动: pid $pid，端口 $PORT"
    echo "本机访问:   http://127.0.0.1:$PORT/"
    echo "局域网访问: http://$ip:$PORT/"
    echo "日志: $LOG"
    echo "停止: ./check.sh --stop"
    echo
    echo "提醒: 页面上传会覆盖 project/data/leave.csv，要留样本就先自己备份。"
    exit 0
}

stop_app() {
    if ! running; then
        echo "没有在运行（没有可用的 $PIDFILE）"
        rm -f "$PIDFILE"
        exit 0
    fi
    pid=$(cat "$PIDFILE")
    kill -TERM "$pid" 2>/dev/null
    for _ in $(seq 1 50); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -KILL "$pid" 2>/dev/null
        echo "已强制结束: pid $pid"
    else
        echo "已停止: pid $pid"
    fi
    rm -f "$PIDFILE"
    exit 0
}

[ "$MODE" = "run" ] && start_app
[ "$MODE" = "stop" ] && stop_app

FAIL=0
RUN=0
TMP=".check.out"

export PYTHONDONTWRITEBYTECODE=1
rm -rf __pycache__ project/__pycache__
step()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()    { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()   { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
skip()  { printf '  \033[36mSKIP\033[0m %s\n' "$*"; }
cleanup() { rm -f "$TMP"; rm -rf __pycache__ project/__pycache__; }
trap cleanup EXIT

# ---------------------------------------------------------------- 1. 语法
step "语法"
RUN=$((RUN + 1))
if python3 -m compileall -q acceptance.py project -x '(__pycache__)' >"$TMP" 2>&1; then
    ok "compileall"
else
    bad "compileall"
    sed 's/^/      /' "$TMP" | tail -10
fi

# ---------------------------------------------------------------- 2. 验收
if [ "$FAST" -eq 0 ]; then
    step "验收"
    RUN=$((RUN + 1))
    python3 acceptance.py >"$TMP" 2>&1
    code=$?
    if [ "$code" -eq 0 ]; then
        ok "$(tail -1 "$TMP" | tr -d '= ')"
    else
        if [ "$code" -eq 2 ]; then
            bad "acceptance.py 未通过（还没有实现）"
        else
            bad "acceptance.py 未通过"
        fi
        sed 's/^/      /' "$TMP" | tail -15
    fi
fi

# ---------------------------------------------------------------- 3. 考卷指纹
step "考卷指纹"
RUN=$((RUN + 1))
if [ "$SEAL" -eq 1 ]; then
    sha256sum acceptance.py project/data/expected.csv >.acceptance.sha256
    ok "已重封基线（acceptance.py + expected.csv）"
elif [ ! -f .acceptance.sha256 ]; then
    skip "缺 .acceptance.sha256（跑 ./check.sh --seal 建立基线）"
elif sha256sum -c .acceptance.sha256 >"$TMP" 2>&1; then
    ok "考卷未被改动"
else
    bad "考卷被改过！"
    sed 's/^/      /' "$TMP"
fi

# ---------------------------------------------------------------- 4. 样本数据
step "样本数据"
RUN=$((RUN + 1))
if python3 - "$TMP" >"$TMP" 2>&1 <<'PY'
import csv
import sys

sys.path.insert(0, "project")
import leave

records = leave.load_records("project/data/leave.csv")
with open("project/data/expected.csv", encoding="utf-8-sig", newline="") as handle:
    rows = list(csv.DictReader(handle))
expected = {int(row["编号"]) for row in rows}
missing = set(range(1, 17)) - expected
if missing:
    print(f"expected.csv 缺编号: {sorted(missing)}")
    sys.exit(1)
print(f"{len(records)} 名员工，{len(rows)} 条期望")
PY
then
    ok "$(tail -1 "$TMP")"
else
    bad "样本数据读不了"
    sed 's/^/      /' "$TMP" | tail -5
fi

# ---------------------------------------------------------------- 5. 工程卫生
step "工程卫生"

RUN=$((RUN + 1))
big=$(find . -path ./.git -prune -o -type f -size +20M -print 2>/dev/null)
if [ -z "$big" ]; then ok "无大文件 (>20MB)"; else bad "有大文件"; echo "$big" | sed 's/^/      /'; fi

RUN=$((RUN + 1))
hits=$(grep -rnE --include=*.py 'except[[:space:]]*:|except[[:space:]]+Exception' project 2>/dev/null \
       | grep -v __pycache__)
if [ -z "$hits" ]; then ok "无裸 except"; else bad "有裸 except"; echo "$hits" | sed 's/^/      /'; fi

RUN=$((RUN + 1))
# 实现里只允许两条输出：启动时的 LISTENING 和启动失败的数据错误
hits=$(grep -rn --include=*.py 'print(' project 2>/dev/null \
       | grep -v __pycache__ | grep -v 'LISTENING' | grep -v '数据错误')
if [ -z "$hits" ]; then ok "实现里无多余 print"; else bad "实现里有 print"; echo "$hits" | sed 's/^/      /'; fi

RUN=$((RUN + 1))
if [ -r project/key.txt ]; then
    KEY=$(tr -d '[:space:]' <project/key.txt)
else
    KEY=""
fi
if [ -z "$KEY" ]; then
    bad "project/key.txt 缺失或为空"
else
    hits=$(grep -rn --include=*.py --include=*.md --include=*.sh -F "$KEY" . 2>/dev/null \
           | grep -v 'key.txt' || true)
    if [ -z "$hits" ]; then ok "key 只存在 key.txt 里"; else bad "key 被写进了代码/文档"; echo "$hits" | sed 's/^/      /'; fi
fi

RUN=$((RUN + 1))
# 上传是唯一写入口：os.replace 只出现一次，且必须在 app.py
hits=$(grep -rn --include=*.py 'os\.replace' project 2>/dev/null | grep -v __pycache__ || true)
count=$(printf '%s' "$hits" | grep -c 'os\.replace' || true)
if [ "$count" -eq 1 ] && printf '%s' "$hits" | grep -q '^project/app.py:'; then
    ok "写入口只有 app.py 的 os.replace"
else
    bad "写入口不唯一（os.replace 出现 $count 次）"
    printf '%s\n' "$hits" | sed 's/^/      /'
fi

RUN=$((RUN + 1))
hits=$(grep -nE "open\(.*['\"][wa]" project/leave.py 2>/dev/null || true)
if [ -z "$hits" ]; then ok "leave.py 不以写方式打开文件"; else bad "leave.py 里有写模式 open"; echo "$hits" | sed 's/^/      /'; fi

# ---------------------------------------------------------------- 汇总
printf '\n----------------------------------------\n'
printf '共 %d 项: %d 失败\n' "$RUN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
    printf '\033[31m结果: 不通过\033[0m\n'
    exit 1
fi
printf '\033[32m结果: 通过\033[0m\n'
