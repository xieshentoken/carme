#!/usr/bin/env bash
# ============================================================
#  Carme 一键安装（在要跑服务的那台机器上执行）
#
#    ./deploy/install.sh              # 装依赖 + 建 venv + 自检
#    ./deploy/install.sh --with-docker   # 顺便装上 colima + docker
#    ./deploy/install.sh --service       # 顺便注册开机自启
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

WITH_DOCKER=0
WITH_SERVICE=0
for arg in "$@"; do
  case "$arg" in
    --with-docker) WITH_DOCKER=1 ;;
    --service)     WITH_SERVICE=1 ;;
    -h|--help)     sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg"; exit 2 ;;
  esac
done

say()  { printf "\n\033[1;35m▸ %s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
die()  { printf "  \033[31m✗\033[0m %s\n" "$1"; exit 1; }

say "检查基础环境"

PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version=$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    major=${version%%.*}; minor=${version##*.}
    if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; then PY="$candidate"; break; fi
  fi
done
[ -n "$PY" ] || die "需要 Python 3.11 或更高版本。macOS 上可以：brew install python@3.12"
ok "Python：$($PY -V)（$PY）"

command -v git >/dev/null 2>&1 && ok "git：$(git --version)" || warn "没有 git，不影响运行"

# ---------- 虚拟环境 ----------
say "准备虚拟环境"
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
  ok "已创建 .venv"
else
  ok ".venv 已存在，复用"
fi

./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -e '.[browser]'
ok "依赖已安装"

# ---------- 网页构建（运行时由 FastAPI 提供，无需常驻 Node 服务） ----------
say "构建 iOS / macOS 网页应用"
if [ -f web/dist/index.html ] && [ "${REBUILD_WEB:-0}" != "1" ]; then
  ok "web/dist 已随分发包提供，跳过构建（需要重新构建时：REBUILD_WEB=1 ./deploy/install.sh）"
else
  command -v npm >/dev/null 2>&1 || die "需要 Node.js 22.12+（或受当前 Vite 支持的版本）和 npm 来构建网页。"
  npm --prefix "$PROJECT_DIR/web" ci --no-audit --no-fund
  npm --prefix "$PROJECT_DIR/web" run build
  ok "网页已构建到 web/dist"
fi

# ---------- 配置文件 ----------
say "准备配置"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  warn "已生成 .env —— 请配置 API key 和访问令牌；测试模型不代表真实模型链路通过"
else
  ok ".env 已存在"
fi
mkdir -p data/workspaces

# ---------- 可选：容器沙箱 ----------
if [ "$WITH_DOCKER" = "1" ]; then
  say "安装容器沙箱（colima + docker）"
  if command -v brew >/dev/null 2>&1; then
    brew list colima >/dev/null 2>&1 || brew install colima
    brew list docker >/dev/null 2>&1 || brew install docker
    if ! colima status >/dev/null 2>&1; then
      colima start --cpu 2 --memory 4 --disk 30
    fi
    ok "colima 已就绪"
    docker build -f deploy/Dockerfile.sandbox -t carme/sandbox:latest . && ok "沙箱镜像已构建"
  else
    warn "没有 Homebrew，跳过。手动装：brew install colima docker"
  fi
fi

# ---------- 可选：开机自启 ----------
if [ "$WITH_SERVICE" = "1" ]; then
  say "注册开机自启（launchd）"
  PLIST="$HOME/Library/LaunchAgents/com.carme.gateway.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
      -e "s|__HOME__|$HOME|g" \
      deploy/com.carme.gateway.plist.template > "$PLIST"
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  ok "已注册：$PLIST"
  ok "以后开机自动起，日志在 $PROJECT_DIR/data/carme.log"
fi

# ---------- 自检 ----------
say "环境自检"
./.venv/bin/python -m carme.cli doctor || true

cat <<EOF

════════════════════════════════════════════════════════
  下一步

  1. 填 key      vim .env
  2. 看模型      ./.venv/bin/python -m carme.cli models probe
  3. 起服务      ./.venv/bin/python -m carme.cli serve
  4. 手机访问    http://<这台机器的局域网IP>:8787

  在网页「执行电脑」中配置2018 MacBook；先确认SSH主机指纹和免密登录。
  手机PWA完整能力需要HTTPS；Safari打开后「分享 → 添加到主屏幕」。
  对其他设备提供访问前，请在 .env 里设 CARME_TOKEN 并配置HTTPS或私网入口。
════════════════════════════════════════════════════════
EOF
