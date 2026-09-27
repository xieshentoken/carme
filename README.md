# Carme

自托管的常驻 AI 员工团队（多 Bot 工作台）：常驻后端 + 隔离执行机 + 网页 / 移动端界面。支持多 Bot 群聊协作，以及**第三方人类访客账号**受邀进群参与讨论。

本仓库**只含源码**。运行数据、账号数据库、备份、浏览器资料、个人配置与云隧道凭据一律不入库（见 `.gitignore`）。

## 目录

| 路径 | 内容 |
| --- | --- |
| `app/` | 后端与前端源码（`carme/`、`web/src/`、`deploy/`、`scripts/`） |
| `app/README.md` | **主要文档**：功能、配置、云隧道与验收说明 |
| `app/deploy/docker/README.md` | 多账号隔离入口（Gateway + 每账号独立实例） |
| `*.command` | macOS 双击启动器（终端启动 / Docker 多账号 / 状态 / 管理员后台） |

## 主要能力

- **多 Bot 团队**：每个 Bot 有独立身份、角色、记忆与任务上下文；同一群内可多 Bot 协作，各自维护自己的长期记忆。
- **群聊**：支持多成员（Bot 与人类）在同一会话中讨论；主账号可组合成员、定义群内规则，群 AI 只使用成员共同获准的上下文。
- **人类访客账号**：主账号为指定 Bot 创建邀请后，访客用一次性密码登录入群。访客在各群有独立身份、可署名发言、受邀请群可见性约束；入群默认只见入群后的历史，主账号可决定是否开放此前的群历史。访客不具备账号管理 token，也没有直接工具或桌面权限；对外发送等敏感动作仍由主账号独占审批。
- **隔离执行**：Bot 的命令与浏览器操作在独立容器或远端执行机中运行，不是后端宿主的 shell。
- **动作审批与操作回执**：需要写操作的动作经人工审批后执行，并留下可核对的操作记录。

> 具体的完成度与验收边界以 `app/README.md` 为准；文档中区分「合成测试」「真实 Docker」「真实模型」「公网验收」，不互相替代。

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

## 平台与运行环境

目前主要面向 **macOS 宿主**验证（终端启动器、launchd 托管、macOS 系统权限的屏幕录制 / 辅助功能 / 输入监控授权、`macos_runner` 宿主执行）。后端与隔离执行部分以 Docker 运行，不绑定特定桌面平台，但多为 macOS 环境下的实测结果；在 Linux 上运行需要自行核对部署脚本与路径假设。

固定域名的来源不写死在源码中：隧道配置读取自 `app/deploy/cloudflared/carme-tunnel.yml`（该文件不入库），或用 `CARME_CF_HOSTNAME` 覆盖。隧道 origin 可用 `CARME_CLOUDFLARE_ORIGIN` 配置。

## 隐私说明

仓库内的路径、域名、隧道 UUID 等均为占位符（`/path/to/carme`、`/Users/you`、`carme.example.com`、`00000000-0000-0000-0000-000000000000`、`your-team`）；真实值只存在于本机安装与 `~/.cloudflared/`。提交前请再确认 `.env`、`app/config/`、`runtime/`、`backups/`、`admin/` 没有被带进来。

## 许可

未声明许可证。
