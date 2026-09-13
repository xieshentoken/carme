#!/usr/bin/env bash
# ============================================================
#  Carme 分发包构建
#
#    ./scripts/package_release.sh
#
#  产出：dist/carme-<版本>-release.tar.gz
#  内容：后端源码 + 预构建网页（接收方无需 Node）+ 配置模板 + 部署脚本
#  不包含：.env 密钥、运行数据、备份、缓存、内部协作文档
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])' 2>/dev/null \
  || sed -n 's/^version = "\([^"]*\)".*/\1/p' pyproject.toml | head -1)"
[ -n "$VERSION" ] || { echo "无法读取版本号" >&2; exit 1; }

STAGING="$(mktemp -d)/carme-$VERSION"
OUT_DIR="$PROJECT_DIR/dist"
OUT="$OUT_DIR/carme-$VERSION-release.tar.gz"
mkdir -p "$OUT_DIR"

# ---- 汇集文件（显式清单，避免把密钥或数据带进包） ----
mkdir -p "$STAGING"
rsync -a \
  --exclude '__pycache__/' --exclude '*.py[cod]' --exclude '.DS_Store' \
  carme/ "$STAGING/carme/"
rsync -a --exclude '.DS_Store' config/ "$STAGING/config/"
rsync -a \
  --exclude 'node_modules/' --exclude '.npm-cache/' --exclude '.dist-before-cli-*' \
  --exclude '.DS_Store' \
  web/ "$STAGING/web/"
rsync -a --exclude '.DS_Store' deploy/ "$STAGING/deploy/"
rsync -a --exclude '__pycache__/' --exclude '.DS_Store' scripts/ "$STAGING/scripts/"
cp pyproject.toml README.md .env.example .gitignore "$STAGING/"

# ---- 安全检查：包内不得出现密钥或本机运行数据 ----
echo "▸ 安全检查"
if grep -RIlE "CARME_MODEL_KEY_[0-9A-F]{16}='|sk-[A-Za-z0-9]{20}" "$STAGING" >/dev/null 2>&1; then
  echo "✗ 包内发现疑似密钥，中止" >&2
  grep -RIlE "CARME_MODEL_KEY_[0-9A-F]{16}='|sk-[A-Za-z0-9]{20}" "$STAGING" >&2
  exit 1
fi
[ ! -e "$STAGING/.env" ] || { echo "✗ .env 不得进入分发包" >&2; exit 1; }
[ ! -e "$STAGING/deploy/cloudflared/carme-tunnel.yml" ] || { echo "✗ 隧道凭据不得进入分发包" >&2; exit 1; }
[ -f "$STAGING/web/dist/index.html" ] || { echo "✗ web/dist 缺失，先执行 npm run build" >&2; exit 1; }
echo "  ✓ 未发现密钥与运行数据"

# ---- 打包 ----
TAR_ROOT="$(dirname "$STAGING")"
tar -czf "$OUT" -C "$TAR_ROOT" "$(basename "$STAGING")"
SHASUM=$(shasum -a 256 "$OUT" | awk '{print $1}')
SIZE=$(du -h "$OUT" | awk '{print $1}')
echo "$SHASUM  $(basename "$OUT")" > "$OUT_DIR/carme-$VERSION-release.sha256"

echo
echo "════════════════════════════════════════"
echo "  分发包已生成"
echo "  文件：$OUT"
echo "  大小：$SIZE"
echo "  SHA256 已写入 $OUT_DIR/carme-$VERSION-release.sha256"
echo "  接收方：解压后执行 ./deploy/install.sh，然后填 .env 并启动"
echo "════════════════════════════════════════"
