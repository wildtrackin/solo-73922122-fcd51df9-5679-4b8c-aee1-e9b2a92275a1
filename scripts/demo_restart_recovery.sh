#!/usr/bin/env bash
# 工作进程异常退出 → 同机原数据目录重启 → 旧批回收 的端到端演示。
#
#   scripts/demo_restart_recovery.sh          本地模式（uvicorn 直接跑）
#   scripts/demo_restart_recovery.sh docker   Docker Compose 模式
#
# 演示步骤（两种模式相同）：
#   1. 启动服务并上传缓存失真样例，确认进入 running（soffice 进程在跑）；
#   2. 强制结束 API 工作进程（本地用演示端点 os._exit，docker 用 pkill -9），
#      孤儿 LibreOffice 进程仍在运行；
#   3. 用原数据目录重启服务；
#   4. 观察旧批 recovering →（确认进程退出、暂存清除）→ interrupted，
#      名额从占用到归还，暂存目录不复活；
#   5. 上传新批，确认已释放名额可正常处理。
set -u
cd "$(dirname "$0")/.."
MODE="${1:-local}"
PORT="${PORT:-8000}"
CT="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BASE="http://127.0.0.1:$PORT"

say()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }

wait_health() {  # $1=说明 $2=超时秒 $3=服务进程PID（可选，用于早夭检测）
  local t=0
  while ! curl -fsS "$BASE/health" >/dev/null 2>&1; do
    if [ -n "${3:-}" ] && [ ! -d "/proc/$3" ]; then
      info "服务进程已退出（端口被占用？），日志："
      tail -5 /tmp/demo-server*.log 2>/dev/null | sed 's/^/     /'
      return 1
    fi
    t=$((t+1)); [ "$t" -gt "$2" ] && { info "等待服务就绪超时"; return 1; }
    sleep 1
  done
  info "$1：服务已就绪"
}

upload_sample() {  # 输出 task_id
  curl -fsS -X POST "$BASE/tasks" \
    -F "file=@samples/cache_drift_sample.xlsx;type=$CT" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["task_id"])'
}

task_status() {
  curl -fsS "$BASE/tasks/$1" 2>/dev/null | python3 -c \
    'import json,sys; d=json.load(sys.stdin); print(d["status"])' 2>/dev/null
}

watch_task() {  # $1=task_id $2=超时秒：打印每次状态变化，终态时返回
  local last="" now t=0
  while [ "$t" -lt "$2" ]; do
    now=$(task_status "$1")
    if [ -n "$now" ] && [ "$now" != "$last" ]; then
      info "任务 $1 状态：$now（slots_in_use=$(curl -fsS "$BASE/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slots_in_use"])')）"
      last="$now"
    fi
    case "$now" in
      completed|failed|cancelled|interrupted) return 0;;
    esac
    t=$((t+1)); sleep 1
  done
  info "等待终态超时"; return 1
}

wait_recalc_process() {  # $1=task_id $2=查找命令模板（含 %s）
  local t=0
  while [ "$t" -lt 15 ]; do
    if eval "$(printf "$2" "$1")" >/dev/null 2>&1; then return 0; fi
    t=$((t+1)); sleep 1
  done
  return 1
}

say "0. 前置检查：/docs 可用性"
if [ "$MODE" != "docker" ]; then
  command -v python3 curl >/dev/null || { echo "需要 python3 与 curl"; exit 1; }
fi

if [ "$MODE" = "docker" ]; then
  say "1. Docker Compose 启动（监督循环 + tini，复现同机重启）"
  docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build || exit 1
  wait_health "compose" 120 || exit 1
  info "/docs 状态码：$(curl -s -o /dev/null -w '%{http_code}' "$BASE/docs")"
  FIND_RECALC='docker compose -f docker-compose.yml -f docker-compose.demo.yml exec -T verify-api pgrep -f staging/%s'
else
  say "1. 本地启动 API（演示进程，含模拟崩溃端点）"
  if curl -fsS "$BASE/health" >/dev/null 2>&1; then
    echo "端口 $PORT 已被其它进程占用，请先停止再演示（或用 PORT= 换端口）" >&2
    exit 1
  fi
  export DATA_DIR="${DATA_DIR:-$(mktemp -d /tmp/verify-demo-XXXXXX)}"
  export RECOVERY_TERM_GRACE_SECONDS=2 RECOVERY_KILL_TIMEOUT_SECONDS=4 \
         RECOVERY_MAX_ROUNDS=2 RECOVERY_SLOW_RETRY_INTERVAL_SECONDS=1 \
         RECOVERY_VERIFY_DELAY_SECONDS=0.3
  info "数据目录：$DATA_DIR"
  PYTHONPATH="$PWD" python3 scripts/demo_server.py >/tmp/demo-server.log 2>&1 &
  SRV_PID=$!
  wait_health "本地" 60 "$SRV_PID" || exit 1
  info "/docs 状态码：$(curl -s -o /dev/null -w '%{http_code}' "$BASE/docs")"
  FIND_RECALC='pgrep -f staging/%s'
fi

say "2. 上传缓存失真样例并确认重算进程在跑"
TASK_ID="$(upload_sample)" || { echo "上传失败"; exit 1; }
info "task_id=$TASK_ID 状态=$(task_status "$TASK_ID")"
if wait_recalc_process "$TASK_ID" "$FIND_RECALC"; then
  info "重算进程已出现（命令行含该批暂存路径）"
else
  info "未等到重算进程（机器太快？），仍继续演示"
fi

say "3. 强制结束 API 工作进程（孤儿计算进程被遗弃）"
if [ "$MODE" = "docker" ]; then
  docker compose -f docker-compose.yml -f docker-compose.demo.yml \
    exec -T verify-api pkill -9 -f 'uvicorn app.main' || true
else
  curl -fsS -X POST "$BASE/__demo_crash" 2>/dev/null || true
fi
sleep 1
if curl -fsS "$BASE/health" >/dev/null 2>&1; then
  if [ "$MODE" = "docker" ]; then
    info "注意：服务仍可访问（监督循环已重启工作进程）"
  else
    echo "崩溃端点未生效（服务仍可访问），演示中止" >&2; exit 1
  fi
else
  info "API 已不可访问，工作进程确已退出"
fi
if eval "$(printf "$FIND_RECALC" "$TASK_ID")" >/dev/null 2>&1; then
  info "确认：旧批计算进程仍在运行（孤儿）"
else
  info "旧批计算进程已自行退出（转换较快），继续演示回收流程"
fi

if [ "$MODE" != "docker" ]; then
  say "4. 用原数据目录重启服务（真实应用，无演示端点）"
  PYTHONPATH="$PWD" python3 scripts/demo_server.py >/tmp/demo-server2.log 2>&1 &
  SRV_PID=$!
  wait_health "重启后" 60 "$SRV_PID" || exit 1
else
  say "4. 监督循环已用原数据目录重启工作进程"
  wait_health "重启后" 60 || exit 1
fi

say "5. 观察旧批回收：recovering → interrupted"
watch_task "$TASK_ID" 90 || exit 1
info "最终记录："
curl -fsS "$BASE/tasks/$TASK_ID" | python3 -m json.tool | grep -E '"(status|error_code|error_message|samples)"' | sed 's/^/     /'

say "6. 确认无残留：进程、暂存、名额"
sleep 2
if eval "$(printf "$FIND_RECALC" "$TASK_ID")" >/dev/null 2>&1; then
  info "失败：旧批计算进程仍在运行！"; exit 1
else
  info "旧批计算进程已不存在"
fi
if [ "$MODE" = "docker" ]; then
  docker compose -f docker-compose.yml -f docker-compose.demo.yml \
    exec -T verify-api ls -A /data/staging
else
  ls -A "$DATA_DIR/staging"
fi | grep -q . && { info "失败：暂存目录未清空（或已复活）"; exit 1; } \
  || info "暂存目录为空且未复活"
info "健康检查：$(curl -fsS "$BASE/health")"

say "7. 已释放名额处理新批"
NEW_TASK="$(upload_sample)"
info "新批 task_id=$NEW_TASK"
watch_task "$NEW_TASK" 60 || exit 1
curl -fsS "$BASE/tasks/$NEW_TASK" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print("   新批状态：", d["status"], "| 失真样本：", d["summary"]["drift_pass_to_fail"])'

say "演示完成"
if [ "$MODE" = "docker" ]; then
  info "清理：docker compose down"
  docker compose -f docker-compose.yml -f docker-compose.demo.yml down
else
  info "清理：结束演示服务进程"
  curl -fsS -X POST "$BASE/__demo_crash" 2>/dev/null || true
fi
