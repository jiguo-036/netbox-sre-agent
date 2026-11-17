#!/usr/bin/env bash
# NetBox 环境一键准备：环境检查 → 拉起容器 → 探测 API
# 用法：./run.sh [start|stop]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

ok()   { echo "✅ $*"; }
warn() { echo "⚠️  $*"; }
die()  { echo "❌ $*" >&2; exit 1; }

ENV_FILE="$ROOT/.env"

# 正常引用 .env 中的配置；仅加载，不校验模型参数与 Token（校验由 manage.sh test 负责）
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1091
  set -a; source "$ENV_FILE" || warn ".env 中有无法解析的行，已忽略"; set +a
fi

# NETBOX_TAG 只取自 .env 文件：不受外部同名环境变量影响，也不设脚本内默认值
NETBOX_TAG="$(sed -n 's/^[[:space:]]*NETBOX_TAG[[:space:]]*=[[:space:]]*//p' \
  "$ENV_FILE" 2>/dev/null | tail -1 | tr -d '[:space:]' || true)"
[[ -n "$NETBOX_TAG" ]] \
  || die "请在 $ENV_FILE 中配置 NETBOX_TAG（例如 NETBOX_TAG=5.1.1），版本仅从该文件读取"

NETBOX_DIR="${NETBOX_DIR:-$ROOT/netbox-docker}"
NETBOX_URL="${NETBOX_URL:-http://127.0.0.1:8000}"
NETBOX_TARBALL="https://codeload.github.com/netbox-community/netbox-docker/tar.gz/refs/tags/${NETBOX_TAG}"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.override.yml)
PROJECT=netbox-docker
PORT="${PORT:-8001}"
export NETBOX_DIR NETBOX_URL

manual_netbox_hint() {
  echo
  echo "自动下载失败，请手动下载上游项目后放到：$NETBOX_DIR"
  echo "  下载地址：$NETBOX_TARBALL"
  echo "  解压后把 netbox-docker-${NETBOX_TAG} 目录重命名为 netbox-docker，放在本仓库根目录；"
  echo "  若放在别处，请先 export NETBOX_DIR=实际路径"
  echo "放好后重新执行 ./run.sh 即可继续，脚本会自动补 override 文件并拉起服务。"
  exit 1
}

check_basic() {
  echo "== 1/4 基础环境检查 =="

  command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装 Docker"
  docker info >/dev/null 2>&1 || die "Docker 守护进程不可用，请先启动 Docker"
  docker compose version >/dev/null 2>&1 || die "需要 Docker Compose v2，请升级 Docker"
  ok "Docker / Compose 可用"

  command -v python3 >/dev/null 2>&1 || die "未找到 python3"
  local py major minor
  py="$(python3 -c 'import sys; print(f"{sys.version_info.major} {sys.version_info.minor}")')"
  major="${py%% *}"; minor="${py##* }"
  if (( major < 3 || (major == 3 && minor < 10) )); then
    die "Python 需要 >= 3.10，当前 ${major}.${minor}"
  fi
  python3 -c 'import venv' >/dev/null 2>&1 \
    || die "Python 缺少 venv 模块：Debian/Ubuntu 请执行 apt install python3-venv"
  ok "Python ${major}.${minor} 满足要求，venv 可用"

  command -v curl >/dev/null 2>&1 || die "未找到 curl（下载与探活需要）"
  command -v tar  >/dev/null 2>&1 || die "未找到 tar（解压上游项目需要）"
  ok "curl / tar 可用"
}

prepare_netbox() {
  echo "== 2/4 检查上游项目目录 =="

  if [[ ! -d "$NETBOX_DIR" ]]; then
    local tmpdir src i
    tmpdir="$(mktemp -d)"
    src="netbox-docker-${NETBOX_TAG}"
    for i in 1 2 3; do
      echo "未找到 $NETBOX_DIR，正在下载 netbox-docker ${NETBOX_TAG}（第 $i/3 次）..."
      if curl -fsSL --connect-timeout 10 --max-time 120 -o "$tmpdir/pkg.tar.gz" "$NETBOX_TARBALL" \
         && tar -xzf "$tmpdir/pkg.tar.gz" -C "$tmpdir" \
         && [[ -f "$tmpdir/$src/docker-compose.yml" ]]; then
        mkdir -p "$(dirname "$NETBOX_DIR")"
        mv "$tmpdir/$src" "$NETBOX_DIR"
        ok "已下载并解压 ${NETBOX_TAG} 到 $NETBOX_DIR"
        break
      fi
      warn "第 $i 次下载失败"
      rm -f "$tmpdir/pkg.tar.gz"
      rm -rf "$tmpdir/$src"
      sleep 2
    done
    rm -rf "$tmpdir"
    [[ -d "$NETBOX_DIR" ]] || manual_netbox_hint
  else
    ok "已存在：$NETBOX_DIR"
    if [[ -f "$NETBOX_DIR/VERSION" ]] \
       && [[ "$(tr -d '[:space:]' < "$NETBOX_DIR/VERSION")" != "$NETBOX_TAG" ]]; then
      warn "目录内版本为 $(tr -d '[:space:]' < "$NETBOX_DIR/VERSION")，与固定版本 $NETBOX_TAG 不一致；"
      warn "如需切换，请备份自改配置后移除该目录，再重新执行 ./run.sh"
    fi
  fi

  [[ -f "$NETBOX_DIR/docker-compose.yml" ]] \
    || die "$NETBOX_DIR 下缺少 docker-compose.yml，请确认 NETBOX_DIR 指向正确目录"

  if [[ ! -f "$NETBOX_DIR/docker-compose.override.yml" ]]; then
    cp "$NETBOX_DIR/docker-compose.override.yml.example" "$NETBOX_DIR/docker-compose.override.yml" \
      || die "生成 docker-compose.override.yml 失败"
    ok "已生成 docker-compose.override.yml"
  fi
}

prepare_python() {
  echo "== 3/4 创建并激活 Python 虚拟环境 =="

  # 对已存在的目录重复执行是安全的，不会清掉已装的包
  python3 -m venv "$ROOT/.venv" \
    || die "创建虚拟环境失败：Debian/Ubuntu 请执行 apt install python3-venv"

  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
  python -m pip install -q --disable-pip-version-check -r "$ROOT/requirements.txt" \
    || die "依赖安装失败，请检查网络或 pip 源"
  ok "虚拟环境 .venv 与依赖就绪"
}

start_all() {
  echo "== 4/4 拉起容器并探测 API =="

  (
    cd "$NETBOX_DIR"
    docker compose "${COMPOSE_FILES[@]}" -p "$PROJECT" up -d
  ) || die "NetBox 启动失败，请查看：$NETBOX_DIR 下 docker compose logs"
  ok "NetBox 容器已启动"

  echo "探测 NetBox API 状态（最多 180 秒）..."
  local code=""
  for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$NETBOX_URL/api/" || true)"
    [[ -n "$code" && "$code" != "000" ]] && break
    sleep 3
  done
  [[ -n "$code" && "$code" != "000" ]] \
    || die "NetBox 未在 180 秒内就绪，请查看：$NETBOX_DIR 下 docker compose logs"
  ok "NetBox API 已响应（HTTP $code）"

  cat <<TIPS

容器已就绪，接下来请手动完成：

  1) 创建管理员（用户名建议 admin）：
     cd $NETBOX_DIR && docker compose -f docker-compose.yml -f docker-compose.override.yml -p netbox-docker exec netbox /opt/netbox/netbox/manage.py createsuperuser

  2) 生成 Token：
     网页：$NETBOX_URL → 用户菜单 → API Tokens → 生成 nbt_ 开头的 Token
     将 Token 填入 $ROOT/.env 的 NETBOX_TOKEN

  3) 启动 Agent：
     $ROOT/manage.sh test && $ROOT/manage.sh start
     Agent 地址：http://服务器IP:$PORT

  NetBox  : $NETBOX_URL
  停止    : $ROOT/run.sh stop
TIPS
}

stop_all() {
  "$ROOT/manage.sh" stop || true
  (
    cd "$NETBOX_DIR"
    docker compose "${COMPOSE_FILES[@]}" -p "$PROJECT" down
  ) || warn "NetBox 停止时出错，请手动检查"
  ok "已停止 Agent 与 NetBox（数据卷保留）"
}

case "${1:-start}" in
  start) check_basic; prepare_netbox; prepare_python; start_all ;;
  stop)  stop_all ;;
  *)     die "用法：$0 [start|stop]" ;;
esac
