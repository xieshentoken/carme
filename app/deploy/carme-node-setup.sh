#!/usr/bin/env bash
# ============================================================
#  远端节点准备脚本 —— 在「2018 款 MacBook Pro」那台机器上执行
#
#  它做的事情：
#    1. 关掉会让机器半夜睡死、导致任务中断的电源策略
#    2. 建好工作目录
#    3. （可选）装 colima + docker，让 Agent 在容器里干活
#    4. 打印出主控机需要填的配置
#
#  用法：
#    bash carme-node-setup.sh              # 基础准备
#    bash carme-node-setup.sh --docker     # 顺便装容器运行时
# ============================================================
set -euo pipefail

WITH_DOCKER=0
[ "${1:-}" = "--docker" ] && WITH_DOCKER=1

say()  { printf "\n\033[1;35m▸ %s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
die()  { printf "  \033[31m✗\033[0m %s\n" "$1"; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "这个脚本是给 macOS 用的"

NODE_DIR="$HOME/carme-node"

say "1/5  机器信息"
printf "  型号　%s\n" "$(sysctl -n hw.model)"
printf "  内存　%.1f GB\n" "$(echo "$(sysctl -n hw.memsize) / 1073741824" | bc -l)"
printf "  系统　%s %s\n" "$(sw_vers -productName)" "$(sw_vers -productVersion)"
printf "  磁盘　%s\n" "$(df -h / | tail -1 | awk '{print $4" 可用 / "$2" 总计"}')"

MEM_GB=$(echo "$(sysctl -n hw.memsize) / 1073741824" | bc)
if [ "$MEM_GB" -lt 9 ]; then
  warn "内存只有 ${MEM_GB}GB —— 沙箱并发必须卡在 1，且不要跑本地大模型"
  warn "请确认 config/sandbox.yaml 里 max_concurrent_sandbox: 1"
fi

say "2/5  电源策略（防止半夜睡死中断任务）"
# 插电时不睡、合盖不休眠。笔记本合盖必须接电源，否则照样睡。
sudo pmset -c sleep 0 displaysleep 10 disksleep 0 2>/dev/null \
  && ok "已设置：接电源时不进入睡眠" \
  || warn "pmset 设置失败（可能需要 sudo 密码），可手动到「系统设置 → 电池」关掉自动睡眠"

# 合盖不休眠（仅接电源时有效）
sudo pmset -c disablesleep 1 2>/dev/null && ok "已设置：合盖不中断（需接电源）" || true

say "3/5  工作目录"
mkdir -p "$NODE_DIR/workspaces" "$NODE_DIR/logs"
ok "$NODE_DIR"

say "4/5  容器运行时"
if [ "$WITH_DOCKER" = "1" ]; then
  if ! command -v brew >/dev/null 2>&1; then
    die "没找到 Homebrew。先装：/bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  fi
  brew list colima >/dev/null 2>&1 || brew install colima
  brew list docker >/dev/null 2>&1 || brew install docker
  if ! colima status >/dev/null 2>&1; then
    # 8GB 机器上 2 CPU / 4G 内存是安全值，再多会拖垮宿主
    colima start --cpu 2 --memory 4 --disk 30
  fi
  ok "colima 已运行"
  docker version --format '  Docker 引擎 {{.Server.Version}}' 2>/dev/null || warn "docker 还连不上，稍等片刻再试"
else
  warn "跳过（加 --docker 参数可以装）。不装也能用 local 模式跑。"
fi

say "5/5  主控机需要填的配置"
HOSTNAME_SHORT=$(scutil --get LocalHostName 2>/dev/null || hostname -s)
cat <<EOF

  在「主控机」的 config/sandbox.yaml 里，把 modes.remote 改成：

    remote:
      host: ${HOSTNAME_SHORT}.local
      port: 22
      user: $(whoami)
      identity_file: ~/.ssh/id_ed25519
      root: ${NODE_DIR}/workspaces
      use_docker: $([ "$WITH_DOCKER" = "1" ] && echo "true" || echo "false")
      docker_memory: 2g

  然后在「主控机」上确认免密登录已通：

    ssh-copy-id -i ~/.ssh/id_ed25519.pub $(whoami)@${HOSTNAME_SHORT}.local
    ssh $(whoami)@${HOSTNAME_SHORT}.local 'echo 通了'

  再执行一次自检：

    ./.venv/bin/python -m carme.cli node check

EOF

# 确认远程登录已开
if sudo -n launchctl print system/com.openssh.sshd >/dev/null 2>&1; then
  ok "远程登录（SSH）已开启"
else
  warn "请确认已开启「系统设置 → 通用 → 共享 → 远程登录」"
fi

printf "\n\033[1;32m节点准备完成\033[0m\n\n"
