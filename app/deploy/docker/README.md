# M2 容器候选部署

## 统一入口与账号密码（2026-09-20）

`carme.gateway` 在现有的每账号 Docker 实例之前提供统一登录入口：

`carme.example.com → Cloudflare Access → Gateway:8898 → 账号自己的 Control/Broker/数据库/浏览器`

Gateway 校验 Cloudflare JWT 的 RS256 签名、issuer、audience 和有效期，再校验 Carme 账号密码。Carme 会话还绑定通过验证的 Cloudflare 用户身份。生产入口只监听 `127.0.0.1`，不接受客户端指定后端，不接受共享入口上的 Bearer 或 URL token 绕过密码登录。`--local-gateway` 只允许 `http://127.0.0.1` 做隔离测试，不能用于公网。

账号名与 Docker `--account` 一一对应，沿用该账号独立的 `runtime/secrets/control-token`。token 不下发前端；Gateway 只在向该账号 Control 转发时使用它。业务数据库、附件、配置、模型凭据、Skill、工作区和浏览器 profile 继续由各自实例保存。`shared` 记忆仍只在该账号内共享。账号创建/禁用由本机管理员操作，没有公开注册入口或跨账号数据管理 API。

密码用带独立随机盐的 scrypt（N=2^17，r=8，p=1）存储，登录失败限流；会话使用 HttpOnly、Secure、SameSite=Strict 的 host-only Cookie，有效期 7 天（168 小时）。服务端仅保存会话摘要。支持设备会话撤销、退出、改密和禁用；撤销会终止已有 SSE。切换账号会更新其他标签页，旧页面的账号标记不能作为后端选择参数。前端本地偏好按账号分开，账号 HTML/API 使用 `no-store`。

### 本机网页管理后台（2026-09-20）

账号管理不再必须用终端：双击根目录 **`carme-admin.command`** 启动本机管理后台（仅监听 `127.0.0.1:8897`，端口可在 `admin/config.json` 改）。

- **所有管理文件都在 `./admin/`**：`admin_app.py`（服务）、`index.html`（页面）、`auth.db`（管理员与会话，600）、`config.json`、`jobs/`（实例任务状态）、`logs/`（任务日志）。
- **远程连接本机（Mac Runner）**：每个账号行的「本机电脑」按钮打开面板——未配对时一键执行 stop → `pair-mac` → start（后台任务，实时日志）；配对后显示 runner_id（Bot 的「执行目标 ID」）、doctor 系统权限三项检查、Runner 运行状态与当前授权。权限未就绪时面板提供 **「请求系统权限」** 按钮（触发 macOS 三个授权弹窗，由人决定是否允许），并显示要授权的**真实二进制路径**（`app/.venv/bin/python` 是符号链接，最终指向如 `/Users/you/.workbuddy/binaries/python/versions/3.13.12/bin/python3.13`；macOS TCC 按真实二进制记录，面板支持一键复制，也可在系统设置 + 选择器里用 ⌘⇧G 粘贴该路径手动添加辅助功能 / 屏幕录制 / 输入监控三项）；屏幕录制/输入监控授予后需重新启动 Runner 生效。Runner 启动若秒退，面板直接显示错误日志尾部。发放 ≤600 秒的本地授权：**Bot ID 留空默认授权该账号全部 Bot**（也可填单个 Bot ID），加 App bundle ID 和窗口 ID；每个动作仍需 Control 人工审批，人工接管立即撤销。Runner 依赖 `pyobjc-framework-ApplicationServices`（已装进 app/.venv）。CLI 等价命令：`macos_runner request`（发起授权请求）、`macos_runner grant --bot`（可省略 = `*` 全部 Bot）。轮换配对密钥仍需管理员手动审查，不在面板提供。
- 功能：账号列表（实例状态 / 健康组件 / 会话数 / 待领取初始密码标记）、新建账号（后台任务跑 `carme_docker.py start`，同账号互斥）、启动 / 停止 / 重启、生成初始密码（一次性弹显 + 写入账号 `login-initial.txt`）、重置密码（可自动生成，撤销全部会话）、禁用 / 恢复登录、踢下线、Gateway 状态、任务实时日志。
- 边界：仅本机可访问（拒绝转发头与非法 Host），不打开业务数据库、不接触模型凭据，全部操作复用 `carme.gateway` 与 `carme_docker.py` 既有实现；实例启停走后台任务（行内按钮防并发）。该后台不影响 Gateway 与公网入口。

### 为账号启用密码

已有账号直接设置密码（终端不回显，不把密码写到参数或聊天）：

```sh
./app/.venv/bin/python -B app/scripts/carme_docker.py login-password --account main
```

新增账号先启动独立实例，再设置密码：

```sh
./start-carme-docker.command --account alice --no-open
./app/.venv/bin/python -B app/scripts/carme_docker.py login-password --account alice
```

也可使用 `login-init --account alice` 生成随机初始密码，只写入该账号的 `login-initial.txt`（权限 600），不会输出密码。初始密码必须在首次登录时修改，改密后文件自动删除。已有密码不会被此命令覆盖。`login-password` 可以恢复被禁用的账号，并撤销所有已有会话；`login-disable --account alice` 禁用登录且保留业务数据，已经运行的业务任务不会因此自动取消。

### 启动统一入口

先在 `app/web` 执行 `npm run build`。Gateway 使用独立的 `runtime/docker/toolchains/gateway` 环境，依赖及 wheel 哈希锁定在 `requirements-gateway.lock`（当前支持 macOS 主机）。从 Access 应用取得 team name 和 AUD tag 后：

```sh
./app/.venv/bin/python -B app/scripts/carme_docker.py gateway-start \
  --origin https://carme.example.com --gateway-port 8898 \
  --access-team YOUR_TEAM --access-audience YOUR_APPLICATION_AUD
./app/.venv/bin/python -B app/scripts/carme_docker.py gateway-status
```

再次启动保留已有配置；更新后用 `gateway-start --restart`。`gateway-stop` 只停止统一入口，不删除账号数据。配置过统一入口后，现有 `start-carme-docker.command` 会同时启动入口，`stop-carme-docker.command` 不指定账号时也会关闭入口。该进程目前由启动器管理，尚未安装新的开机 LaunchAgent。启动器结束时打开的是**本机账号地址**（`http://c<hex>.localhost:<port>`，直接进 Carme，不需要 Cloudflare 登录，未配对时在「设置 → 通用」填 control-token）；要打开公网域名改用 `start-carme-docker.command --public`，那条路径需要先完成 Cloudflare Access 登录。

**域名切换是独立的最后一步。** 确认旧数据归属、备份和迁移后，将该 hostname 的 Tunnel `service` 从 `http://127.0.0.1:8899` 改为 `http://127.0.0.1:8898`，保留 Cloudflare Access Allow 策略，重启对应 tunnel 并从公网验证两次登录。远程管理的 Tunnel 还需核对云端 ingress。Gateway 自身验签，不能以伪造转发头代替 Cloudflare 登录。

2026-09-20 本次实施没有迁移旧数据：旧库有 6 个会话、26 条消息，Docker `main` 为空；等待用户确定归属。没有复制旧 `.env` 或个人浏览器资料；浏览器和模型身份需按账号分别配置。同日按用户要求完成域名切换实施（见下节），旧版 8899 不再直接提供公网内容。
### 域名切换的实际实施（2026-09-20）

发现该 Tunnel 的 ingress 由 **Cloudflare 云端托管**（本地 `carme-tunnel.yml` 的改动只被日志确认为「远程配置覆盖」，`Updated to new configuration ... service http://localhost:8899`），本机没有 Cloudflare API 凭据可直接改云端配置。因此本次切换改为：

1. `carme-tunnel.yml` 的 service 已同步改为 `http://127.0.0.1:8898`（云端一旦改回「本地配置」即直接生效）。
2. 云端仍指向 `localhost:8899`，故把旧的 `com.carme.serve`（8899 常驻旧服务）停用，替换为 `app/deploy/cloudflared/loopback-forward.py` 的 8899→8898 原始 TCP 转发（launchd 同名 Label 管理，日志 `~/Library/Logs/carme/forward.log`）。公网域名由此打到 Gateway：Access JWT → 账号密码 → `main` 账号隔离实例。
3. 备份：`backups/carme-tunnel.yml.bak-*-8899`、`backups/com.carme.serve.plist.bak-*`；旧服务 plist 保留为 `com.carme.serve.plist.disabled-20260920`。

**收尾（待办）**：在 Cloudflare 控制台把该 hostname 的 Tunnel ingress 从 `http://localhost:8899` 改为 `http://localhost:8898` 后：`launchctl bootout gui/$(id -u)/com.carme.serve`，还原 `com.carme.serve.plist.disabled-20260920` 并重新 bootstrap（或直接停用转发、让 8899 不再服务），即回到纯 8898 直连路径。

### 验证与回退

新增专项测试：`runtime/docker/toolchains/gateway/bin/python -B app/scripts/test_gateway.py`。覆盖双重认证、密码、CSRF、会话撤销、改密、限流、跨账号路由和存储。真实页面验收使用两个临时 Docker 账号，不调用真实模型；真实 Docker Chromium 另行验证账号 Cookie 隔离及同账号持久化。

本轮通过：18 项认证专项测试、24 项实际页面/聊天/附件/SSE 检查、8 项首次改密页面检查、14 项 Docker Chromium 检查，以及既有 M1 20 项、Cloudflare 6 项、Service Worker 9 项回归。页面尺寸为 1440×1000 和 390×844，未出现前端运行时异常或横向溢出。测试证据保存在本机 `/private/tmp/carme-gateway-*`；受限环境最初阻止 Chromium/进程归属检查，最终浏览器验收在获准的隔离测试进程内完成。公网双重登录、旧数据迁移与 iPhone 真机仍未验收，不能将本机通过等同于已上线。

停止 Gateway 即可撤回尚未切换的入口；旧库、旧服务和原 Tunnel 配置未变。若后续已经切换域名，先恢复已备份的 Tunnel `service` 并重启 Tunnel，再停止 Gateway。回退前端与启动器的源码备份位于 `backups/accounts-login-20260920-163814/`；认证 DB 与账号文件保留，避免丢失新密码或恢复已撤销会话。

设计参考：[Cloudflare 源站 JWT 验证](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/#access-settings)、[OWASP 密码存储](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)。

---

本目录接通现有 Carme，不替换其 FastAPI、SQLite、React、Harness、Bot、消息或审批体系。当前是经过合成模型验收的候选：真实 Carme 专用模型身份待用户配置；不迁移或重启已有实例。完整结果见 `docs/carme-isolation-plan/implementation/M2_TEST_RESULTS.md`。

## 已支持的边界

- Control 是唯一 SQLite 写入者；管理配置、审批、事件、模型凭据与 Artifact。没有 Docker socket，禁止本地 CLI/Shell 和第三方 stdio MCP。
- Broker 是独立可信宿主进程，持有 Docker 管理权限，只接受签名任务及本地管理员登记的 image ID / target。没有对外监听端口，不运行 LLM，不接受任意 Docker flags/mount/entrypoint。
- Pi 固定 `@earendil-works/pi-coding-agent@0.85.1`、Node 22.22.2；每次使用全新 HOME/profile，不挂载任务文件和个人配置，不加载自动资源或原生工具。仅受信 bridge 经 Broker 转交 Control。
- Action 每个动作一个容器，同一 run 的 workspace/out 持久保留；其他 run 不挂载。输入只读、镜像只读、UID 1000、cap_drop ALL、no-new-privileges、Docker 默认 seccomp、768 MiB/1 CPU/64 PID，临时目录有独立容量限额。
- Pi/Action 均 `network=none`。Pi 的本地模型 facade 经 stdio/签名 relay 请求 Control 的固定 HTTPS 模型出口；只有 Control 持有供应商密钥。API 模型继续使用现有网关，API 工具与 Pi 工具都进入同一 Action 路径。
- Docker/Broker/镜像不可用直接失败，不回退个人 Pi 或宿主 Shell。任务结束、取消、权限变更、lease 到期使旧 token 失效；重启不自动重放。

## 准备独立目录与镜像

使用专用 `CARME_HOME`，不要使用个人 HOME、`.pi`、项目根或生产数据目录。目录需有可信父目录；只让管理实例的用户访问。Docker Desktop 的 context 必须显式指定，Broker 的路径须属于该 daemon 可见的宿主。当前只验收了 macOS arm64 + 本机 Docker Desktop；远程 daemon 与其他架构未验收。

```text
CARME_HOME/
  config/                       # 现有 agents/models/browser/sandbox + isolation.yaml
  runtime/control/              # SQLite/WAL/SHM、会话、旧附件
  runtime/artifacts/             # 新二进制内容 hash 存储，仅 Control
  runtime/skills/                # Carme 自己的 Skill，仅 Control 只读
  runtime/secrets/               # 每个服务仅挂所需的单个密钥文件
  runtime/broker/                # 独占锁、清理回执、Broker 私有临时目录
  runtime/runs/<run_id>/         # Broker 计算的 owner/workspace/inputs/out
  broker.json                   # 可信管理员配置，不提供给 Agent
```

Docker 镜像、构建缓存和 daemon 元数据仍由 Docker 管理，不属于此目录。不要使用全局清理。持久工作区目前没有磁盘 quota 或自动删除策略，需管理员按任务记录审查保留量。

从经过审查的源码 `app/` 构建，使用自己的显式 context；不要传入 token ARG/ENV：

```sh
docker --context "$CARME_DOCKER_CONTEXT" build --iidfile control.id -f deploy/docker/Dockerfile.control .
docker --context "$CARME_DOCKER_CONTEXT" build --iidfile pi.id -f deploy/docker/Dockerfile.pi .
docker --context "$CARME_DOCKER_CONTEXT" build --iidfile action.id -f deploy/docker/Dockerfile.action .
```

记录三个 image ID、源码 hash 和测试结果。Control/Pi/Action 均使用 `sha256:...` ID；运行时 `--pull=never`。Python/Node 基础镜像按 digest 固定，Python 包带 hash、npm 带 lock；Action apt 精确版本锁为此次 arm64 构建生成，镜像仓库撤下版本时构建会失败，不会放宽版本继续。构建期间需要下载依赖，Worker 运行期间不下载。

### Google Chrome 正式版（可选，channel: chrome）

`Dockerfile.browser` 按 `TARGETARCH` 条件安装 **google-chrome-stable**（仅 amd64；Google 不提供 Linux arm64 包，arm64 构建会跳过，此时 `channel: chrome` 会在启动时报找不到浏览器，不影响默认 Chromium）。Chrome 与 Playwright 自带 Chromium 并存，登录态仍按 profile 目录隔离。

要让 Browser 容器用真 Chrome：

1. 重新构建镜像：源码 hash 变了，`carme_docker.py` 的 start/build 流程会生成新 release 并更新 `config/isolation.yaml` 里 `browser.image_digest`；或在审查后的 `app/` 下手动 `docker build -f deploy/docker/Dockerfile.browser .` 并按既有流程登记 image ID。
2. 在账号 `config/browser.yaml` 加 `channel: chrome`，重启该账号实例。**Control 与 Broker 必须同时升级**：browser payload 的键集是严格校验的，一侧旧一侧新会双向拒绝（`browser_fields_denied`）；新 Control 配旧 Browser 镜像时容器会静默忽略 `browser_channel` 回退 Chromium，不报错。
3. Payload 校验接受空串（未配置渠道 = 自带 Chromium）或 `[a-z][a-z0-9-]{0,19}` 渠道名；channel 由 Control 配置下发，容器入口再校验一次，模型/任务不可指定。
4. Chrome 在同一 seccomp 白名单与非 root 环境下未实测；若启动失败优先排查 `browser-seccomp.json` 系统调用（如 `clone3`）与 Chrome 需要的 `--no-sandbox` 等价项（项目内 Playwright 已按 relay 环境决定 `chromium_sandbox`）。

## 配置专用身份与服务

1. 在独立配置中保留所需的 Bot/模型定义。复制 `deploy/isolation.example.yaml` 为 `config/isolation.yaml`，填入明确的 Pi/Action image ID、模型和 target。空值会失败关闭。
2. 用户自行生成独立管理令牌和 Broker 配对密钥（至少 32 字节），以及 Carme 专用供应商 API key 文件；不读取、复制、软链接或挂载个人 Pi auth。不要把密钥写入报告、命令参数、日志或镜像。
3. Pi credentials 只接受 `openai-completions`；配置固定 HTTPS/443 `base_url`、精确 `provider/model` allowlist、`/run/secrets/<专用名称>`。Compose 按该名字补一个独立只读文件挂载。默认不使用环境代理；`proxy_url` 等额外配置明确拒绝。私有模型地址仅允许管理员明确 `allowed_ips`，link-local 永远拒绝；自签证书需显式只读 CA 文件。
4. 密钥父目录保持私有；仅授予对应服务 UID 1000 读取明确挂载文件所需的权限。不要把整个 secrets、CARME_HOME、HOME 或 Docker socket 挂给 Worker。示例 Compose 只挂 Control token / Broker key，专用模型 key 必须另加。
5. Bot 配置 `engine: pi`、`runtime_profile: pi-managed-v1`、`execution_target: container`、`execution_target_id: pi-action`，显式授予 `files/exec` 等所需能力。API Bot 使用现有模型设置，也须声明同一容器 target 才能执行工具。未声明 target 时不执行。
6. 填写 `broker.example.json` 的副本：与 Control 一致的 instance、配对密钥、Pi/Action ID、target ID、专用 home、绝对 Docker CLI 路径、独立 Docker config 和 context。生产不使用测试生成的临时凭据或 fixture 模型。

在独立候选目录启动 Control（默认 loopback 8900，与旧 8899 分开）：

```sh
CARME_HOME="$CARME_CANDIDATE_HOME" CONTROL_IMAGE="$CARME_CONTROL_IMAGE" \
  docker --context "$CARME_DOCKER_CONTEXT" compose -p carme-candidate \
  -f deploy/docker/compose.yaml up -d
```

Broker 使用独立 Python 3.13 venv；依赖见 `requirements-control.lock`（安装时 `pip --require-hashes`）。在经过审查的 `app/` 下运行，不使用用户个人 Python/Pi 插件：

```sh
CARME_LOAD_ENV=0 "$CARME_BROKER_PYTHON" -B -m carme.broker "$CARME_CANDIDATE_HOME/broker.json"
```

Broker 使用自己的白名单子进程环境和 HOME；命令不会把 key 内容放入 argv。HTTP 仅接受 loopback，跨机器 Control 地址必须为 HTTPS 并验证证书。Broker/Worker 响应也校验签名。

在候选 UI 配对后查看「设置 → 模型」的组件状态，再做指定 profile 连接测试。镜像 ready、profile 已配置、供应商身份实际验证是不同状态。没有凭据返回 `carme_auth_required`，OAuth 返回 `credential_refresh_unsupported`；不开放未实现的刷新路径。

## 验收与停止

测试脚本：`scripts/test_isolation_m2.py`、`test_pi_loader_m2.py`、`test_isolation_m2_docker.py`。真实 Docker 测试必须传入临时目录、显式 context/config 和预构建 image ID 的参数 JSON；使用独立 HTTPS 合成模型与随机测试凭据。支持额外 `fault_only: true` 分别验证 CPU 限额、Broker/Control 强制中断。

当前二进制成果从 Action 安全读取原始 bytes，经有界 relay 归档，单文件上限 10 MiB；记录 hash、任务/会话 ACL、来源和 `unverified`。尚未实现流式大文件上传、隔离格式解析、成果正式验收、Skill 学习发布或可靠外部动作对账。Pi 模型 facade 当前仅文本，未开放通用网络下载、第三方 extension/MCP、个人项目挂载或 Mac Runner。后者属于后续阶段，不应为绕过限制授予 host network/socket 或本机执行。

停止时先结束候选 Broker，让它按 instance/job 标记清理自己的容器，再停止该 Compose project。不要删数据目录或全局 Docker 资源。异常退出保留 cleanup pending 回执，重启同实例 Broker 只对该实例执行器对账并终止旧执行，不重跑业务动作。生产切换、SQLite 一致备份与完整迁移在 M3 单独实施；本阶段不改原启动入口。
