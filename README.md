# Carme

自托管的常驻 AI 员工团队（多 Bot 工作台）：常驻后端 + 隔离执行机 + 网页 / 移动端界面。

本仓库**只含源码**。运行数据、账号数据库、备份、浏览器资料、个人配置与云隧道凭据一律不入库（见 `.gitignore`）。

## 目录

| 路径 | 内容 |
| --- | --- |
| `app/` | 后端与前端源码（`carme/`、`web/src/`、`deploy/`、`scripts/`） |
| `app/README.md` | **主要文档**：功能、配置、云隧道与验收说明 |
| `app/deploy/docker/README.md` | 多账号隔离入口（Gateway + 每账号独立实例） |
| `*.command` | macOS 双击启动器（终端启动 / Docker 多账号 / 状态 / 管理员后台） |

## 快速开始

需要 Python 3.11+（建议 3.13）与 Node.js 22.12+（仅构建前端时用）。

```sh
cd app
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env            # 至少填入自己的随机 CARME_TOKEN
cd web && npm ci && npm run build && cd ..
.venv/bin/python -m carme.cli serve --host 127.0.0.1 --port 8787
```

浏览器打开 `http://127.0.0.1:8787`，在网页连接界面填入 `CARME_TOKEN` 完成配对。也可以直接运行 `bash app/deploy/install.sh` 自动安装（`--service` 才会注册后台服务）。

模型供应商、Bot 角色、语音等配置由网页写入 `app/config/*.yaml`，**这些个人配置不在本仓库**。公网入口（Cloudflare Tunnel + Access）请参考 `app/README.md` 与 `app/deploy/cloudflared/carme-tunnel.yml.example` 自行配置。

## 隐私说明

仓库内的路径、域名、隧道 UUID 等均为占位符（`/path/to/carme`、`carme.example.com`、`00000000-0000-0000-0000-000000000000`）；真实值只存在于本机安装与 `~/.cloudflared/`。提交前请再确认 `.env`、`app/config/`、`runtime/`、`backups/`、`admin/` 没有被带进来。

## 许可

未声明许可证。
