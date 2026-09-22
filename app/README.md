# Carme

自托管的 Bot 团队。模仿 Grok Bot 的持续聊天与三栏界面，通过同一网页应用 / PWA 在 iOS 和 macOS 上使用。

**Bot 使用的执行电脑目前暂定为 MacBook，后续可以替换。** 它与保存任务的常驻后端 Mac、用户使用的客户端是三个不同角色。

## 当前实现（0.2.0，2026-09-12）

| 能力 | 状态 |
|---|---|
| 桌面三栏、手机单栏导航、成员和会话搜索 | 已实现，桌面和手机尺寸浏览器已检查 |
| 创建 / 编辑 Bot、创建单聊与群聊 | 已接入真实配置与 SQLite |
| 模型连接管理、模型列表、effort 选择 | 已接入网页设置、接口测试与配置保存；当前真实 API 已验证工具调用链路，其他连接需逐个验收 |
| 团队默认模型、演示模型开关 | 网页可分别配置各档位，保留 Bot 单独指定的模型 |
| 右键菜单、分组、隐藏和恢复 | 已实现；删除先移到最近删除，运行中的对话需先停止任务 |
| 字体与字号 | 设置 → 外观，字体列表读自运行 Carme 的这台机器的系统字体；立即生效并保存在当前设备 |
| Bot 头像：形状 / 颜色 / 随机生成 / 图片上传 | 已实现，上传格式和大小限制在页面说明 |
| 执行中的头像动画与旋转彩带 | 已按真实任务状态接入，隔离任务验证完成、停止及切换聊天后的更新 |
| 持续追问、模型执行期间追加要求、成员委派 | 隔离测试已通过；真实 API 已通过委派、记忆写入及结果回传的合成任务 |
| 持久化消息、请求幂等、SSE 重连补消息 | 已验证重复提交、队列溢出和重启边界 |
| 私有 / 团队共享记忆管理、长对话摘要 | 已接入网页查看、编辑、删除与显式共享；摘要保留完整聊天原文 |
| 流式正文、实际模型标识 | Chat Completions、Responses、Anthropic 均已隔离验证，部分回复中断状态可恢复 |
| 文档 / 图片附件、成果预览下载与后端归档 | 已开放下述格式；真实 API 已完成读取附件→生成 CSV→归档的合成任务 |
| Cloudflare named Tunnel、固定域名、Access 配置页和安全启动检查 | 源码与隔离检查脚本已实现；Cloudflare 账户、固定域名、Access Allow 规则和 iPhone 真机仍待本人完成 |
| 使用 `/Users/you/Downloads/logo.png` 的品牌图标与 PWA 图标 | 已生成网页、favicon、Apple Touch、192/512 和 maskable 资源；安装效果待真机验收 |
| 任务取消、预算失败、过期待办清理 | 已实现，取消和重启清理遗留审批 |
| 登记执行电脑、设默认、固定任务机器、子任务继承 | 已实现；目前只有未配置的 2018 MacBook 草稿 |
| 远端 SSH 与 Chrome CDP | 已实现连接代码和隔离边界测试，尚未接真实 MacBook |
| Codex / Pi / Claude 本机 CLI 引擎 | 已接入任务级工作目录、受控 Carme 工具桥接、native 活动与取消清理；Codex / Claude 已完成隔离真实链路，Pi 认证仍待配置 |
| PWA manifest / 离线页面外壳 | 已构建；iPhone 安装、HTTPS 和后台表现待真机验收 |
| 本机屏幕查看与远程鼠标控制（macOS） | 已实现：Pillow 屏幕抓帧 + Quartz 鼠标注入；手机端单指拖动＝相对位移移动电脑鼠标（不与电脑原光标位置冲突），长按后拖动＝按住左键拖动（选文字、拖窗口/文件），单指点按＝在电脑当前光标位置单击，双指＝右键。需授予屏幕录制 / 辅助功能权限；隔离账号经已配对且授权的 Mac Runner 实现「Bot 的电脑」实时 Mac 屏幕 + 勾选鼠标键盘人工控制（按需抓帧约 2–3 fps、控制动作经签名通道中继、租约 ≤600 秒、人工接管立即撤销）；未授权 Runner 时自动退回 Docker 浏览器画面流 |
| 例行任务、Web Push、语音 | 尚未开放，界面不模拟成功 |
| 技能（Skill）安装与调用、外部 MCP Server 接入 | 已实现，入口在「探索 Bot」的三个标签页；四个安装来源、stdio / HTTP 两种 MCP 传输均有隔离验收 |
| 第三方技能市场、示范教学、桌面视觉自动操作 | 后续阶段（自建技能与 MCP 已可用，见「技能与 MCP」） |

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

## 技能与 MCP

两种「把外部能力装进来」的方式，都在网页侧边栏的**探索 Bot** 里管理（三个标签页：Bot 团队 / 已安装的 Skill / 已安装的 MCP）。

### 技能（Skill）

一个技能就是一个目录，里面有 `SKILL.md`：frontmatter 写 `name` 和 `description`，正文写「怎么做事」，脚本和模板放在同一个目录下。

```markdown
---
name: PDF 报告
description: 用脚本把 CSV 渲染成 PDF 报表
version: "1.2"
---
# 步骤
1. 读 data.csv
2. 运行 scripts/render.py
```

- **四个安装来源**：粘贴 Markdown、本机路径（目录或 `.md` 文件）、http(s) 地址、GitHub 仓库（`owner/repo`，可用子目录指定仓库内的技能目录）。内容落在 `<数据目录>/skills/<id>/`，安装来源与启停状态记在 `config/skills.yaml`；目录才是内容的事实来源，手工删掉目录列表里就少一个。
- **渐进式披露**：只有「名字 + 一句话用途」进系统提示，正文由模型调用 `use_skill` 按需读取，所以装几十个技能也不会把每一步的 prompt 撑爆。`list_skills` 用来在描述不够时看全貌。
- **谁来用**：技能工具属于 `skill` 工具分组（`list_skills` / `use_skill`）。Bot 的「可用工具」里要有 `skill`，它才会看到技能清单 —— 没给就完全不提，避免它去幻觉一个读不到的技能。面板里会显示哪些 Bot 已开启，并提供一个「为全部 Bot 打开」的按钮（等价于手工在 `agents.yaml` 的 `tools` 里加 `skill`）。
- **边界**：只复制普通文件，符号链接、`.git`、`__pycache__` 会被跳过；单个 `SKILL.md` 上限 512 KB，一个技能目录上限 32 MB / 400 个文件；GitHub 压缩包里的 `../` 与符号链接成员一律丢弃。`config/skills.yaml` 的 `roots` 可以再加只读技能目录（例如团队共享目录），Carme 只读不删。

### MCP（Model Context Protocol）

连接外部 MCP Server，把对方的工具变成 Bot 能调用的工具。

| 传输 | 说明 |
|---|---|
| `stdio` | 在**常驻后端 Mac 上**拉起一个常驻子进程，用换行分隔的 JSON-RPC 通信（`npx` / `uvx` / 绝对路径都行） |
| `http` | 用 POST 发 JSON-RPC，兼容 streamable HTTP 的 JSON 与 SSE 两种应答 |

- **工具命名**：`mcp__<server>__<tool>`。外部工具名里的非法字符会替换成 `_`，超长会截断并加哈希后缀，保证符合各家模型的函数名约束。
- **谁来用**：MCP 工具属于 `mcp` 工具分组，展开成「当前已连上的全部 MCP 工具」。Bot 的「可用工具」里要有 `mcp`（面板同样有一键打开）。没有连上的服务器不会把死工具留在工具表里。
- **生命周期**：`enabled` 的服务器在后端启动时后台自动连接，连不上就记下原因并保持 `error` 状态（定义不丢，改完可以重连）；调用时发现进程已退出会按需重连一次。断开连接会把它的工具从工具表里摘掉。
- **审批**：每个 Server 可选「每次调用需人工确认」，打开后每次工具调用都走既有人工确认闸门（超时按拒绝处理）。默认 `auto`：装了就等于授权。
- **密钥**：`env` / `headers` 的值只写在后端 `config/mcp.yaml`（权限 600），接口只回显变量**名**，界面留空表示沿用已保存的值。编辑界面里命令、参数、工作目录、地址按结构化字段原样回填，参数里的空格不会被拆坏。
- **安全**：stdio 服务器是后端 Mac 上的真实进程，权限等于运行 Carme 的那个账号。装哪个 Server 就是一次授权动作，只装你信任的；不需要时用停用或删除，而不是留着不看。

接口：`GET/POST/PATCH/DELETE /api/skills*`、`POST /api/skills/reload`、`GET/POST/PUT/PATCH/DELETE /api/mcp/servers*`、`POST /api/mcp/servers/{id}/connect|disconnect`、`POST /api/tool-groups/grant`，全部与其它 `/api` 路由共用同一个令牌校验。

## 构建和启动

以下在**常驻后端 Mac** 的本项目目录操作：

```bash
cd '/path/to/carme'
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

## 本机浏览器与桌面控制

Carme 支持两种浏览器形态：**未配置远端执行电脑时**，`web_open / web_click` 等代操作直接在本机启动可见 Chromium（`headless: false`，登录态保存在 `data/browser/<身份名>/`）；配置并设为默认执行电脑后，则经 SSH 隧道连接远端 Chrome CDP，不会静默回退本机。所有 Bot 默认拥有 `browser` + `computer` 权限（`config/agents.yaml`）。

「执行电脑」面板顶部提供**本机屏幕**：约 1 秒间隔显示后端 Mac 主屏，可开启**鼠标与键盘**开关（配置闸门 `desktop.control_enabled` 打开时才可开启）。

**浏览器隔离账号的降级画面**：启用了浏览器隔离（`config/isolation.yaml` 有 `browser:` 段）的账号没有本机桌面权限，「执行电脑」面板、全屏弹窗和详情页缩略卡会自动把画面源从 `GET /api/desktop/screenshot`（本机抓屏）切换到 `GET /api/browser/screenshot`——展示该账号 Bot 最近一次浏览器操作截图（Docker Browser 回传归档、`kind='browser_shot'` 的 PNG），轮询刷新、画面未变化时返回 304 省流量。画面**只读**：不提供鼠标键盘注入，相关开关在浏览器模式下隐藏。`/desktop/status` 的 `mode` 字段（`local` / `browser`）驱动这一切换，无活跃截图时显示"等待 Bot 产生浏览器画面"占位。


手机 / 触屏上的操作方式（画面即触控板）：

- **单指在画面上拖动 = 移动电脑鼠标**：手指位移只在手机端按画面显示比例换算成**相对位移**下发，从电脑当前光标位置继续走，不使用绝对坐标，因此不会与被控电脑原本的鼠标位置（或用户自己刚动过的光标）互相打架，也不会一拖就乱跳。
- 单次下发上限 600 像素（后端硬上限 1200），长滑动自动拆成多段依次下发；被节流窗口攒下的最后一段位移会在松手前补齐，指针不会停在手指后面。
- **单指点按 = 在电脑当前光标位置左键单击**（不带坐标，不会把光标瞬移到手指按下的位置），双指点按 = 右键。拖动超过 12 像素后松手不会补点击。
- **按住不动约半秒再拖动 = 按住左键拖动**：长按判定（450ms、手指位移 ≤10 像素）通过后，先给电脑发一次 `press`（左键按下，位置取电脑当前光标位置），之后的单指拖动就是真正的拖拽——选文字、拖窗口或文件都可用；松手时 `release` 排在最后一段位移之后下发，拖拽不会提前结束。按住期间画面光标变成实心点，工具栏显示「按住左键中」。
- 按键状态由后端跟踪：关闭「鼠标与键盘」、重载配置、服务退出、页面卸载/切后台、超过 90 秒没有任何后续动作，都会自动松开，不会把鼠标键留在电脑上按下；`status` 接口会报告 `pressed` 字段便于排查。
- **画面光标（手机上看到的那一个）只由手指位移累加驱动**：拖动中 1:1、平滑连续，不做任何「以电脑真实位置为准」的吸附，所以不会回跳；手指停下的一瞬间它就停住，松手后不会再自行移动一小段。与电脑真实位置的收敛只在**完全空闲**时（超过 2.5 秒没有鼠标动作、且没有在途位移）由状态轮询完成——电脑端自己移动鼠标时，光标点也会这样跟过去。
- 桌面浏览器上仍是绝对定位：鼠标移动 / 单击 / 右键 / 滚轮，坐标按真实屏幕分辨率换算。相对拖动使用时不会被限制在主屏内，光标在第二块屏幕上也不会被拉回主屏。
- 实现要点（踩过的坑）：`CGEventPost` 之后立刻用 `CGEventGetLocation` 回读光标会**滞后若干步**（实测约 250ms 才稳定），拿它当下一次位移的基准会把前面的位移覆盖掉，表现就是「一拖就乱跳」。后端因此记住「我们让光标去的目标位置」和最近几步实际生效的位移，按「回读 = 认知 − 最近 k 步位移之和」识别滞后并沿用认知；只有确认不是滞后时才采用真实回读（尊重用户自己搬动的光标）。返回值也是「目标位置」，不再是立刻回读值。
- 实现要点（第二坑，就是「乱跳 + 松手后又走一段」的成因）：回读值永远滞后于在途请求（手机到后端的往返越长越明显）。早期实现用「回读位置 + 未发送位移」反推画面光标，回读一到就重算，于是光标在在途量之间来回跳、松手时又被拉回真实位置"补走"一段。现在画面光标是纯累加器，回读值只用于后端下一次相对位移的基准和完全空闲时的校正。

验证方式（都不需要真机，也不会真的移动这台 Mac 的鼠标）：

```bash
./.venv/bin/python scripts/test_desktop_control.py     # 假 Quartz 隔离测试：相对位移语义、滞后回读、多屏钳位
./.venv/bin/python scripts/test_desktop_touch_e2e.py   # 真实 Chrome 触摸事件驱动页面；鼠标请求被拦截，不落到后端
```

### macOS 系统权限（必须手动添加，通常不会弹授权窗）

Carme 的「本机屏幕画面 / 远程鼠标键盘」走 macOS 截屏与 CGEvent 注入，需要**屏幕录制**和**辅助功能**两项权限。系统不会弹授权窗，必须手动加：

| 能力 | 需要的权限 |
|---|---|
| 本机屏幕画面 | 屏幕录制（Screen Recording） |
| 远程鼠标控制 | 辅助功能（Accessibility） |

**授权对象是“责任进程”，也就是启动 Carme 的那个 app，不是 Python 本身。** macOS 把子进程的权限请求归因到最外层的图形应用（tccd 日志里直接给出 `responsible_path=`，`./start-carme.command --diagnose` 会读它并打印结论）：

| 启动方式 | 「屏幕录制」「辅助功能」里要加的对象 |
|---|---|
| `./start-carme.command --service`（launchd 托管，推荐） | 解释器真实路径，例如 `/Users/you/.workbuddy/binaries/python/versions/3.13.12/bin/python3.13` |
| 双击 `start-carme.command`（终端启动） | 「终端」`/System/Applications/Utilities/Terminal.app` |
| 从 WorkBuddy / PI-Desktop 等宿主启动 | 该宿主 app，例如 `/Applications/WorkBuddy.app`（com.tencent.workbuddy.mac） |

「已经授权却仍然报没有权限」几乎都是授权授给了 Python 或别的 app，而实际请求来自上面的宿主。不确定时让脚本判断：

```bash
./start-carme.command --diagnose      # 只体检：会抓一次截屏并从 tccd 日志读出该授权哪个 app
```

在权限列表点 ➕ 后按 ⌘⇧G 粘贴路径；**改完必须重启服务**才生效：`./start-carme.command --restart`。加了仍黑屏可先 `tccutil reset ScreenCapture` 清残留。

`--service` 托管的两条实测坑（本项目目录在 `~/Documents` 下时必然遇到）：

- `~/Documents`、`~/Desktop`、`~/Downloads` 属于 TCC 保护目录，launchd 的 `xpcproxy` 既不能在其中读取程序、也不能在其中创建 `StandardOutPath` / `StandardErrorPath`，否则任务以 `posix_spawn(...) Operation not permitted`、退出码 78 反复失败。所以脚本注册的 LaunchAgent 用 Documents 之外的解释器真实路径（`.venv/bin/carme` 是项目内的 shell 包装器，指向它会失败），日志写到 `~/Library/Logs/carme/serve.log`。
- 权限变更后只重载 plist 不够，必须让进程真正重启（脚本用 `launchctl kickstart -k`）才会重新读取授权状态。

`--uninstall-service` 可取消托管，回到 `daemonize_serve.py` 的后台启动（那种方式归因跟随启动它的 app）。

### 依赖与配置

```bash
./.venv/bin/pip install -e '.[browser,desktop]'
```

- 浏览器代操作：`config/browser.yaml`（`headless`、`profiles`、危险词审批、域名黑白名单）。`channel: chrome` 可让 Bot 使用 Google Chrome 正式版（本机需已安装 Chrome；Docker Browser 镜像需为含 Chrome 的构建，见 `app/deploy/docker/Dockerfile.browser`）。
- 桌面：同文件 `desktop` 段（`enabled`、`control_enabled`、`jpeg_quality`、`max_fps`）。`control_enabled` 只是允许打开控制的配置闸门，实际控制状态仅存于进程内，**服务重启后自动关闭**。
- 安全：桌面接口只挂在现有 Carme API（Bearer 令牌 / 会话 Cookie），不新增监听端口，令牌不出现在 URL 或图片地址；请勿把后端端口暴露到不可信网络。原始鼠标控制只提供给已认证的人工前端，不会下发给模型。

## 模型连接与 Bot 头像

在「设置 → 模型 → 添加模型连接」填写连接名称、API 类型、基础网址和 API key。支持 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages，以及提供 OpenAI Chat Completions 协议的其他兼容服务。

1. 点击「测试连接」，后端读取该凭据可见的模型列表；失败时显示错误，不保存半成品。
2. 勾选模型并选择 effort。接口提供能力信息时直接读取；未知时可点击「检测 effort」，仅列出接口实际接受的选项。对静默忽略参数的网关不宣称已确认支持，可保留模型默认。
3. 点击「验证并保存所选模型」。后端逐个发送固定测试句验证所选配置，成功后写配置。检测和验证可能产生少量 API 费用，不包含用户聊天内容；工具调用能力仍需真实任务另行验收。
4. 在每个 Bot 的设置中选择「使用的 Agent（引擎）」和「使用的大模型」：Agent 列表只列出当前已连接的引擎（Carme API 网关需要有可用 key，CLI 要装好且已登录），模型列表只列出已经配置好 API key 的模型、按连接分组，effort 跟随所选模型。已经保存但暂时不可用的选择会保留在列表里并标注，不会被静默改掉。切换保留 Bot 的固定 ID、角色、会话、摘要和长期记忆，运行中的任务继续使用其角色配置快照，新任务使用新选择。不同模型自身的上下文容量仍有差异。

密钥仅写后端 `.env`，YAML 保存变量引用，API 不回显密钥。编辑连接时留空保留已存密钥；更换网址或 API 类型后需重新输入，防止旧密钥被发给新地址。旧的手工配置继续读取。当前自动发现需要供应商实现相应的模型列表接口。

参数依据：[OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[Anthropic 模型能力](https://platform.claude.com/docs/en/api/models)、[Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort)。型号和支持等级从连接取得，不写死品牌型号表。

点击 Bot 头像打开编辑面板：Bot 页选择 8 种形状、11 种颜色；生成页在本地随机组合；上传页支持 PNG、JPEG、WebP 静态图片，最大 5 MB，宽高均为 32–4096 像素，建议 512×512。图片居中裁切并重新编码为 512×512 WebP，去掉元数据，保存在后端数据目录的 `avatars/` 中，读取受访问令牌保护。预览后点击「保存修改」才绑定到 Bot；取消不会更换 Bot 头像，重置使用原 Emoji。

现有头像和设置保持不变。仅在任务执行时，按自带头像几何生成有体积的立体形状，绕模型空间竖直的 Z 轴旋转，头像上沿显示彩带；上传图片、Emoji 和群聊图标保持静止，只显示彩带。侧边栏、聊天标题、执行提示与当前成员详情同步显示，历史消息头像保持静止。动画依据后端 `running` 状态，排队、等待确认、完成、失败和取消恢复原头像；开启系统「减少动态效果」时展示静态立体形状、彩带和文字提示。

## 聊天菜单与外观

右键侧边栏中的 Bot，或点击旁边的「更多」按钮，可置顶、移至新建或已有分组、标为未读、重命名 Bot、编辑资料、创建副本、复制对话 ID、隐藏及删除对话。手机增加长按入口；触摸手势仍需 iPhone 真机验收。键盘可用 Shift+F10 打开菜单，方向键选择，Escape 关闭。

创建副本会复制角色、模型、工具权限与头像，并新建独立 Bot 和空白对话；不复制原 Bot 的记忆和聊天。重命名 Bot 会同步显示于其单聊入口。群聊提供群聊重命名，资料编辑和复制 Bot 仅用于单聊。

「删除」只将本段对话移到最近删除，不删除 Bot 的角色和记忆，也不影响其他对话。隐藏和删除均可从侧边栏的「隐藏与最近删除」恢复；当前没有自动清空或永久删除操作。状态保存在后端，两端共用。

「设置 → 外观」的字体列表来自运行 Carme 的这台机器：后端扫描系统字体目录（macOS 含按需下载的系统字体），按比例字体与等宽字体分两组给出，另有系统默认、苹方、宋体、楷体和等宽等内置兜底项；列表较长时可用搜索框过滤。手机或另一台电脑上没装的字体会自动回退到替代字体。基准字号 14–22 px，界面文字按比例调整；保存在当前浏览器，因此 iPhone 和 Mac 可各自选择合适字号。点击「重新检测」重新扫描字体目录，点击「恢复默认」回到系统字体、16 px。

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

### 换 logo / 修复 iOS 主屏图标

图标清单固定在 `web/` 根目录：`icon-512.png`（源）、`icon-192.png`、`apple-touch-icon.png`（180，iOS 主屏用它）、`favicon.png`（32）、`icon-maskable.png`（Android 自适应）、`manifest.webmanifest`。换图标的完整流程：

1. 用新图覆盖 `web/icon-512.png`，再用 Pillow 生成其余尺寸（全部 RGB、无 alpha；maskable 把内容缩到约 78% 居中、外圈填底色，避免被系统裁掉边角）。
2. 把 `web/index.html` 与 `web/sw.js` 里的 `?v=` 同时加一（现在都是 `v=3`）——手机与浏览器按 URL 缓存图标，不加版本号就抓不到新图；SW 的 `CACHE` 也要一起加一，旧缓存会在 activate 时清掉。
3. `npm --prefix web run build`。

`web/vite.config.ts` 的 `carme-public-files` 插件会把 Vite 生成的 `/assets/apple-touch-icon-<hash>.png` 改回 `/apple-touch-icon.png` 这类**稳定路径**，并删掉重复的哈希副本。哈希名每次构建都会变，之前那版就是因此让已加到 iOS 主屏的图标失效的。改动后手机上要**先删除旧图标再重新「添加到主屏幕」**，iOS 才会重新抓取。

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

### 固定域名的托管方式（本机现状，2026-09-14 实测）

`./start-carme.command` 已经把入口一起带上：默认启动流程会确保云端隧道有一个 launchd 托管的连接器（`com.carme.tunnel`）并体检公网入口，浏览器默认打开固定域名（`--local` 改为只开本机地址）。

| 项目 | 位置 / 值 |
|---|---|
| 固定域名 | `https://carme.example.com`（Cloudflare Access，team `your-team`） |
| 隧道 | `00000000-0000-0000-0000-000000000000` |
| 路由 | `carme.example.com` → `http://127.0.0.1:8899` |
| 本地配置 | `app/deploy/cloudflared/carme-tunnel.yml` |
| 凭据 | `~/.cloudflared/<tunnel-id>.json`（权限 600，不进仓库） |
| 托管 / 日志 | `~/Library/LaunchAgents/com.carme.tunnel.plist` / `~/Library/Logs/carme/tunnel.log` |

隧道相关命令：`--tunnel` 重装并重启隧道、`--tunnel-stop` 停止、`--no-tunnel` 只起本机服务、`--diagnose` 体检（含公网入口状态；302=Access 在保护，000=连不上）。

三个实测注意点：

- **云端配置优先**：该隧道在 Cloudflare Dashboard 上有 remotely-managed 配置，连接器启动时会拉取它并覆盖本地 `ingress`（日志里出现 `Updated to new configuration`）。改端口或域名要两边一起改，否则以云端为准；本地文件是云端配置缺失时的兜底。
- **origin 写 `127.0.0.1` 而不是 `localhost`**：macOS 上 `localhost` 先解析到 `::1`，而 Carme 只监听 IPv4 的 `127.0.0.1`，写 `localhost` 会让每次建连先失败一次。
- **入口准入由边缘 Access 负责**：网页「设置 → Cloudflare Tunnel」可能提示 `Access JWT 校验尚未写入该 ingress`，那是它按本地文件判断的，实际入口已由 Cloudflare Access 应用保护。若要再加一层 ingress 级 JWT 校验，需把 `originRequest.access`（`required: true`、`teamName: your-team`、对应 `audTag`）写进本地 ingress **并删掉 Dashboard 上的远端配置**，否则远端配置仍会覆盖它。

手机端提示「Cloudflare Access 登录已过期或尚未完成」时：用 Safari 打开 `https://carme.example.com` 走完 Access 登录即可。这条提示同时也是“请求拿到 HTML/重定向或 SSE 断开”的通用文案，所以 Carme 自己的会话 cookie（网关入口 7 天，`POST /api/login` 下发；直连模式的 `carme_session` 同为 7 天）过期后刷新页面同样能恢复。要让这 7 天真正生效，必须同时在 Zero Trust → Access → Applications 里把该应用的 Session Duration 设为 7 天或更长（Cloudflare 上限 30 天）：Carme 会话有效期取自身 Cookie 与 Access JWT `exp` 的较小值，Access 侧更短就会先要求重新登录。

相关官方文档：[运行命名 Tunnel](https://developers.cloudflare.com/tunnel/advanced/run-parameters/)、[originRequest Access JWT](https://developers.cloudflare.com/tunnel/advanced/origin-parameters/)、[Tunnel 防火墙端口](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)。

**连接器失效模式与加固（2026-09-22）**：连接器可能出现「进程活着、但 Cloudflare 边缘侧的注册状态失效」——表现为公网 502 Bad gateway（错误页上 Host 那一腿 Error），而 `~/Library/Logs/carme/tunnel.log` 一片空白、`cloudflared_tunnel_total_requests` 计数不动，源站三跳其实都健康；重启连接器即恢复。因此：① 已装 `com.carme.tunnel-restart`（每日 04:30 优雅重启，先 SIGTERM 等退出再 `kickstart`，**不要用 `kickstart -k`**，SIGKILL 不向边缘注销旧连接、反而可能再造出这个状态）；手工等价命令 `sh app/deploy/cloudflared/carme-tunnel-restart.sh`（launchd 无法执行 `~/Documents` 下的脚本，所以 plist 内联同一段命令）。② 连接器已加 `--edge-ip-version 4`（这条链路上 UDP/QUIC 被挡，预检 FAIL）。③ 待收尾：把云端 Tunnel 的 Public Hostname 由 `http://localhost:8899` 改为 `http://127.0.0.1:8898`，再停用 `com.carme.serve`（8899→8898 的 `loopback-forward.py`），历史 304 次 `Unable to reach the origin (read tcp [::1]:8899: connection reset by peer)` 都发生在它身上。

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
./.venv/bin/python scripts/test_migration.py
./.venv/bin/python scripts/test_fonts.py
./.venv/bin/python scripts/test_extensions.py
```

`test_fonts.py` 用合成字体文件覆盖 TTF / 字体集合解析、简繁中文名优先级、等宽判定、隐藏与坏文件容忍、目录为空时的回退，以及 `/api/fonts` 的令牌校验与 `/api/fonts/reload` 重新扫描；`test_engines.py` 不调用真实模型，覆盖 CLI 参数边界、loopback MCP、图片内容、native JSONL 工具活动、进程组取消和错误脱敏；`test_agent_engine_routes.py` 覆盖 CLI-only Bot 保存、独立 CLI effort，以及失效 API 模型引用切换 CLI 的 HTTP 路由边界；`test_migration.py` 覆盖导出包结构（角色 / 记忆 / 对话上下文）、导入合并策略、跨设备依赖缺失时的逐项降级、内嵌头像以及坏包与超量边界。`test_extensions.py` 用合成技能目录、内存里的 GitHub 压缩包和一个假 MCP Server（stdio 子进程 + 本地 HTTP/SSE 端点）覆盖技能的四种安装来源、路径穿越与符号链接拒绝、启停与只读目录保护，MCP 的校验 / 连接 / 工具注册与注销 / 断线重连 / 工具名安全化，以及 `/api/skills`、`/api/mcp`、`/api/tool-groups/grant` 的令牌校验、密钥不外泄和分组授权；不联网，也不读写真实 `config/` 与 `data/`。

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
