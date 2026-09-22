#!/bin/bash
# 启动 Carme 账号管理后台（仅 127.0.0.1，端口见 admin/config.json，默认 8897）。
# 首次启动自动创建管理员 admin，随机初始密码写入 admin/initial-password.txt（600）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd -P)"
PYTHON="${CARME_BOOTSTRAP_PYTHON:-${ROOT}/app/.venv/bin/python}"
if [ ! -x "${PYTHON}" ]; then
  echo "未找到 Carme Python。请设置 CARME_BOOTSTRAP_PYTHON 为已安装的 Python 3.13 路径。" >&2
  exit 1
fi
mkdir -p "${ROOT}/admin" "${ROOT}/runtime/docker/client-home"
chmod 700 "${ROOT}/admin" 2>/dev/null || true
LOG="${ROOT}/admin/admin.log"
echo "管理后台日志：${LOG}"
echo "首次使用：登录名 admin，初始密码见 ${ROOT}/admin/initial-password.txt"
exec /usr/bin/env -i HOME="${ROOT}/runtime/docker/client-home" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  PYTHONDONTWRITEBYTECODE=1 "${PYTHON}" -B "${ROOT}/admin/admin_app.py" 2>>"${LOG}"
