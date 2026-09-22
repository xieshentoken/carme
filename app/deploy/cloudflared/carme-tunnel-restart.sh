#!/bin/sh
# 优雅重启固定域名隧道连接器。
#
# 为什么必须优雅：连接器可能出现「进程活着、但 Cloudflare 边缘侧的注册状态失效」，
# 表现为公网 502 Bad gateway（图上 Host 那一腿 Error），而隧道日志一片空白、请求计数不动。
# 重启能恢复；但 `launchctl kickstart -k` 用的是 SIGKILL，旧连接不会向边缘注销，
# 反而可能再造出同样的失效状态。所以这里先 SIGTERM 等它自己退出，再启动。
#
# 注意：launchd 不能执行 ~/Documents 下的脚本（macOS TCC 报 Operation not permitted），
# 所以装好的 com.carme.tunnel-restart 把同样的逻辑内联在 plist 里；本脚本供人工重启用：
#   sh app/deploy/cloudflared/carme-tunnel-restart.sh
set -eu

LABEL="com.carme.tunnel"
TARGET="gui/$(id -u)/${LABEL}"
MATCH="cloudflared tunnel --config"

running() {
  pgrep -f "${MATCH}" >/dev/null 2>&1
}

# KeepAlive 只在非零退出时重启，正常 SIGTERM 退出（0）不会被自动拉起，由本脚本显式启动。
launchctl kill SIGTERM "${TARGET}" 2>/dev/null || true
waited=0
while [ "${waited}" -lt 20 ]; do
  running || break
  sleep 1
  waited=$((waited + 1))
done
if running; then
  # 卡住不退出时才兜底强杀，避免定时任务把连接器留在半死状态。
  launchctl kill SIGKILL "${TARGET}" 2>/dev/null || true
  sleep 2
fi
launchctl kickstart "${TARGET}"

printf '%s 已优雅重启 %s（等待 %s 秒）\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${LABEL}" "${waited}"
