#!/bin/sh
# Local, named Cloudflare Tunnel helper for Carme.
# It never creates a Quick Tunnel, changes Karing, or edits Cloudflare policy.
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
CONFIG=${CARME_CLOUDFLARE_CONFIG:-$ROOT_DIR/deploy/cloudflared/carme-tunnel.yml}
ENV_FILE=${CARME_ENV_FILE:-$ROOT_DIR/.local/active/.env}
PID_FILE=${CARME_CLOUDFLARE_PID_FILE:-$ROOT_DIR/.local/active/cloudflared.pid}
LOG_FILE=${CARME_CLOUDFLARE_LOG_FILE:-$ROOT_DIR/.local/active/cloudflared.log}

case "$CONFIG" in
  /*) ;;
  *) CONFIG="$ROOT_DIR/$CONFIG" ;;
esac
case "$ENV_FILE" in
  /*) ;;
  *) ENV_FILE="$ROOT_DIR/$ENV_FILE" ;;
esac
case "$PID_FILE" in
  /*) ;;
  *) PID_FILE="$ROOT_DIR/$PID_FILE" ;;
esac
case "$LOG_FILE" in
  /*) ;;
  *) LOG_FILE="$ROOT_DIR/$LOG_FILE" ;;
esac

usage() {
  printf '%s\n' \
    "用法：$0 {check|status|start|stop|diagnose} [http2|quic]" \
    "  check              只检查安装、CARME_TOKEN 和 ingress 配置" \
    "  status             只读取本脚本记录的本地进程状态" \
    "  start              用命名隧道后台启动，默认采用配置中的协议" \
    "  stop               优雅停止本脚本启动的隧道进程" \
    "  diagnose PROTOCOL  前台运行一次实际协议诊断，PROTOCOL 为 http2 或 quic"
}

find_binary() {
  if command -v cloudflared >/dev/null 2>&1; then
    command -v cloudflared
    return 0
  fi
  for candidate in /opt/homebrew/bin/cloudflared /usr/local/bin/cloudflared /usr/bin/cloudflared; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

find_python() {
  if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    printf '%s\n' "$ROOT_DIR/.venv/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  return 1
}

read_carme_token() {
  python=$(find_python) || return 1
  "$python" - "$ENV_FILE" <<'PY'
import os
import sys
from dotenv import dotenv_values

if "CARME_TOKEN" in os.environ:
    value = os.environ["CARME_TOKEN"]
else:
    try:
        value = dotenv_values(sys.argv[1], interpolate=False).get("CARME_TOKEN") or ""
    except (OSError, UnicodeError, ValueError):
        value = ""
value = str(value).strip()
if value:
    sys.stdout.write(value)
    raise SystemExit(0)
raise SystemExit(1)
PY
}

has_carme_token() {
  token=$(read_carme_token 2>/dev/null) || return 1
  [ -n "$token" ]
}

validate_config() {
  python=$(find_python) || {
    printf '%s\n' 'FAIL: 未找到可读取配置的 Python。' >&2
    return 1
  }
  "$python" - "$CONFIG" <<'PY'
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

try:
    import yaml
except ImportError:
    print("FAIL: 当前 Python 缺少 PyYAML，无法验证命名隧道配置。", file=sys.stderr)
    raise SystemExit(1)

path = Path(sys.argv[1])
try:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
except (OSError, UnicodeError, yaml.YAMLError):
    print("FAIL: 无法读取 Cloudflare 配置。", file=sys.stderr)
    raise SystemExit(1)

def fail(message):
    print("FAIL: " + message, file=sys.stderr)
    raise SystemExit(1)

if not isinstance(raw, dict):
    fail("配置顶层必须是对象。")
tunnel = str(raw.get("tunnel", "")).strip()
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", tunnel):
    fail("tunnel 必须是命名隧道名称或 UUID。")
credentials = str(raw.get("credentials-file", "")).strip()
if not credentials:
    fail("缺少 credentials-file。")
credentials_path = Path(os.path.expanduser(credentials))
if not credentials_path.is_absolute():
    credentials_path = path.parent / credentials_path
if not credentials_path.is_file():
    fail("credentials-file 指向的文件不存在。")
protocol = str(raw.get("protocol", "http2")).strip().lower()
if protocol not in {"auto", "http2", "quic"}:
    fail("protocol 必须是 auto、http2 或 quic。")

ingress = raw.get("ingress")
if not isinstance(ingress, list):
    fail("ingress 必须是列表。")
fallback_item = ingress[-1] if ingress else None
fallback = (isinstance(fallback_item, dict)
            and set(fallback_item) == {"service"}
            and str(fallback_item.get("service", "")).strip().lower().replace(" ", "") == "http_status:404")
origin_routes = []
for item in ingress:
    if not isinstance(item, dict):
        continue
    hostname = str(item.get("hostname", "")).strip().lower().rstrip(".")
    service = str(item.get("service", "")).strip()
    try:
        parsed = urlsplit(service)
        origin_ok = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                     and parsed.port == 8899 and parsed.path.rstrip("/") == ""
                     and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment)
    except ValueError:
        origin_ok = False
    if origin_ok and not hostname:
        fail("指向 127.0.0.1:8899 的 ingress 必须有固定 hostname。")
    if not hostname:
        continue
    labels = hostname.split(".")
    if ("." not in hostname or len(hostname) > 253
            or any(not 1 <= len(label) <= 63
                   or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                   for label in labels)):
        fail("ingress hostname 不是固定域名。")
    if not origin_ok:
        continue
    origin_routes.append((hostname, item.get("path") in (None, "", "/"), item))

expected = os.environ.get("CARME_CLOUDFLARE_HOSTNAME", "").strip().lower().rstrip(".")
if len(origin_routes) != 1 or not origin_routes[0][1]:
    fail("必须恰好有一个指向 127.0.0.1:8899 的固定域名 ingress。")
target_hostname, _, target_item = origin_routes[0]
if expected and target_hostname != expected:
    fail("配置域名与 CARME_CLOUDFLARE_HOSTNAME 不一致。")
origin_request = target_item.get("originRequest")
access = origin_request.get("access") if isinstance(origin_request, dict) else None
if not isinstance(access, dict) or access.get("required") is not True:
    fail("Carme ingress 必须启用 originRequest.access.required=true。")
if not str(access.get("teamName", "")).strip():
    fail("Carme ingress 缺少 Access teamName。")
audience = access.get("audTag")
if not isinstance(audience, list) or not any(str(tag).strip() for tag in audience):
    fail("Carme ingress 缺少 Access audTag。")
if not fallback:
    fail("缺少 http_status:404 fallback。")
print("OK: Cloudflare ingress 已验证（固定域名、loopback origin、Access JWT 字段和 fallback）。")
PY
}

check_backend_auth() {
  python=$(find_python) || {
    printf '%s\n' 'FAIL: 未找到 Python，无法验证当前 Carme 鉴权。' >&2
    return 1
  }
  token=$(read_carme_token 2>/dev/null) || {
    printf '%s\n' 'FAIL: 无法读取 CARME_TOKEN；不会启动公网入口。' >&2
    return 1
  }
  CARME_CHECK_TOKEN="$token" "$python" - "http://127.0.0.1:8899/api/health" <<'PY'
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

url = sys.argv[1]
token = os.environ.get("CARME_CHECK_TOKEN", "")

def status(request):
    try:
        with urlopen(request, timeout=4) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except (OSError, URLError, TimeoutError):
        return None

without_token = status(Request(url))
with_token = status(Request(url, headers={"Authorization": "Bearer " + token}))
if without_token != 401:
    print("FAIL: 当前 8899 /api/health 未在无令牌请求时返回 401；请先配置 CARME_TOKEN 并重启现有服务。", file=sys.stderr)
    raise SystemExit(1)
if with_token != 200:
    print("FAIL: 当前 8899 /api/health 未接受配置的 CARME_TOKEN；请核对 .env 与运行进程后重启。", file=sys.stderr)
    raise SystemExit(1)
print("OK: 当前 8899 已验证为 CARME_TOKEN 保护（无令牌=401，正确令牌=200；令牌值未输出）。")
PY
}

read_pid() {
  [ -f "$PID_FILE" ] || return 1
  pid=$(tr -d '[:space:]' < "$PID_FILE")
  case "$pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$pid" -gt 1 ] || return 1
  printf '%s\n' "$pid"
}

is_running() {
  pid=$1
  kill -0 "$pid" 2>/dev/null || return 1
  command_line=$(ps -p "$pid" -o command= 2>/dev/null || true)
  case "$command_line" in
    *cloudflared*--config*"$CONFIG"*) return 0 ;;
    *) return 1 ;;
  esac
}

check() {
  binary=$(find_binary) || {
    printf '%s\n' 'FAIL: 未找到 cloudflared；不会启动 Quick Tunnel。' >&2
    return 1
  }
  printf '%s\n' "OK: cloudflared 已安装（${binary}）"
  if has_carme_token; then
    printf '%s\n' 'OK: 检测到 CARME_TOKEN 配置（值不会输出）'
  else
    printf '%s\n' 'FAIL: 未检测到 CARME_TOKEN；公网入口前必须先配置并重启 Carme。' >&2
    return 1
  fi
  [ -f "$CONFIG" ] || {
    printf '%s\n' "FAIL: 缺少命名隧道配置：$CONFIG" >&2
    return 1
  }
  validate_config
  "$binary" tunnel --config "$CONFIG" ingress validate
  check_backend_auth
  printf '%s\n' 'OK: ingress 语法和当前 Carme 鉴权检查通过；Access Allow 策略仍需在 Cloudflare Dashboard 实际验证。'
}

status() {
  if pid=$(read_pid) && is_running "$pid"; then
    printf '%s\n' 'RUNNING: 本脚本记录的 cloudflared 进程正在运行。'
  else
    printf '%s\n' 'NOT_RUNNING: 没有发现本脚本记录的运行中 cloudflared 进程。'
  fi
}

start() {
  check
  if pid=$(read_pid) && is_running "$pid"; then
    printf '%s\n' '已在运行，不重复启动。' >&2
    return 1
  fi
  protocol=${CARME_CLOUDFLARE_PROTOCOL:-}
  if [ -z "$protocol" ]; then
    python=$(find_python) || {
      printf '%s\n' 'FAIL: 未找到 Python，无法读取配置协议。' >&2
      return 1
    }
    protocol=$("$python" - "$CONFIG" <<'PY'
import sys
import yaml
from pathlib import Path
raw = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
print(str(raw.get("protocol", "http2")).strip().lower())
PY
)
  fi
  case "$protocol" in
    auto|http2|quic) ;;
    *) printf '%s\n' 'CARME_CLOUDFLARE_PROTOCOL 必须是 auto、http2 或 quic。' >&2; return 1 ;;
  esac
  mkdir -p "$(dirname -- "$PID_FILE")" "$(dirname -- "$LOG_FILE")"
  touch "$LOG_FILE"
  chmod 600 "$LOG_FILE"
  binary=$(find_binary)
  nohup "$binary" tunnel --config "$CONFIG" --protocol "$protocol" --pidfile "$PID_FILE" run \
    >>"$LOG_FILE" 2>&1 </dev/null &
  printf '%s\n' "已发起命名隧道启动，协议=${protocol}；请用 status 和日志确认真实连接。"
}

stop() {
  if ! pid=$(read_pid) || ! is_running "$pid"; then
    printf '%s\n' '没有发现本脚本记录的运行中隧道。'
    return 0
  fi
  kill "$pid"
  printf '%s\n' '已发送优雅停止信号；稍后用 status 确认。'
}

diagnose() {
  protocol=${1:-}
  case "$protocol" in
    http2|quic) ;;
    *) printf '%s\n' '请明确指定 diagnose http2 或 diagnose quic。' >&2; return 2 ;;
  esac
  check
  binary=$(find_binary)
  printf '%s\n' "开始前台 $protocol 协议诊断；按 Ctrl-C 停止，不会修改 Karing。"
  exec "$binary" tunnel --config "$CONFIG" --protocol "$protocol" run
}

action=${1:-}
case "$action" in
  check) check ;;
  status) status ;;
  start) start ;;
  stop) stop ;;
  diagnose) diagnose "${2:-}" ;;
  *) usage >&2; exit 2 ;;
esac
