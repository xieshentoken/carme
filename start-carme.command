#!/bin/bash
# ============================================================
#  Carme 一键启动 / 重启 / 权限体检 / 固定域名隧道（macOS）
#
#  用法（在项目文件夹里双击本文件，或在终端执行）：
#    ./start-carme.command             启动 Carme → 拉起固定域名隧道 → 体检 → 打开浏览器
#    ./start-carme.command --restart   重启 Carme；改完系统权限后必须用它
#    ./start-carme.command --stop      停止 Carme（含隧道，如果由本脚本托管）
#    ./start-carme.command --diagnose  只体检：权限 + 隧道 + 公网入口，不改任何状态
#    ./start-carme.command --service   改由 launchd 托管 Carme 服务：开机/崩溃自启
#    ./start-carme.command --uninstall-service  取消 Carme 服务的 launchd 托管
#    ./start-carme.command --tunnel    重新安装并重启固定域名隧道（launchd 托管）
#    ./start-carme.command --tunnel-stop  停止隧道
#    ./start-carme.command --no-tunnel 本次不碰隧道（只起本机服务）
#    ./start-carme.command --local     浏览器只打开本机地址（不打开固定域名）
#    ./start-carme.command --no-open   不打开浏览器
#
#  固定域名：https://carme.example.com
#    隧道配置：app/deploy/cloudflared/carme-tunnel.yml（隧道 00000000…，ingress → http://127.0.0.1:8899）
#    凭据：    ~/.cloudflared/<tunnel-id>.json（权限 600，不要放进项目目录）
#    托管：    ~/Library/LaunchAgents/com.carme.tunnel.plist，日志 ~/Library/Logs/carme/tunnel.log
#    注意：该隧道的 ingress 由 Cloudflare 云端配置（dashboard）下发时会覆盖本地文件，
#          两边都指向本机 8899；改端口要同时改云端和本地文件。
#    入口保护：Cloudflare Access（team your-team）。手机端提示“Access 登录已过期或未完成”
#          时，用 Safari 重新打开一次 https://carme.example.com 完成登录即可。
#
#  系统权限（2026-09-14 在本机实测）：
#    「本机屏幕 / 远程鼠标键盘」需要 macOS 的「屏幕录制」+「辅助功能」。
#    macOS 把这两项权限授给**责任进程**，也就是“启动 Carme 的那个 app”，而不是 Python 本身：
#      从 WorkBuddy 启动    → 授权 /Applications/WorkBuddy.app（com.tencent.workbuddy.mac）
#      双击本文件（终端）  → 授权 /System/Applications/Utilities/Terminal.app
#      从 PI-Desktop 启动  → 授权 PI-Desktop.app
#      --service（launchd）→ 授权解释器本身（实测：
#                            /Users/you/.workbuddy/binaries/python/versions/3.13.12/bin/python3.13）
#    所以“已经授权却仍说没权限”，通常是授权授给了 Python 或别的 app。
#    权限改完必须重启服务：./start-carme.command --restart
#
#  访问令牌：只读 app/.env 的 CARME_TOKEN；未初始化时拒绝启动或发布。
#  注意：本脚本里变量都写成 ${VAR}，因为 macOS 的 bash 3.2 会把紧跟“）”“，”这类
#  全角字符的 $VAR 一起当成变量名（$code）→ 变量 code），从而打印成空。
# ============================================================

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="${DIR}/app"
HOST="127.0.0.1"
PORT="8899"
URL="http://${HOST}:${PORT}"
PLIST="${HOME}/Library/LaunchAgents/com.carme.serve.plist"
LOGDIR="${HOME}/Library/Logs/carme"

# ---------- 固定域名隧道 ----------
CFG="${APP}/deploy/cloudflared/carme-tunnel.yml"
TUNNEL_LABEL="com.carme.tunnel"
TUNNEL_PLIST="${HOME}/Library/LaunchAgents/${TUNNEL_LABEL}.plist"
TUNNEL_METRICS="127.0.0.1:20247"
TUNNEL_PIDFILE="${APP}/.local/active/cloudflared.pid"
# 与 carme-tunnel.yml 的 ingress hostname 保持一致
CF_HOSTNAME="carme.example.com"
PUBLIC_URL="https://${CF_HOSTNAME}"

OPEN_TARGET="public"   # public = 打开固定域名；local = 打开本机地址
OPEN_BROWSER=1
USE_TUNNEL=1
MODE="start"
for arg in "$@"; do
  case "${arg}" in
    --restart)           MODE="restart" ;;
    --stop)              MODE="stop" ;;
    --diagnose)          MODE="diagnose" ;;
    --service)           MODE="service" ;;
    --uninstall-service) MODE="uninstall-service" ;;
    --tunnel)            MODE="tunnel" ;;
    --tunnel-stop)       MODE="tunnel-stop" ;;
    --no-tunnel)         USE_TUNNEL=0 ;;
    --local)             OPEN_TARGET="local" ;;
    --no-open)           OPEN_BROWSER=0 ;;
    -h|--help)           sed -n '2,36p' "$0"; exit 0 ;;
    *) echo "未知参数：${arg}（用 -h 看用法）"; exit 2 ;;
  esac
done

pause_and_exit() { echo; echo "按任意键关闭…"; read -n 1 -s; exit 1; }

# ---------- 读取令牌 ----------
TOKEN="$(grep -E '^CARME_TOKEN=' "${APP}/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' | tr -d '"')"
TOKEN_SOURCE="app/.env"
if [ -z "${TOKEN}" ]; then
  TOKEN_SOURCE="未初始化（请配置 app/.env）"
  case "${MODE}" in
    start|restart|service|tunnel)
      printf '%s\n' 'authentication_not_initialized: 请先在 app/.env 配置 CARME_TOKEN。不会启动或发布服务。' >&2
      exit 1 ;;
  esac
fi

# venv 的 bin/python 是符号链接，权限要加真实解释器路径时用得到
REAL_PY=""
if [ -x "${APP}/.venv/bin/python" ]; then
  REAL_PY="$("${APP}/.venv/bin/python" -c 'import os,sys; print(os.path.realpath(sys.executable))' 2>/dev/null)"
fi

# 保留服务启动所需的 PATH；Agent 子进程独立构造白名单环境，不继承此 PATH。
service_path() {
  local candidates node_bin
  candidates="$(ls -d "${HOME}"/.workbuddy/binaries/node/versions/*/bin 2>/dev/null || true)"
  node_bin="$(printf '%s\n' "${candidates}" | grep -v '^$' | (sort -V 2>/dev/null || cat) | tail -1)"
  if [ -n "${node_bin}" ] && [ -x "${node_bin}/node" ]; then
    printf '%s:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:%s/.local/bin' "${node_bin}" "${HOME}"
    return 0
  fi
  printf '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:%s/.local/bin' "${HOME}"
}

find_cloudflared() {
  if [ -x /opt/homebrew/bin/cloudflared ]; then printf '%s' /opt/homebrew/bin/cloudflared; return 0; fi
  if [ -x /usr/local/bin/cloudflared ]; then printf '%s' /usr/local/bin/cloudflared; return 0; fi
  if [ -x /usr/bin/cloudflared ]; then printf '%s' /usr/bin/cloudflared; return 0; fi
  if command -v cloudflared >/dev/null 2>&1; then command -v cloudflared; return 0; fi
  return 1
}

# ---------- 探测 ----------
probe() {  # 服务存活且令牌正确时返回 0
  curl -sf -m 3 -o /dev/null -H "Authorization: Bearer ${TOKEN}" "${URL}/api/stats"
}

shot_code() {  # 抓一次本机屏幕，返回 HTTP 状态码（200=有屏幕录制权限，503=被系统拒绝）
  curl -s -m 10 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" "${URL}/api/desktop/screenshot"
}
wait_ready() {
  local i
  for i in $(seq 1 60); do
    if probe; then return 0; fi
    sleep 0.5
  done
  echo "❌ 服务启动超时。最近日志："
  tail -15 "${APP}/.local/serve.log" 2>/dev/null
  tail -15 "${LOGDIR}/serve.log" 2>/dev/null
  pause_and_exit
}

# ---------- 停止 / 重启服务 ----------
service_is_loaded() { launchctl print "gui/$(id -u)/com.carme.serve" >/dev/null 2>&1; }

# 卸载 launchd 任务并等它真正消失：紧接着 bootstrap 会撞上
# "Bootstrap failed: 5: Input/output error"（旧任务还在卸载中）。
bootout_service() {
  local i
  launchctl bootout "gui/$(id -u)/com.carme.serve" 2>/dev/null
  for i in $(seq 1 20); do
    service_is_loaded || return 0
    sleep 0.5
  done
  return 1
}

stop_service() {  # 停掉占用端口的进程（不含 launchd 托管的情况）
  local pids i
  pids="$(lsof -t -nP -iTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | sort -u | tr '\n' ' ')"
  if [ -z "${pids}" ]; then
    echo "ℹ️  端口 ${PORT} 上没有服务在跑。"
    return 0
  fi
  echo "🛑 正在停止旧服务（PID: ${pids}）…"
  kill ${pids} 2>/dev/null
  for i in $(seq 1 20); do
    if ! lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "✅ 已停止。"
      return 0
    fi
    sleep 0.5
  done
  kill -9 ${pids} 2>/dev/null
  sleep 1
  echo "✅ 已强制停止。"
}
stop_any() {
  if service_is_loaded; then
    echo "🛑 正在通过 launchd 停止服务（com.carme.serve）…"
    bootout_service
    sleep 1
    echo "✅ 已停止。"
    return 0
  fi
  stop_service
}
restart_running() {  # 保留 launchd 归因，只重启进程；否则停掉再交给 start_service
  if service_is_loaded; then
    echo "♻️  正在通过 launchd 重启 Carme（com.carme.serve）…"
    if ! launchctl kickstart -k "gui/$(id -u)/com.carme.serve" 2>/dev/null; then
      echo "❌ launchctl kickstart 失败，请改用 ./start-carme.command --service"
      pause_and_exit
    fi
    wait_ready
    return 0
  fi
  stop_service
  return 1
}

# ---------- Carme 服务的 launchd 托管 ----------
install_service() {
  if [ ! -x "${APP}/.venv/bin/carme" ]; then
    echo "❌ 未找到 ${APP}/.venv/bin/carme，请先在 app 目录完成安装（python3 -m venv .venv && .venv/bin/pip install -e .）"
    pause_and_exit
  fi
  if [ -z "${REAL_PY}" ]; then
    echo "❌ 读不到解释器真实路径（${APP}/.venv/bin/python），无法注册 launchd 托管。"
    pause_and_exit
  fi
  local site
  site="$("${APP}/.venv/bin/python" -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null)"
  [ -n "${site}" ] || site="${APP}/.venv/lib"
  local service_env_path
  service_env_path="$(service_path)"
  mkdir -p "${HOME}/Library/LaunchAgents" "${LOGDIR}"
  echo "📦 正在注册 LaunchAgent：${PLIST}"
  stop_service   # 先让出端口
  # 两个在本机实测出来的硬限制（macOS TCC 保护 ~/Documents）：
  #   1) 程序必须写 ~/Documents 之外的解释器真实路径；写 .venv/bin/carme 这种
  #      项目内的脚本/包装器时，xpcproxy 会报 “posix_spawn(...) Operation not permitted”；
  #   2) StandardOutPath / StandardErrorPath 也不能落在 ~/Documents 里（同样报错），
  #      所以日志放到 ~/Library/Logs/carme/。
  cat > "${PLIST}" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.carme.serve</string>
  <key>ProgramArguments</key>
  <array>
    <string>${REAL_PY}</string>
    <string>-m</string>
    <string>carme.cli</string>
    <string>serve</string>
    <string>--host</string>
    <string>${HOST}</string>
    <string>--port</string>
    <string>${PORT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${APP}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>${APP}:${site}</string>
    <key>PATH</key>
    <string>${service_env_path}</string>
    <key>NO_PROXY</key>
    <string>*</string>
    <key>no_proxy</key>
    <string>*</string>
  </dict>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOGDIR}/serve.log</string>
  <key>StandardErrorPath</key>
  <string>${LOGDIR}/serve.log</string>
  <key>ProcessType</key>
  <string>Background</string>
  <key>Nice</key>
  <integer>5</integer>
</dict>
</plist>
PLISTEOF
  bootout_service || true
  # bootstrap 的退出码不可全信（它的失败信息被吞掉时更难查），所以加载后用 launchctl print 硬校验，
  # 并且不再回退到会“假成功”的 launchctl load -w。
  local boot_out
  if ! boot_out="$(launchctl bootstrap "gui/$(id -u)" "${PLIST}" 2>&1)"; then
    echo "❌ 注册 LaunchAgent 失败：${boot_out}"
    echo "   手动排查：launchctl bootstrap gui/$(id -u) ${PLIST}"
    pause_and_exit
  fi
  sleep 1
  if ! launchctl print "gui/$(id -u)/com.carme.serve" >/dev/null 2>&1; then
    echo "❌ launchctl 没有加载 com.carme.serve（bootstrap 报告成功但任务不存在）。"
    echo "   手动排查：launchctl bootstrap gui/$(id -u) ${PLIST}"
    pause_and_exit
  fi
  wait_ready
  echo "✅ 已由 launchd 托管并启动：${URL}"
  echo "   登录后自动启动、崩溃自动重启；日志：${LOGDIR}/serve.log"
  echo "   托管时权限对象固定是解释器：${REAL_PY}"
}
uninstall_service() {
  bootout_service
  if [ -f "${PLIST}" ]; then
    rm -f "${PLIST}"
    echo "✅ 已取消 launchd 托管（${PLIST} 已删除），Carme 已停止。"
    echo "   之后可以用 ./start-carme.command 普通后台启动（权限归因会跟随启动它的 app）。"
  else
    echo "ℹ️  没有安装过 launchd 托管。"
  fi
}

# ---------- 固定域名隧道 ----------
tunnel_loaded() { launchctl print "gui/$(id -u)/${TUNNEL_LABEL}" >/dev/null 2>&1; }

bootout_tunnel() {
  local i
  launchctl bootout "gui/$(id -u)/${TUNNEL_LABEL}" 2>/dev/null
  for i in $(seq 1 20); do
    tunnel_loaded || return 0
    sleep 0.5
  done
  return 1
}
tunnel_pid() {
  launchctl print "gui/$(id -u)/${TUNNEL_LABEL}" 2>/dev/null \
    | sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\)$/\1/p' | head -1
}
write_tunnel_pidfile() {  # 让 Carme 网页的 Cloudflare 面板显示“运行中”
  local p
  p="$(tunnel_pid)"
  if [ -n "${p}" ]; then
    mkdir -p "$(dirname "${TUNNEL_PIDFILE}")"
    printf '%s\n' "${p}" > "${TUNNEL_PIDFILE}"
    chmod 600 "${TUNNEL_PIDFILE}"
  fi
}
public_code() {  # 固定域名的 HTTP 状态码；302=Cloudflare Access 在挡（正常）
  curl -s -m 12 -o /dev/null -w '%{http_code}' "${PUBLIC_URL}/api/stats" 2>/dev/null
}
install_tunnel_agent() {
  local cf
  if [ ! -f "${CFG}" ]; then
    echo "⚠️  未找到隧道配置 ${CFG}，跳过隧道。"
    return 1
  fi
  if ! cf="$(find_cloudflared)"; then
    echo "⚠️  未找到 cloudflared（brew install cloudflared），跳过隧道。"
    return 1
  fi
  if ! grep -q '^credentials-file:' "${CFG}"; then
    echo "⚠️  隧道配置里没有 credentials-file，跳过隧道。"
    return 1
  fi
  local cred
  cred="$(sed -n 's/^credentials-file:[[:space:]]*//p' "${CFG}" | head -1)"
  if [ ! -f "${cred}" ]; then
    echo "⚠️  隧道凭据不存在：${cred}（应指向 ~/.cloudflared/<tunnel-id>.json），跳过隧道。"
    return 1
  fi
  mkdir -p "${HOME}/Library/LaunchAgents" "${LOGDIR}"
  echo "📦 正在注册隧道 LaunchAgent：${TUNNEL_PLIST}"
  cat > "${TUNNEL_PLIST}" <<TUNNELEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${TUNNEL_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${cf}</string>
    <string>tunnel</string>
    <string>--config</string>
    <string>${CFG}</string>
    <string>--no-autoupdate</string>
    <string>--protocol</string>
    <string>http2</string>
    <string>--edge-ip-version</string>
    <string>4</string>
    <string>--metrics</string>
    <string>${TUNNEL_METRICS}</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${APP}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>${LOGDIR}/tunnel.log</string>
  <key>StandardErrorPath</key>
  <string>${LOGDIR}/tunnel.log</string>
</dict>
</plist>
TUNNELEOF
  bootout_tunnel || true
  local boot_out
  if ! boot_out="$(launchctl bootstrap "gui/$(id -u)" "${TUNNEL_PLIST}" 2>&1)"; then
    echo "❌ 注册隧道 LaunchAgent 失败：${boot_out}"
    return 1
  fi
  sleep 1
  if ! tunnel_loaded; then
    echo "❌ launchctl 没有加载 ${TUNNEL_LABEL}（bootstrap 报告成功但任务不存在）。"
    return 1
  fi
  # 每日定时重启连接器：连接器可能「进程活着但边缘注册状态失效」，表现为公网 502（Host Error）而隧道日志为空。
  local restart_label="${TUNNEL_LABEL}-restart" restart_plist="${HOME}/Library/LaunchAgents/${TUNNEL_LABEL}-restart.plist"
  cat > "${restart_plist}" <<RESTARTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${restart_label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>launchctl kill SIGTERM gui/\$(id -u)/${TUNNEL_LABEL} 2&gt;/dev/null; i=0; while [ "\$i" -lt 20 ]; do pgrep -f "cloudflared tunnel --config" &gt;/dev/null || break; sleep 1; i=\$((i+1)); done; if pgrep -f "cloudflared tunnel --config" &gt;/dev/null; then launchctl kill SIGKILL gui/\$(id -u)/${TUNNEL_LABEL} 2&gt;/dev/null; sleep 2; fi; launchctl kickstart gui/\$(id -u)/${TUNNEL_LABEL}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>4</integer>
    <key>Minute</key>
    <integer>30</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>${LOGDIR}/tunnel-restart.log</string>
  <key>StandardErrorPath</key>
  <string>${LOGDIR}/tunnel-restart.log</string>
</dict>
</plist>
RESTARTEOF
  launchctl bootout "gui/$(id -u)/${restart_label}" >/dev/null 2>&1 || true
  if ! launchctl bootstrap "gui/$(id -u)" "${restart_plist}" >/dev/null 2>&1; then
    echo "⚠️  每日重启作业注册失败（不影响隧道本身）：${restart_plist}"
  fi
  return 0
}
tunnel_stop() {
  if tunnel_loaded; then
    echo "🛑 正在停止固定域名隧道（${TUNNEL_LABEL}）…"
    bootout_tunnel
    rm -f "${TUNNEL_PIDFILE}"
    echo "✅ 隧道已停止。"
  else
    echo "ℹ️  隧道没有在运行。"
  fi
}
ensure_tunnel() {  # 返回 0 = 隧道已在服务
  local i code
  if ! tunnel_loaded; then
    install_tunnel_agent || return 1
  fi
  if ! tunnel_loaded; then
    echo "❌ 隧道启动失败。tail -20 ${LOGDIR}/tunnel.log"
    return 1
  fi
  write_tunnel_pidfile
  for i in $(seq 1 20); do
    code="$(public_code)"
    if [ "${code}" = "302" ] || [ "${code}" = "200" ] || [ "${code}" = "401" ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}
tunnel_report() {
  local cf code pid
  cf="$(find_cloudflared 2>/dev/null || true)"
  if [ -z "${cf}" ]; then
    echo "🚇 固定域名：未安装 cloudflared，跳过（brew install cloudflared）"
    return 1
  fi
  if [ ! -f "${CFG}" ]; then
    echo "🚇 固定域名：缺少隧道配置 ${CFG}"
    return 1
  fi
  if tunnel_loaded; then
    pid="$(tunnel_pid)"
    echo "🚇 固定域名：launchd 托管中（${TUNNEL_LABEL}，PID ${pid:-?}）"
    echo "   路由：${CF_HOSTNAME} → http://127.0.0.1:${PORT}（配置文件 ${CFG}）"
  else
    echo "🚇 固定域名：隧道未运行（./start-carme.command --tunnel 可启动）"
    return 1
  fi
  code="$(public_code)"
  case "${code}" in
    302) echo "   公网入口：正常 —— Cloudflare Access 在保护（未登录时 302 到登录页）"
         echo "   手机端提示“Access 登录已过期或未完成”时：用 Safari 打开 ${PUBLIC_URL} 重新登录一次。" ;;
    401) echo "   公网入口：已绕开登录直达后端（Access 未生效？），后端按令牌返回 401" ;;
    200) echo "   公网入口：正常（200）" ;;
    000) echo "   ❌ 公网入口：连不上。检查网络/隧道日志：tail -20 ${LOGDIR}/tunnel.log" ;;
    *)   echo "   ⚠️ 公网入口：返回 HTTP ${code}；隧道日志：tail -20 ${LOGDIR}/tunnel.log" ;;
  esac
  return 0
}

# ---------- 权限体检 ----------
app_from_exec() {  # 从可执行文件路径推出 .app 路径
  case "$1" in
    *".app/"*) printf '%s' "${1%%.app/*}.app" ;;
    *) printf '%s' "" ;;
  esac
}
bundle_id_of() {  # $1 = .app 路径
  if [ -n "$1" ] && [ -f "$1/Contents/Info.plist" ]; then
    defaults read "$1/Contents/Info.plist" CFBundleIdentifier 2>/dev/null
  fi
}
responsible_app() {  # 取最近一次截屏请求在 tccd 日志里的“责任进程”路径
  local dump id resp
  dump="$(log show --last 10m --predicate 'subsystem == "com.apple.TCC"' --style compact 2>/dev/null)"
  # kTCCServiceScreenCapture 只写在 AUTHREQ_CTX 行上，真正的归因在 AUTHREQ_ATTRIBUTION 行，
  # 两者用 msgID 关联；从最近一次请求往前找，跳过还没写归因的那些。
  for id in $(printf '%s\n' "${dump}" | grep 'kTCCServiceScreenCapture' | grep -o 'msgID=[0-9.]*' | tail -8 | tail -r); do
    resp="$(printf '%s\n' "${dump}" | grep "AUTHREQ_ATTRIBUTION: ${id}," | sed -n 's/.*responsible_path=\([^,}]*\).*/\1/p' | head -1)"
    if [ -n "${resp}" ]; then printf '%s' "${resp}"; return 0; fi
  done
  return 1
}
engines_report() {  # CLI 引擎（pi / codex / claude）是否已连接：直接问后端的实时探测
  local body
  body="$(curl -s -m 40 -H "Authorization: Bearer ${TOKEN}" "${URL}/api/engines" 2>/dev/null)"
  if [ -z "${body}" ]; then
    echo "🤖 CLI 引擎：接口无响应（服务没起或令牌不对）"
    return 1
  fi
  printf '%s' "${body}" | "${APP}/.venv/bin/python" -c '
import json, sys
try:
    engines = json.load(sys.stdin).get("engines", [])
except Exception:
    print("🤖 CLI 引擎：响应无法解析")
    raise SystemExit(1)
labels = {"codex": "Codex CLI", "pi": "Pi", "claude": "Claude Code"}
shown = 0
for e in engines:
    eid = e.get("id", "")
    if eid not in labels:
        continue
    shown += 1
    ready = bool(e.get("ready"))
    state = "已连接" if ready else ("已发现但未登录" if e.get("installed") else "未安装")
    print("   " + ("✅" if ready else "⚠️ ") + " " + labels[eid] + ": " + state + "（" + str(e.get("version", "")) + "）")
if not shown:
    print("   ⚠️  没有发现任何 CLI 引擎")
' 2>/dev/null || echo "🤖 CLI 引擎：解析失败"
}
permission_report() {
  local code resp app bid
  code="$(shot_code)"
  if [ "${code}" = "200" ]; then
    echo "✅ 屏幕录制：可用（服务已能读到本机屏幕）"
    if service_is_loaded; then
      echo "   远程鼠标键盘还要「辅助功能」：托管服务用的是解释器，请加这一条"
      echo "     ${REAL_PY}"
    else
      echo "   远程鼠标键盘还要「辅助功能」：请给启动 Carme 的那个 app 勾上同一项"
    fi
    return 0
  fi
  echo "❌ 屏幕录制：不可用（/api/desktop/screenshot 返回 ${code}）"
  sleep 1
  resp="$(responsible_app)"
  app="$(app_from_exec "${resp}")"
  if [ -n "${resp}" ]; then
    echo "   macOS 把这次截屏算在“责任进程”上："
    echo "     ${resp}"
    if [ -n "${app}" ]; then
      bid="$(bundle_id_of "${app}")"
      if [ -n "${bid}" ]; then
        echo "   ➜ 要授权的就是这个 app：${app}（${bid}）"
      else
        echo "   ➜ 要授权的就是这个 app：${app}"
      fi
    else
      echo "   ➜ 要授权的就是上面这个可执行文件（解释器本身）"
    fi
  else
    echo "   ➜ 没抓到实时归因（日志里只有旧记录），给下面任一个授权即可："
    echo "     · 你启动 Carme 用的那个 app（WorkBuddy / 终端 / PI-Desktop）"
    [ -n "${REAL_PY}" ] && echo "     · 解释器本身：${REAL_PY}"
  fi
  echo "   操作：系统设置 → 隐私与安全性 → 屏幕录制 → ➕ → ⌘⇧G 粘贴上面的路径"
  echo "         系统设置 → 隐私与安全性 → 辅助功能 → ➕ → 同一个 app（远程鼠标键盘用）"
  echo "   授权后必须重启服务才生效：./start-carme.command --restart"
  [ -n "${REAL_PY}" ] && echo "   解释器真实路径（--service 托管时改成授权它）：${REAL_PY}"
  return 1
}

# ---------- 普通后台启动 ----------
start_service() {
  if [ ! -x "${APP}/.venv/bin/carme" ]; then
    echo "❌ 未找到 ${APP}/.venv/bin/carme，请先在 app 目录完成安装（python3 -m venv .venv && .venv/bin/pip install -e .）"
    pause_and_exit
  fi
  # 已经注册过 launchd 托管的话，必须继续用 launchd 起：
  # 用 daemonize_serve.py 起会让权限归因漂回启动它的 app（屏幕录制会再失败一次）。
  if [ -f "${PLIST}" ]; then
    echo "🚀 正在通过 launchd 启动 Carme（复用已注册的托管配置）…"
    if ! launchctl bootstrap "gui/$(id -u)" "${PLIST}" 2>/dev/null; then
      launchctl load -w "${PLIST}" 2>/dev/null
    fi
    wait_ready
    echo "✅ Carme 已启动：${URL}（launchd 托管）"
    return 0
  fi
  echo "🚀 正在后台启动 Carme …"
  /usr/bin/python3 "${APP}/.local/daemonize_serve.py"
  wait_ready
  echo "✅ Carme 已启动：${URL}"
}

# ---------- 只停 / 只体检 / 只调托管 ----------
if [ "${MODE}" = "stop" ]; then
  stop_any
  tunnel_stop
  echo "完成。本窗口可以关闭。"
  exit 0
fi

if [ "${MODE}" = "tunnel-stop" ]; then
  tunnel_stop
  echo "完成。本窗口可以关闭。"
  exit 0
fi

if [ "${MODE}" = "uninstall-service" ]; then
  uninstall_service
  echo "完成。本窗口可以关闭。"
  exit 0
fi

if [ "${MODE}" = "diagnose" ]; then
  if ! probe; then
    echo "❌ 服务没在跑（或它用的令牌与 ${TOKEN_SOURCE} 不一致）。先运行：./start-carme.command"
    pause_and_exit
  fi
  echo "🔎 体检 · 本机 ${URL} · 令牌来自 ${TOKEN_SOURCE}"
  permission_report
  echo
  engines_report
  echo
  tunnel_report
  echo
  echo "完成。本窗口可以关闭。"
  exit 0
fi

if [ "${MODE}" = "tunnel" ]; then
  install_tunnel_agent || pause_and_exit
  if ensure_tunnel; then
    echo "✅ 固定域名隧道已启动：${PUBLIC_URL}"
  else
    echo "⚠️  隧道已启动，但公网入口暂时探测不到（可能是网络/边缘在建连）。"
    echo "   稍后用 ./start-carme.command --diagnose 复检；日志：tail -20 ${LOGDIR}/tunnel.log"
  fi
elif [ "${MODE}" = "service" ]; then
  install_service
else
  # ---------- 判断端口状态 ----------
  HTTP_CODE="$(curl -s -m 3 -o /dev/null -w '%{http_code}' "${URL}/api/stats" 2>/dev/null || true)"
  if [ "${MODE}" = "restart" ]; then
    if restart_running; then
      HTTP_CODE="200"
    else
      HTTP_CODE="000"
    fi
  fi

  if [ "${HTTP_CODE}" = "200" ]; then
    echo "✅ Carme 已在运行：${URL}"
  elif [ "${HTTP_CODE}" = "401" ]; then
    if probe; then
      echo "✅ Carme 已在运行：${URL}"
    else
      echo "❌ 端口 ${PORT} 上有 Carme 在运行，但它使用的 CARME_TOKEN 与 ${TOKEN_SOURCE} 中的不一致。"
      echo "   请用 ./start-carme.command --restart 重启后再试。"
      pause_and_exit
    fi
  else
    # 000 = 端口无响应（未启动，或被非 HTTP 程序占用）；其他 = 被别的 HTTP 服务占用
    if [ "${HTTP_CODE}" != "000" ]; then
      echo "❌ 端口 ${PORT} 被其他服务占用（HTTP ${HTTP_CODE}），请先处理后再启动。"
      pause_and_exit
    fi
    if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "❌ 端口 ${PORT} 被一个非 HTTP 程序占用，请先处理后再启动。"
      pause_and_exit
    fi
    start_service
  fi
fi

# ---------- 固定域名隧道 ----------
TUNNEL_OK=0
if [ "${USE_TUNNEL}" = "1" ]; then
  echo
  if ensure_tunnel; then
    TUNNEL_OK=1
    echo "✅ 固定域名已接通：${PUBLIC_URL}"
  else
    echo "⚠️  固定域名隧道没能确认可用。详见：./start-carme.command --diagnose"
  fi
fi

# ---------- 权限体检 ----------
echo
echo "🔎 权限体检 · 服务 ${URL}"
echo "   访问令牌：${TOKEN}（来自 ${TOKEN_SOURCE}）"
permission_report
echo
engines_report

if [ "${USE_TUNNEL}" = "1" ]; then
  echo
  tunnel_report
fi

# ---------- 打开浏览器（令牌通过 URL 参数自动填入，前端会立即从地址栏清除）----------
echo
OPEN_URL="${URL}"
OPEN_NOTE="本机"
if [ "${OPEN_TARGET}" = "public" ] && [ "${TUNNEL_OK}" = "1" ]; then
  OPEN_URL="${PUBLIC_URL}"
  OPEN_NOTE="固定域名（需通过 Cloudflare Access 登录）"
fi
if [ "${OPEN_BROWSER}" = "1" ] && command -v open >/dev/null 2>&1; then
  open "${OPEN_URL}/"
  echo "🌐 已在浏览器打开${OPEN_NOTE}，并自动填入访问令牌。"
else
  echo "🌐 手动打开：${OPEN_URL}/"
fi
echo "   本机地址：${URL}/"
[ "${USE_TUNNEL}" = "1" ] && echo "   固定域名：${PUBLIC_URL}/"
echo "完成。本窗口可以关闭。"
