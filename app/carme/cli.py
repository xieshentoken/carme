"""命令行入口。

    carme serve              起服务（界面 + API）
    carme run "目标"          直接跑一个任务，结果打到终端
    carme doctor             环境自检（新机器上第一步就跑这个）
    carme agents             看成员花名册
    carme node check         测远端节点通不通
    carme models probe       列出你手上的 key 真实可用的模型
    carme tasks              看最近的任务
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from . import config as config_module


def _fmt_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "（空）"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = "\n".join(
        "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)) for row in rows
    )
    return f"{line}\n{sep}\n{body}"


# --------------------------------------------------------------------------- #


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ["CARME_BIND_HOST"] = args.host
    print(f"\n  Carme 界面：http://{args.host}:{args.port}\n")
    uvicorn.run(
        "carme.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level.lower(),
        access_log=False,
        timeout_graceful_shutdown=5,
        timeout_keep_alive=120,
    )
    return 0


def cmd_agents(_args: argparse.Namespace) -> int:
    config = config_module.load()
    rows = []
    for spec in config.agents.agents.values():
        usable = config.models.candidates(spec.tier)
        rows.append(
            [
                "▶" if spec.entry else " ",
                spec.id,
                f"{spec.emoji} {spec.name}".strip(),
                spec.title,
                spec.tier,
                usable[0] if usable else "（无可用模型）",
                spec.sandbox,
                ",".join(spec.tools),
            ]
        )
    print()
    print(_fmt_table(["入口", "ID", "名称", "职位", "档位", "首选模型", "环境", "工具"], rows))
    print()
    entry = config.agents.entry_agent
    print(f"  你在界面上的默认对接人：{entry.display}（{entry.id}）\n")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    """环境自检。换机器部署时第一件事就是跑它。"""
    import platform
    import shutil

    config = config_module.load()
    ok = True

    print("\n═══ Carme 环境自检 ═══\n")

    # 系统
    print(f"  系统      {platform.system()} {platform.release()}　Python {platform.python_version()}")
    mem_bytes = 0
    try:
        import subprocess

        mem_bytes = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip())
        print(f"  内存      {mem_bytes / 1024**3:.1f} GB")
        if mem_bytes < 8 * 1024**3:
            print("  ⚠ 内存偏低，建议把 max_concurrent_sandbox 保持为 1")
    except Exception:  # noqa: BLE001
        print("  内存      （非 macOS，跳过）")

    # 供应商
    print("\n  ── 模型供应商 ──")
    ready = 0
    for name, provider in config.models.providers.items():
        if provider.is_local:
            print(f"  · {name:<12} 本地服务（可选）")
            continue
        mark = "✓" if provider.available else "·"
        detail = "已配置 key" if provider.available else f"缺 {provider.api_key_env}"
        if provider.available:
            ready += 1
        print(f"  {mark} {name:<12} {detail}")
    if ready == 0:
        ok = False
        print("\n  ✗ 一个可用的供应商都没有。复制 .env.example 成 .env 并填入 API key。")
    else:
        print(f"\n  {ready} 个供应商可用")

    # 档位
    print("\n  ── 模型档位 ──")
    for tier in config.models.tiers:
        candidates = config.models.candidates(tier)
        used_by = [s.id for s in config.agents.agents.values() if s.tier == tier]
        if candidates:
            print(f"  ✓ {tier:<10} → {candidates[0]}　（{len(candidates)} 个候选）")
        else:
            ok = False
            print(f"  ✗ {tier:<10} → 没有可用候选　（使用方：{', '.join(used_by) or '无'}）")

    # 工具链
    print("\n  ── 工具链 ──")
    for name, cmd in [("docker", "docker"), ("ssh", "ssh"), ("git", "git")]:
        path = shutil.which(cmd)
        print(f"  {'✓' if path else '·'} {name:<8} {path or '未安装（按需）'}")

    # doctor 只检查配置；真实连接由用户调用 node check。
    print("\n  ── Bot 的执行电脑 ──")
    try:
        node = config.sandbox.resolve_node()
        print(f"  · {node['name']} 已配置；运行 carme node check 检查真实 SSH / Chrome 连接")
    except ValueError as exc:
        print(f"  · {exc}（普通聊天仍可使用）")
    built = config_module.PROJECT_ROOT / "web" / "dist" / "index.html"
    print(f"  {'✓' if built.exists() else '✗'} 网页构建 {'已存在' if built.exists() else '缺少：在 web 目录运行 npm ci 和 npm run build'}")
    ok = ok and built.exists()

    print("\n" + ("═══ 自检通过 ═══\n" if ok else "═══ 有问题需要处理，见上面 ✗ ═══\n"))
    return 0 if ok else 1


async def _probe_sandbox(config, mode: str) -> dict:
    from .sandbox import SandboxManager

    manager = SandboxManager(config)
    if mode == "remote":
        return await manager.probe_node()
    return await manager.probe(mode)


def cmd_run(args: argparse.Namespace) -> int:
    return asyncio.run(_run_task(args))


async def _run_task(args: argparse.Namespace) -> int:
    from .bus import EventBus
    from .runtime import Runtime
    from .store import Store

    config = config_module.load()
    store = Store(config_module.DATA_DIR / "carme.db")
    bus = EventBus()
    runtime = Runtime(config, store, bus)

    agent_id = args.agent or config.agents.entry_agent.id

    # 把执行过程实时打出来，不用盯着界面
    async def printer():
        queue = await bus.subscribe()
        try:
            while True:
                event = await queue.get()
                etype = event["type"]
                payload = event.get("payload") or {}
                if etype == "tool.start":
                    name = payload.get("tool", "?")
                    detail = payload.get("command") or payload.get("query") or payload.get("url") or payload.get("path") or ""
                    print(f"    ⚙ {name}: {str(detail)[:110]}")
                elif etype == "delegate.start":
                    print(f"    ↳ 派给 {payload.get('to')}：{payload.get('title')}")
                elif etype == "delegate.end":
                    print(f"    ↲ {payload.get('to')} 完成")
                elif etype == "task.finished" and event.get("task_id") == task_id:
                    return
        except asyncio.CancelledError:
            return

    task_id = await runtime.submit(agent_id, args.goal, source="cli")
    print(f"\n  任务 {task_id} 已交给 {agent_id}\n")

    watcher = asyncio.create_task(printer())
    task = store.get_task(task_id)
    while task and task["status"] not in ("done", "failed", "cancelled"):
        await asyncio.sleep(0.5)
        task = store.get_task(task_id)

    watcher.cancel()

    if not task:
        print("  任务记录丢失")
        return 1

    print(f"\n{'═' * 70}")
    print(f"  状态：{task['status']}　耗时：{(task['finished_at'] or 0) - (task['started_at'] or 0):.1f}s"
          f"　成本：${task['cost_usd']:.4f}　tokens：{task['tokens']}")
    print(f"{'═' * 70}\n")
    print(task["result"] or task["error"] or "(无输出)")

    children = store.children_of(task_id)
    if children:
        print(f"\n── 子任务 {len(children)} 条 ──")
        for child in children:
            print(f"  [{child['status']:<8}] {child['agent_id']:<10} {child['title'][:60]}")

    await runtime.shutdown()
    store.close()
    return 0 if task["status"] == "done" else 1


def cmd_tasks(args: argparse.Namespace) -> int:
    from .store import Store

    config = config_module.load()
    store = Store(config_module.DATA_DIR / "carme.db")
    tasks = store.list_tasks(limit=args.limit)
    rows = [
        [
            t["id"],
            t["agent_id"],
            t["status"],
            f"{t['cost_usd']:.4f}",
            (t["title"] or "")[:44],
        ]
        for t in tasks
    ]
    print()
    print(_fmt_table(["任务ID", "成员", "状态", "成本$", "标题"], rows))
    stats = store.stats()
    print(f"\n  今日花费 ${stats['spend_today_usd']:.4f}　任务统计：{stats['tasks']}\n")
    store.close()
    return 0


def cmd_node(args: argparse.Namespace) -> int:
    if args.action != "check":
        print("用法：carme node check")
        return 2

    config = config_module.load()
    try:
        settings = config.sandbox.resolve_node()
    except ValueError as exc:
        print(f"\n  {exc}\n  请在网页的执行电脑设置中登记 SSH 地址、用户和密钥路径，再设为默认。\n")
        return 1

    print(f"\n  正在检查 {settings.get('user')}@{settings.get('host')} ...\n")
    result = asyncio.run(_probe_sandbox(config, "remote"))
    if result.get("ok"):
        print("  ✓ 节点可用\n")
        print("  " + str(result.get("detail", "")).replace("\n", "\n  "))
        print()
        return 0
    print(f"  ✗ {result.get('error')}\n")
    return 1


def cmd_models(args: argparse.Namespace) -> int:
    if args.action != "probe":
        print("用法：carme models probe")
        return 2

    async def run():
        from .llm import LLMGateway

        config = config_module.load()
        gateway = LLMGateway(config)
        report = await gateway.probe()
        await gateway.aclose()
        return report

    report = asyncio.run(run())
    print()
    for provider, info in report.items():
        if info.get("ok"):
            print(f"  ✓ {provider}（{info['count']} 个型号）")
            for model_id in info["models"][:40]:
                print(f"      {provider}/{model_id}")
            if info["count"] > 40:
                print(f"      ... 还有 {info['count'] - 40} 个")
        else:
            print(f"  ✗ {provider}：{info.get('error')}")
        print()

    print("  把要用的型号填进 config/models.yaml 的 tiers 里，格式 provider/型号。\n")
    return 0


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="carme",
        description="自托管的常驻 AI 员工团队",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--profile", type=Path, help="使用已有的独立运行目录（包含 config、data 和 .env）")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="起服务（界面 + API）")
    p_serve.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0（手机要能连）")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    p_serve.add_argument("--log-level", default="info")
    p_serve.set_defaults(func=cmd_serve)

    p_run = sub.add_parser("run", help="直接跑一个任务")
    p_run.add_argument("goal", help="要做什么")
    p_run.add_argument("--agent", default="", help="指定成员，默认主控")
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("agents", help="看成员花名册").set_defaults(func=cmd_agents)
    sub.add_parser("doctor", help="环境自检").set_defaults(func=cmd_doctor)

    p_tasks = sub.add_parser("tasks", help="看最近的任务")
    p_tasks.add_argument("--limit", type=int, default=20)
    p_tasks.set_defaults(func=cmd_tasks)

    p_node = sub.add_parser("node", help="远端节点操作")
    p_node.add_argument("action", choices=["check"])
    p_node.set_defaults(func=cmd_node)

    p_models = sub.add_parser("models", help="模型操作")
    p_models.add_argument("action", choices=["probe"])
    p_models.set_defaults(func=cmd_models)

    args = parser.parse_args(argv)
    if args.profile:
        profile = args.profile.expanduser().resolve()
        if not (profile / "config" / "models.yaml").is_file():
            parser.error("运行目录不存在或缺少 config/models.yaml；不会创建空白实例")
        for name, child in (("CONFIG_DIR", "config"), ("DATA_DIR", "data"), ("ENV_FILE", ".env")):
            value = profile / child
            os.environ[f"CARME_{name}"] = str(value)
            setattr(config_module, name, value)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n  已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
