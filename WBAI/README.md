# Carme

自托管的 Bot 团队。参考 Grok Bot 的持续聊天与三栏界面，通过同一网页应用 / PWA 在 iOS 和 macOS 上使用。

**Bot 使用的执行电脑目前暂定为 2018 MacBook，后续可以替换。** 它与保存任务的常驻后端 Mac、用户使用的客户端是三个不同角色。

## 当前实现（0.2.0，2026-09-12）

| 能力 | 状态 |
|---|---|
| 桌面三栏、手机单栏导航、成员和会话搜索 | 已实现，桌面和手机尺寸浏览器已检查 |
| 创建 / 编辑 Bot、创建单聊与群聊 | 已接入真实配置与 SQLite |
| 模型连接管理、模型列表、effort 选择 | 已接入网页设置、接口测试与配置保存；当前真实 API 已验证工具调用链路，其他连接需逐个验收 |
| 团队默认模型、演示模型开关 | 网页可分别配置各档位，保留 Bot 单独指定的模型 |
| 右键菜单、分组、隐藏和恢复 | 已实现；删除先移到最近删除，运行中的对话需先停止任务 |
| 字体与字号 | 设置 → 外观，立即生效并保存在当前设备 |
| Bot 头像：形状 / 颜色 / 随机生成 / 图片上传 | 已实现，上传格式和大小限制在页面说明 |
| 执行中的头像动画与旋转彩带 | 已按真实任务状态接入，隔离任务验证完成、停止及切换聊天后的更新 |
| 持续追问、模型执行期间追加要求、成员委派 | 隔离测试已通过；真实 API 已通过委派、记忆写入及结果回传的合成任务 |
| 持久化消息、请求幂等、SSE 重连补消息 | 已验证重复提交、队列溢出和重启边界 |
| 私有 / 团队共享记忆管理、长对话摘要 | 已接入网页查看、编辑、删除与显式共享；摘要保留完整聊天原文 |
| 流式正文、实际模型标识 | Chat Completions、Responses、Anthropic 均已隔离验证，部分回复中断状态可恢复 |
| 文档 / 图片附件、成果预览下载与后端归档 | 已开放下述格式；真实 API 已完成读取附件→生成 CSV→归档的合成任务 |
| Cloudflare named Tunnel、固定域名、Access 配置页和安全启动检查 | 源码与隔离检查脚本已实现；Cloudflare 账户、固定域名、Access Allow 规则和 iPhone 真机仍待本人完成 |
| 品牌图标与 PWA 图标（本地 logo.png） | 已生成网页、favicon、Apple Touch、192/512 和 maskable 资源；安装效果待真机验收 |
| 任务取消、预算失败、过期待办清理 | 已实现，取消和重启清理遗留审批 |
| 登记执行电脑、设默认、固定任务机器、子任务继承 | 已实现；目前只有未配置的 2018 MacBook 草稿 |
| 远端 SSH 与 Chrome CDP | 已实现连接代码和隔离边界测试，尚未接真实 MacBook |
| Codex / Pi / Claude 本机 CLI 引擎 | 已接入任务级工作目录、受控 Carme 工具桥接、native 活动与取消清理；Codex / Claude 已完成隔离真实链路，Pi 认证仍待配置 |
| PWA manifest / 离线页面外壳 | 已构建；iPhone 安装、HTTPS 和后台表现待真机验收 |
| 实时桌面 / noVNC / 人工接管 | 待实施、待真机连接，界面明确显示不可用 |
| 例行任务、Web Push、语音 | 尚未开放，界面不模拟成功 |
| 技能市场、示范教学、桌面视觉自动操作 | 后续阶段 |

当前已验证所配置真实 API 模型的合成工具任务，并已在隔离工作目录验证 Codex / Claude 本机 CLI 的真实任务链路；尚未连接真实 SSH 或完成手机真机验收。`passdown.md` 和旧 `docs/architecture.html` 是此前版本记录；当前范围以根目录 `AGENTS.md` 和本页为准。

## 三个角色

- **常驻后端 Mac**：FastAPI、SQLite、模型 API 调用、任务编排，保存角色、会话、任务和事件。
- **执行 MacBook**：Bot 的命令、文件、Chrome 和后续桌面操作；本项目不部署本地推理。
- **iOS / macOS 客户端**：聊天、查看、审批和后续电脑接管。退出页面不会主动停止后端任务。

服务和持久化数据由自己管理；发给云端模型的对话及工具输出会传给所配置的 API。密钥放在后端 `.env`，SSH 私钥保留在原密钥文件，网页配置只保存文件路径引用。

## CLI 引擎与执行边界

Bot 可在设置中选择内置 API 网关，或选择后端 Mac 已安装的 Codex CLI、Pi、Claude Code。两条执行路径必须区分：

- **API 引擎**继续使用任务快照中的执行电脑节点（当前规划为 2018 MacBook），终端、文件和浏览器工具走既有节点 / SSH 边界。
- **CLI 引擎**在常驻后端 Mac 本机运行，进程默认从该 Bot 的后端工作目录启动；默认是数据目录下按 Bot 划分的 `cli-workspaces/<bot-id>`，也可在 Bot 设置中指定后端本地目录。实际文件访问范围由 CLI 自身权限模式和 Mac 账号权限决定，不宣称与 API 的 OS / SSH 沙箱等价（Codex 使用 `workspace-write`，Claude 使用普通权限模式，Pi 不承诺相同的 OS 隔离）。它不会使用或伪装成 2018 MacBook。
- 创建任务时快照引擎、CLI 模型、CLI `engine_effort` 和工作目录；运行中的任务不会因设置修改而换引擎或换目录。API 的 `effort` 与 CLI 的 `engine_effort` 分开保存，切换回 API 时保留原 API 强度。
- CLI 只通过任务级 loopback 桥接调用现有 Carme 的记忆、附件读取、成果归档和成员委派工具。权限、审批、事件和任务预算仍由 Carme 控制；原生工具活动只记录安全的工具名与成功 / 失败状态，不写入命令、路径、参数或工具输出。
- CLI 的发现、登录状态和一次“测试连接”不能等同于完整任务能力。CLI 供应商不一定返回可核对的 token / 价格，因此 CLI 任务的费用和 token 显示为未知，不计入 Carme 的已知美元金额统计；工具调用仍受任务步数预算约束。Carme 不把 API key 复制给 CLI，也不修改 CLI 的用户认证配置。

2026-09-12 的隔离真实链路已验证 Codex CLI 与 Claude Code：后端本机工作目录、原生文件、旧聊天、私有 / 团队记忆、图片附件读取和 `create_artifact` 均写入或归档正确；Pi 已发现但认证状态未知 / 测试返回认证失败，未计为已接通。取消或超时会清理整个 CLI 进程组及其后代。桌面 GUI 接管和真实 2018 MacBook 仍属于独立验收项。

## 构建和启动

以下在**常驻后端 Mac** 的本项目目录操作：

```bash
cd <项目目录>/WBAI
python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[browser]'
cd web
npm ci
npm run build
cd ..
./.venv/bin/python -m carme.cli serve --host 127.0.0.1 --port 8787
```

需要 Python 3.11+，以及当前 Vite 支持的 Node.js（建议 22.12+）。Node 只用于构建；运行时 FastAPI 提供 `web/dist` 和同源 API。

在浏览器打开 `http://127.0.0.1:8787`。若设置了 `CARME_TOKEN`，在网页连接界面填入令牌。首次使用可按 `.env.example` 创建 `.env`，已有文件不要覆盖。模型连接和 Bot 角色都可在网页设置中配置，分别保存到 `config/models.yaml` 和 `config/agents.yaml`。

真实使用前应配置可用云端模型并关闭 `allow_mock`，然后验证一次完整的模型工具调用。`models probe` 只检查可见模型列表，不代表执行链路通过。测试模型启用时界面和回答会明确提示。

首次自动安装也可以运行 `bash deploy/install.sh`。该脚本会安装 Python 依赖、构建网页、在 `.env` 不存在时创建示例文件；`--service` 才会注册后台服务。配置执行机不需要在后端安装 Docker。

## 模型连接与 Bot 头像

在「设置 → 模型 → 添加模型连接」填写连接名称、API 类型、基础网址和 API key。支持 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages，以及提供 OpenAI Chat Completions 协议的其他兼容服务。

1. 点击「测试连接」，后端读取该凭据可见的模型列表；失败时显示错误，不保存半成品。
2. 勾选模型并选择 effort。接口提供能力信息时直接读取；未知时可点击「检测 effort」，仅列出接口实际接受的选项。对静默忽略参数的网关不宣称已确认支持，可保留模型默认。
3. 点击「验证并保存所选模型」。后端逐个发送固定测试句验证所选配置，成功后写配置。检测和验证可能产生少量 API 费用，不包含用户聊天内容；工具调用能力仍需真实任务另行验收。
4. 在每个 Bot 的设置中选择已保存的模型，可继承连接的 effort 或单独选择已确认的选项。切换保留 Bot 的固定 ID、角色、会话、摘要和长期记忆，运行中的任务继续使用其角色配置快照，新任务使用新选择。不同模型自身的上下文容量仍有差异。

密钥仅写后端 `.env`，YAML 保存变量引用，API 不回显密钥。编辑连接时留空保留已存密钥；更换网址或 API 类型后需重新输入，防止旧密钥被发给新地址。旧的手工配置继续读取。当前自动发现需要供应商实现相应的模型列表接口。

参数依据：[OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[Anthropic 模型能力](https://platform.claude.com/docs/en/api/models)、[Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort)。型号和支持等级从连接取得，不写死品牌型号表。

点击 Bot 头像打开编辑面板：Bot 页选择 8 种形状、11 种颜色；生成页在本地随机组合；上传页支持 PNG、JPEG、WebP 静态图片，最大 5 MB，宽高均为 32–4096 像素，建议 512×512。图片居中裁切并重新编码为 512×512 WebP，去掉元数据，保存在后端数据目录的 `avatars/` 中，读取受访问令牌保护。预览后点击「保存修改」才绑定到 Bot；取消不会更换 Bot 头像，重置使用原 Emoji。

现有头像和设置保持不变。仅在任务执行时，按自带头像几何生成有体积的立体形状，绕模型空间竖直的 Z 轴旋转，头像上沿显示彩带；上传图片、Emoji 和群聊图标保持静止，只显示彩带。侧边栏、聊天标题、执行提示与当前成员详情同步显示，历史消息头像保持静止。动画依据后端 `running` 状态，排队、等待确认、完成、失败和取消恢复原头像；开启系统「减少动态效果」时展示静态立体形状、彩带和文字提示。

## 聊天菜单与外观

右键侧边栏中的 Bot，或点击旁边的「更多」按钮，可置顶、移至新建或已有分组、标为未读、重命名 Bot、编辑资料、创建副本、复制对话 ID、隐藏及删除对话。手机增加长按入口；触摸手势仍需 iPhone 真机验收。键盘可用 Shift+F10 打开菜单，方向键选择，Escape 关闭。

创建副本会复制角色、模型、工具权限与头像，并新建独立 Bot 和空白对话；不复制原 Bot 的记忆和聊天。重命名 Bot 会同步显示于其单聊入口。群聊提供群聊重命名，资料编辑和复制 Bot 仅用于单聊。

「删除」只将本段对话移到最近删除，不删除 Bot 的角色和记忆，也不影响其他对话。隐藏和删除均可从侧边栏的「隐藏与最近删除」恢复；当前没有自动清空或永久删除操作。状态保存在后端，两端共用。

「设置 → 外观」提供系统默认、苹方、宋体、楷体和等宽字体，缺少字体时由浏览器回退。基准字号 14–22 px，界面文字按比例调整；保存在当前浏览器，因此 iPhone 和 Mac 可各自选择合适字号。点击「恢复默认」回到系统字体、16 px。

## 聊天、记忆与文件交付

- 在聊天详情打开「记忆管理」，或在 Bot 设置中管理其记忆。可新增、编辑、删除，也可明确复制到团队共享区；同范围同名条目会覆盖。旧数据仍保留为所属 Bot 的私有记忆，主 Bot 不再充当共享命名空间。模型可使用 `recall` 获取完整条目；系统只注入有上限的记忆预览，修改会在后续任务使用。
- 「长对话摘要」显示已有摘要和来源模型。历史超过 40 条或约 2.4 万字符时压缩较早对话，每个任务最多 3 次摘要调用并记录用量；原始消息不删除。摘要失败时明确提示，本次使用已有摘要及最近消息。摘要只是压缩背景，可能遗漏细节，不能等同无限上下文。
- 新回复下方显示实际调用连接及模型标识：API 以响应中的型号为准（未返回时使用请求型号），CLI 表示本次使用的引擎与模型设置。这不是 Bot 自述；供应商仍可能使用别名。流式正文约每 120 ms 保存一次，刷新或重连恢复同一条消息；中断保留部分文字并明确标注，不自动用另一模型拼接。忽略流式参数的兼容网关按完整响应显示，不伪造逐字动画。旧消息没有追溯补造型号。
- 输入框可上传每条最多 4 个附件、每个不超过 10 MB。支持未加密 PDF（最多 100 页）、DOCX，以及 UTF-8 TXT、MD、CSV、JSON、HTML。提取文本最多 50 万字符；长文件先提供预览，模型用 `read_attachment` 分段读取。PDF 仅提取文本层，没有 OCR；DOCX 提取正文及表格文字，不含嵌入图片、批注、页眉页脚。
- 图片支持 PNG、JPEG、WebP，边长最多 8192 px、总像素最多 2400 万。服务端验证图片内容，去除元数据并转换为最长边 2048 px 的 JPEG；动图只保留首帧。图片通过对应协议的视觉输入发送，所选型号需支持视觉；当前真实 API 的验收覆盖文本附件，图片理解质量需用视觉型号另验。
- Bot 可调用 `create_artifact` 生成 TXT、MD、CSV、JSON、HTML，生成成功后显示文件卡片。每次生成都是独立文件，不覆盖旧版本。「附件与成果」可查看、预览、下载；DOCX/PDF 显示提取文字，HTML 使用禁用脚本及外部资源的沙盒预览。下载始终要求与 API 相同的认证。尚未接入执行机文件回传，也未实现生成 Office/PDF 二进制文档。
- 文件元信息保存在 SQLite，内容在当前数据目录的 `attachments/`。使用不透明文件 ID，不允许模型访问任意后端路径。Bot 仅可读取本会话已发送的附件/成果；待发送文件可从输入框移除，已发送文件不被草稿清理删除。备份应包含数据库和整个数据目录。附件发送给所选云端模型时会离开后端 Mac。

2026-09-12 的隔离验收覆盖四种 API 类型的流式片段、工具参数拼接、推理签名保留、取消/恢复、记忆范围、文件解析/认证/跨会话边界及摘要原文保留。浏览器已操作桌面和 390 px 手机布局中的记忆编辑、附件预览下载、流式刷新恢复与成果下载。`glm-5.3-flash` 真实合成文件任务通过，3 次调用、2,416 tokens；没有读取用户原有对话或记忆，不代表 iPhone 真机已通过。

## 固定运行目录

当前 8899 服务已从临时预览目录迁入 `WBAI/.local/active/`，包括 `config/`、`data/` 和只读写给当前用户的 `.env`。迁移前副本保存在 `.local/backups/`，原项目 `config/`、`data/`、`.env` 未覆盖，整个 `.local/` 已排除版本控制。后续启动同一实例请在 `WBAI/` 下运行：

```bash
./.venv/bin/python -m carme.cli --profile .local/active serve --host 127.0.0.1 --port 8899
```

目前仍仅监听本机，尚未安装开机自启或对局域网开放。若已有更早的正式数据，请继续用对应实例；不混合两个数据库。设置中的「团队默认模型」控制未单独指定模型的成员；切换到真实 API 时为每个档位选择真实模型并关闭演示模型。

### 工作区与持久化契约

执行机的工作区分两级：**共享根 + 每任务子目录**。相对路径一律落在本任务的 `<root>/<bot-id>/<task-id>/`，这个目录同时是该次执行的默认工作目录和 `HOME`；要读写别的任务留下的产物，必须写从共享根开始的完整路径。这样接力任务能接着上一个任务的成果，又不会因为随手写相对路径污染别人的目录。共享根内可以自由读取，越出共享根一律拒绝（本机模式写入直接报错）。

维护动作分三档，不要混用：

| 档位 | 做什么 | 保留什么 |
|---|---|---|
| Update | 升级代码、重启服务 | 全部 `data/`（数据库、附件、成果、浏览器 profile）与 `.env` |
| Recover | 更换或重装执行电脑 | 后端数据保留；执行机上的工作区、网站登录、已装依赖需重新检查，不承诺自动迁移 |
| Reset | 只有明确要清空时才做 | 删除浏览器 profile 等于掉登录；`data/carme.db` 含会话与记忆，删前先备份 |

可以随时丢弃的是临时目录、pip / npm 缓存和未归档的中间文件。发给云端模型的对话与工具输出仍会离开后端 Mac。

## 接入或更换 2018 MacBook

现在还没有真实地址 / 用户 / SSH 密钥配置；不要把示例 IP 当成实际机器。

1. 在执行 MacBook 上开启 macOS「远程登录」，确认用户名、局域网地址及已有 SSH 公钥授权。
2. 从常驻后端 Mac 手动连接一次，核对执行电脑的主机指纹后写入 `known_hosts`。程序使用 `StrictHostKeyChecking=yes`，不会自动信任新主机。
3. 在执行机上为 Chrome 建立专用持久化用户目录，启用本地回环 CDP 端口（默认 9222）；登录需要的网站。后端经 SSH 隧道访问该端口。
4. 在 Carme「执行电脑」中编辑 2018 MacBook，填写地址、用户、端口、后端密钥文件路径及远端工作目录，保存并检查连接。
5. 将已配置的电脑设为默认，再运行真实任务。Chrome 与终端必须对应这台执行电脑。

新版配置使用 `config/sandbox.yaml` 中的 `nodes` 和 `default_node_id`，不再用旧 `modes.remote.host` 选择任务机器。命令行检查入口：

```bash
./.venv/bin/python -m carme.cli node check
```

更换电脑时添加新节点并切换默认。新任务使用新电脑，运行中的任务与子任务保持原来的连接快照。既有会话和任务记录保留在后端。网站登录、未归档文件和正在运行的程序不会自动迁移。

未配置 / 离线时不会自动在后端 Mac 执行。断开 SSH 或取消任务只保证停止后端等待；远端进程是否已退出需要核实，不能对有副作用的操作自动重试。同一执行电脑的根任务排队，同一父任务对该电脑的成员委派也串行，防止共享浏览器页面相互干扰。

## 手机访问

客户端使用同一后端地址。当前推荐的外网入口是 Cloudflare named Tunnel：cloudflared 只把固定域名转发到 `http://127.0.0.1:8899`，不改变 Carme 的运行地址，也不修改 Karing。应用入口必须同时通过 Cloudflare Access 和 Carme 的 `CARME_TOKEN`；Access 的 Allow 账号、域名、Tunnel 凭据和云端策略不由本项目代办。PWA Service Worker 只缓存带 Carme shell 标记的同源响应；Access 重定向或登录 HTML 不会覆盖离线壳。

### Cloudflare named Tunnel

先在 Cloudflare 账户中准备自己控制的域名、创建 named Tunnel 和 Access Application，再在「设置 → Cloudflare Tunnel」填写 Tunnel 名称或 UUID、已有 `credentials-file` 路径、固定域名、协议、Access Team Name 和 Audience Tag。网页只写入本地 `deploy/cloudflared/carme-tunnel.yml`，不会读取或上传凭据内容；该文件已被 `.gitignore` 排除。

如果已有配置含多个 ingress、路径路由或其他 `originRequest` 字段，网页会拒绝覆盖并保持原文件字节不变；请先人工备份并明确整理成 Carme 独占的单根路由。读取器和启动检查也不会把 `/foo`、重复 origin 或缺少 hostname 的路由当成有效入口。

在 `WBAI/` 目录执行：

```bash
./deploy/cloudflared/carme-tunnel.sh check
./deploy/cloudflared/carme-tunnel.sh start
./deploy/cloudflared/carme-tunnel.sh status
./deploy/cloudflared/carme-tunnel.sh diagnose http2
./deploy/cloudflared/carme-tunnel.sh diagnose quic
```

`check` 在启动前验证 cloudflared、Tunnel 配置、固定域名到 loopback origin、Access JWT 字段和活动 8899 服务的鉴权：无令牌请求必须是 401，正确 `CARME_TOKEN` 必须是 200。缺少令牌、凭据文件、Access 字段或实际后端重启时，脚本不会启动入口。优先单独验证 HTTP/2/TCP 7844；QUIC/UDP 7844 只有在 Karing 和网络允许时再诊断。脚本不会创建 Quick Tunnel、不会修改 Karing，也不会把 `CARME_TOKEN` 放进 SSE URL。

本地只需设置 `CARME_TOKEN` 到当前 `.local/active/.env` 并重启现有 8899 服务；不要把它粘贴到聊天、域名或分享链接。启动后仍要在 Cloudflare Dashboard 确认 Access Allow 只包含自己的账号，再用 iPhone Safari 打开固定域名，完成 Access 登录并分别验证聊天、SSE 重连、附件上传下载，最后「分享 → 添加到主屏幕」。安装成功或进程存在都不代表公网链路已验收。

相关官方文档：[运行命名 Tunnel](https://developers.cloudflare.com/tunnel/advanced/run-parameters/)、[originRequest Access JWT](https://developers.cloudflare.com/tunnel/advanced/origin-parameters/)、[Tunnel 防火墙端口](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)。

当前还没有 Web Push。手机锁屏后任务继续由后端运行，返回页面会补齐消息；关闭页面后的即时通知属于后续交付。

## 隔离验证

以下脚本使用临时配置 / 数据库和替代模型，不调用真实云端 API 或 SSH：

```bash
./.venv/bin/python scripts/test_conversations.py
./.venv/bin/python scripts/test_runtime.py
./.venv/bin/python scripts/test_nodes.py
./.venv/bin/python scripts/test_model_settings.py
./.venv/bin/python scripts/test_chat_files.py
./.venv/bin/python scripts/test_engines.py
./.venv/bin/python scripts/test_agent_engine_routes.py
```

`test_engines.py` 不调用真实模型，覆盖 CLI 参数边界、loopback MCP、图片内容、native JSONL 工具活动、进程组取消和错误脱敏；`test_agent_engine_routes.py` 覆盖 CLI-only Bot 保存、独立 CLI effort，以及失效 API 模型引用切换 CLI 的 HTTP 路由边界。

需要已安装 Playwright Chromium 时，可额外验证浏览器快照不会读取输入值：

```bash
CARME_LIVE_BROWSER_TEST=1 ./.venv/bin/python scripts/test_nodes.py
```

该可选检查启动新的临时浏览器资料目录，不读取已有登录态。

需要复验真实模型的委派和记忆工具时，可显式运行以下合成任务。它使用临时数据库和测试角色，最多 7 次模型调用，不读取用户聊天、记忆或执行电脑；会产生所选 API 的实际用量。`--model` 填入已配置模型的完整引用：

```bash
./.venv/bin/python scripts/test_model_settings.py --live-profile .local/active --model '供应商ID/模型ID'
```

本次所配置 `glm-5.3-flash` 的成功验收包括 4 次模型调用、2 个任务：主 Bot 委派成员、成员写入独立记忆、结果返回主 Bot。此结果不替代真实 SSH、浏览器操作或复杂任务质量验收。

`scripts/test_approval_gate.py` 另用临时本地页面验证 Chromium 真实点击与审批；本轮 21/21 检查通过。它不连接执行 MacBook。

可使用 `CARME_CONFIG_DIR`、`CARME_DATA_DIR` 和 `CARME_ENV_FILE` 指定独立验证目录及 `.env` 路径，`CARME_LOAD_ENV=0` 禁用读取 `.env`。测试模型保存功能时必须把 `CARME_ENV_FILE` 也指向临时文件。已有 `data/carme.db`、浏览器资料、`.env` 均应保留；正式切换前先停止旧服务并备份整个数据目录及配置。首次加载旧数据库会做加法迁移，并将遗留活动任务标为中断失败，不自动重放此前操作。
