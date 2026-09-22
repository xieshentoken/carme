"""真实 Runtime/Agent + 替代模型的隔离回归；不使用网络或真实执行电脑。"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CARME_LOAD_ENV"] = "0"

from carme.bus import EventBus
from carme.config import AgentSpec, AgentsConfig, BrowserConfig, Config, ModelsConfig, SandboxConfig
from carme.llm import LLMResponse, ToolCall, Usage
from carme.runtime import Runtime
from carme.store import Store


class ScriptedGateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), **kwargs})
        if not self.responses:
            raise AssertionError("Unexpected extra model call")
        response = self.responses.pop(0)
        return await response() if callable(response) else response

    async def aclose(self):
        pass


def setup(root: Path, responses, *, budget=0, concurrency=3):
    root.mkdir()
    agents = AgentsConfig(defaults={"max_steps": 5}, agents={
        "chief": AgentSpec("chief", "Chief", entry=True, execution_target="ssh", sandbox="none", tools=["team", "memory"], can_delegate=True),
        "writer": AgentSpec("writer", "Writer", execution_target="ssh", sandbox="none"),
        "outside": AgentSpec("outside", "Outside", execution_target="ssh", sandbox="none"),
    })
    nodes = [{"node_id": value, "name": value, "host": f"{value}.invalid", "user": "fixture",
              "identity_file": "/synthetic/private/key"} for value in ("node-a", "node-b")]
    config = Config(agents, ModelsConfig(budget_daily_usd=budget),
        SandboxConfig(nodes=nodes, default_node_id="node-a", limits={"max_concurrent_tasks": concurrency}),
        BrowserConfig(enabled=False), root=root)
    store = Store(root / "test.db")
    runtime = Runtime(config, store, EventBus())
    gateway = ScriptedGateway(responses)
    runtime.gateway = gateway
    return runtime, store, gateway


async def settle(runtime):
    while runtime._jobs:
        await asyncio.gather(*list(runtime._jobs.values()), return_exceptions=True)
        await asyncio.sleep(0)


async def test_followup(root):
    runtime, store, gateway = setup(root, [LLMResponse(text="记住了：青色"), LLMResponse(text="你刚才说青色")])
    cid = store.create_conversation(["chief"])["id"]
    first = await runtime.submit_message(cid, "记住我喜欢青色", "one")
    await settle(runtime)
    retry = await runtime.submit_message(cid, "记住我喜欢青色", "one")
    assert retry["task_id"] == first["task_id"] and not retry["created"]
    assert len(gateway.calls) == 1
    second = await runtime.submit_message(cid, "我刚才说什么颜色？", "two")
    await settle(runtime)
    messages = gateway.calls[1]["messages"]
    assert any(message["content"] == "记住我喜欢青色" for message in messages)
    assert any(message["content"] == "记住了：青色" for message in messages)
    assert store.get_task(second["task_id"])["status"] == "done"
    assert len(store.list_conversation_messages(cid)) == 4
    await runtime.shutdown()
    store.close()


async def test_steering(root):
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked():
        entered.set()
        await release.wait()
        return LLMResponse(text="初始答复")
    runtime, store, gateway = setup(root, [blocked, LLMResponse(text="按照最新要求改为三项")])
    cid = store.create_conversation(["chief"])["id"]
    first = await runtime.submit_message(cid, "整理事项", "initial")
    await asyncio.wait_for(entered.wait(), 3)
    follow = await runtime.submit_message(cid, "只保留三项", "steer")
    repeated = await runtime.submit_message(cid, "只保留三项", "steer")
    assert follow["task_id"] == first["task_id"] and follow["steering"]
    assert not repeated["created"]
    release.set()
    await settle(runtime)
    assert len(gateway.calls) == 2
    assert gateway.calls[1]["messages"][-1] == {"role": "user", "content": "只保留三项"}
    assert len(store.list_conversation_tasks(cid)) == 1
    assert store.list_conversation_messages(cid)[-1]["content"] == "按照最新要求改为三项"
    await runtime.shutdown()
    store.close()


async def test_delegate(root):
    async def switch_then_delegate():
        runtime.config.sandbox.default_node_id = "node-b"
        return LLMResponse(text="交给写作者", tool_calls=[ToolCall("delegate-1", "delegate", {"agent": "writer", "goal": "写一段", "title": "写作"})])
    runtime, store, gateway = setup(root, [switch_then_delegate, LLMResponse(text="成员完成"), LLMResponse(text="已汇总")])
    cid = store.create_conversation(["chief", "writer"])["id"]
    first = await runtime.submit_message(cid, "协作写作", "group")
    await settle(runtime)
    children = store.children_of(first["task_id"])
    assert len(children) == 1, [call["messages"] for call in gateway.calls]
    child = children[0]
    assert child["conversation_id"] == cid
    assert json.loads(child["meta"])["node"]["node_id"] == "node-a"
    assert runtime.config.sandbox.default_node_id == "node-b"
    assert child["status"] == "done"
    visible = store.list_conversation_messages(cid)
    assert any(message["agent_id"] == "writer" and message["content"] == "成员完成" for message in visible)
    assert all(message["role"] != "tool" for message in visible)
    assert len(gateway.calls) == 3
    await runtime.shutdown()
    store.close()


async def test_budget(root):
    response = LLMResponse(tool_calls=[ToolCall("memory-1", "remember", {"key": "color", "value": "blue"})],
                           usage=Usage(cost_usd=2))
    runtime, store, gateway = setup(root, [response], budget=1)
    cid = store.create_conversation(["chief"])["id"]
    first = await runtime.submit_message(cid, "整理", "budget")
    await settle(runtime)
    assert len(gateway.calls) == 1
    assert store.get_task(first["task_id"])["status"] == "failed"
    assert "预算" in store.get_task(first["task_id"])["result"]
    duplicate = await runtime.submit_message(cid, "整理", "budget")
    assert not duplicate["created"] and duplicate["task_id"] == first["task_id"]
    assert len(gateway.calls) == 1
    await runtime.shutdown()
    store.close()


async def test_queued_cancel(root):
    runtime, store, gateway = setup(root, [])
    cid = store.create_conversation(["chief"])["id"]
    first = await runtime.submit_message(cid, "马上取消", "cancel")
    approval_id = store.create_approval(task_id=first["task_id"], agent_id="chief", kind="test", summary="pending")
    # 在新协程拿到执行机会前取消。
    assert await runtime.cancel(first["task_id"])
    await settle(runtime)
    assert not gateway.calls
    assert store.get_task(first["task_id"])["status"] == "cancelled", "Unstarted job remained queued"
    assert store.get_approval(approval_id)["status"] == "rejected"
    await runtime.shutdown()
    store.close()


async def test_waiting_cancel(root):
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked():
        entered.set()
        await release.wait()
        return LLMResponse(text="first done")
    runtime, store, gateway = setup(root, [blocked], concurrency=1)
    first_cid = store.create_conversation(["chief"])["id"]
    second_cid = store.create_conversation(["writer"])["id"]
    await runtime.submit_message(first_cid, "occupy slot", "one")
    await asyncio.wait_for(entered.wait(), 3)
    second = await runtime.submit_message(second_cid, "queued", "two")
    await asyncio.sleep(0)
    assert await runtime.cancel(second["task_id"])
    release.set()
    await settle(runtime)
    assert store.get_task(second["task_id"])["status"] == "cancelled"
    assert len(gateway.calls) == 1
    await runtime.shutdown()
    store.close()


async def test_restart(root):
    runtime, store, _ = setup(root, [])
    cid = store.create_conversation(["chief"])["id"]
    queued = store.create_conversation_turn(cid, "chief", "before restart", "queued")["task_id"]
    running = store.create_conversation_turn(cid, "chief", "running", "running")["task_id"]
    store.set_task_status(running, "running")
    approval = store.create_approval(task_id=running, agent_id="chief", kind="test", summary="waiting")
    await runtime.shutdown()
    store.close()
    store = Store(root / "test.db")
    assert store.mark_stale_running_as_failed() == 2
    assert store.get_task(queued)["status"] == "failed"
    assert store.get_task(running)["status"] == "failed"
    assert store.get_approval(approval)["status"] == "rejected"
    assert len(store.list_conversation_messages(cid)) == 4
    assert not store.create_conversation_turn(cid, "chief", "before restart", "queued")["created"]
    store.close()


async def main(root):
    failures = []
    for check in (test_followup, test_steering, test_delegate, test_budget, test_queued_cancel, test_waiting_cancel, test_restart):
        try:
            await asyncio.wait_for(check(root / check.__name__), 10)
            print(f"PASS: {check.__name__}")
        except Exception as exc:
            print(f"FAIL: {check.__name__}: {type(exc).__name__}: {exc}")
            failures.append(check.__name__)
    if failures:
        raise AssertionError(", ".join(failures))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="carme-runtime-") as directory:
        asyncio.run(main(Path(directory)))
