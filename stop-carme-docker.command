#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd -P)"
PYTHON="${CARME_BOOTSTRAP_PYTHON:-${ROOT}/app/.venv/bin/python}"
if [ ! -x "${PYTHON}" ]; then
  echo "未找到 Carme Python。请设置 CARME_BOOTSTRAP_PYTHON 为已安装的 Python 3.13 路径。" >&2
  exit 1
fi
mkdir -p "${ROOT}/runtime/docker/bootstrap-home"
exec /usr/bin/env -i HOME="${ROOT}/runtime/docker/bootstrap-home" PATH=/usr/bin:/bin:/usr/sbin:/sbin PYTHONDONTWRITEBYTECODE=1 \
  "${PYTHON}" -B "${ROOT}/app/scripts/carme_docker.py" stop "$@"
