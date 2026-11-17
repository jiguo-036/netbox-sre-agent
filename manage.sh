#!/usr/bin/env bash
# NetBox SRE Agent 管理脚本
# 用法：./manage.sh start|stop|restart|status|logs|test
set -euo pipefail

# 脚本所在目录即项目根，不受调用位置影响
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$ROOT/agent"
VENV="$ROOT/.venv"
PID_FILE="$ROOT/agent.pid"
LOG_FILE="$ROOT/agent.log"
PORT="${PORT:-8001}"
HOST="${HOST:-0.0.0.0}"

# 加载虚拟环境
activate() {
  if [[ -f "$VENV/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
  else
    echo "❌ 找不到虚拟环境：$VENV" >&2
    exit 1
  fi
}

# 检查端口是否被占用
port_busy() {
  command -v ss >/dev/null 2>&1 && ss -ltn "sport = :$PORT" | grep -q LISTEN
}

# 启动前探测模型是否可用，失败则中止启动
probe_model() {
  echo "🔍 探测模型可用性..."
  cd "$AGENT_DIR"
  if python - <<'PY'
import os
import sys
import loop  # 复用 loop.py 对项目根 .env 的加载
from openai import OpenAI

key = os.getenv("OPENAI_API_KEY", "")
base = os.getenv("OPENAI_BASE_URL") or None
model = os.getenv("MODEL_NAME", "gpt-4o-mini")
if not key:
    print("❌ OPENAI_API_KEY 未配置", file=sys.stderr)
    sys.exit(1)

client = OpenAI(api_key=key, base_url=base)

def call(**kwargs):
    return client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        timeout=20,
        **kwargs,
    )

last_error = None
# 兼容不同网关：普通模型用 max_tokens，推理模型可能只认 max_completion_tokens
for kwargs in ({"max_tokens": 1}, {"max_completion_tokens": 1}, {}):
    try:
        call(**kwargs)
        break
    except Exception as e:
        last_error = e
else:
    print(f"❌ 模型调用失败：{type(last_error).__name__}: {last_error}", file=sys.stderr)
    sys.exit(1)

print(f"✅ 模型可用：{model}")
PY
  then
    return 0
  fi
  echo "❌ 模型不可用，Agent 未启动。请检查 .env 的 Key / BaseURL / 模型名或网络。" >&2
  exit 1
}

cmd_start() {
  activate
  cd "$AGENT_DIR"

  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "✅ 已在运行（PID $(cat "$PID_FILE")），无需重复启动"
    return 0
  fi

  if port_busy; then
    echo "❌ 端口 $PORT 已被其他进程占用，请先释放或换端口：" >&2
    echo "   PORT=8002 $0 start" >&2
    exit 1
  fi

  echo "🚀 启动 agent（端口 $PORT）..."
  nohup uvicorn app:app --host "$HOST" --port "$PORT" \
    > "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"

  # 等待就绪
  for i in $(seq 1 15); do
    sleep 1
    if curl -s -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then
      echo "✅ 启动成功（PID $(cat "$PID_FILE")）"
      echo "   访问：http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
      echo "   日志：$LOG_FILE"
      return 0
    fi
  done

  echo "❌ 启动超时，最后 30 行日志：" >&2
  tail -30 "$LOG_FILE" >&2
  exit 1
}

cmd_stop() {
  if [[ ! -f "$PID_FILE" ]]; then
    echo "ℹ️  未找到 PID 文件，尝试按端口清理..."
    pkill -f "uvicorn app:app" 2>/dev/null && echo "✅ 已清理" || echo "ℹ️  没有运行中的进程"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    for i in $(seq 1 10); do
      sleep 1
      kill -0 "$pid" 2>/dev/null || break
    done
    kill -9 "$pid" 2>/dev/null || true
    echo "✅ 已停止（PID $pid）"
  else
    echo "ℹ️  进程 $pid 已不存在"
  fi
  rm -f "$PID_FILE"
}

cmd_status() {
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "✅ 运行中（PID $pid）"
    echo "   端口：$PORT"
    echo "   内存：$(ps -o rss= -p "$pid" 2>/dev/null | awk '{printf "%.1f MB", $1/1024}')"
    echo "   运行：$(ps -o etime= -p "$pid" 2>/dev/null | xargs)"
  else
    echo "⛔ 未运行"
  fi
}

cmd_logs() {
  if [[ -f "$LOG_FILE" ]]; then
    tail -f "$LOG_FILE"
  else
    echo "ℹ️  日志文件不存在：$LOG_FILE"
  fi
}

# 冒烟测试：检查 .env、模块导入、NetBox Token 与模型可用性
cmd_test() {
  activate
  cd "$AGENT_DIR"
  echo "🔍 检查环境..."
  python - <<'PY'
import os, sys
from pathlib import Path
import loop  # 触发项目根 .env 加载
from tools import api_probe
key = os.getenv("OPENAI_API_KEY", "")
url = os.getenv("OPENAI_BASE_URL", "")
model = os.getenv("MODEL_NAME", "")
token = os.getenv("NETBOX_TOKEN", "")
print(f"  KEY   : {key[:8]}...{key[-4:] if len(key) > 12 else '(空)'}")
print(f"  URL   : {url or '(空)'}")
print(f"  MODEL : {model or '(空)'}")
print(f"  NBURL : {os.getenv('NETBOX_URL') or '(空)'}")
print(f"  TOKEN : {token[:8]}...{token[-4:] if len(token) > 12 else '(空)'}")
if not key:
    print("❌ OPENAI_API_KEY 未加载，检查 agent/.env 或项目根 .env")
    sys.exit(1)
if not url:
    print("❌ OPENAI_BASE_URL 未加载")
    sys.exit(1)
if not token:
    print("❌ NETBOX_TOKEN 未加载")
    sys.exit(1)
print("✅ 环境与模块导入正常")

print("🔍 验证 NetBox Token ...")
r = api_probe()
print(f"  API   : {r.get('url')} -> HTTP {r.get('http_status')}（{r.get('hint')}）")
if not r.get("ok"):
    print("❌ NetBox Token 验证失败：请使用 Web 界面生成的 nbt_ 开头 v2 Token，")
    print("   并确认 NetBox 服务已启动、NETBOX_URL 正确")
    sys.exit(1)
print("✅ NetBox Token 验证通过")
PY
  probe_model
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; sleep 1; cmd_start ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  test)    cmd_test ;;
  *)
    cat <<EOF
NetBox SRE Agent 管理脚本

用法：$0 <命令>

命令：
  start     后台启动服务
  stop      停止服务
  restart   重启服务
  status    查看运行状态
  logs      实时查看日志（Ctrl+C 退出）
  test      检查 .env、模块导入、NetBox Token 与模型 API 是否可用

环境变量：
  PORT      服务端口（默认 8001）
  HOST      监听地址（默认 0.0.0.0）

示例：
  $0 start
  PORT=8002 $0 restart
EOF
    exit 1
    ;;
esac
