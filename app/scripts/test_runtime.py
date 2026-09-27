"""真实 Runtime/Agent + 替代模型的隔离回归；不使用网络或真实执行电脑。"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
from unittest.mock import patch
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


async def test_group_mention_delivery(root):
    import httpx
    from fastapi import FastAPI
    from carme.api.routes import build_router
    runtime, store, gateway = setup(root, [LLMResponse(text="指定成员答复"), LLMResponse(text="主成员答复")])
    runtime.config.agents.agents['chief'].name = '示例甲'
    runtime.config.agents.agents['writer'].name = '示例乙'
    cid = store.create_conversation(['chief', 'writer'])['id']
    app = FastAPI(); app.include_router(build_router(runtime.config, store, runtime))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        url = f'/api/conversations/{cid}/messages'
        body = {'content': '@示例乙，请回复', 'request_id': 'mention-one'}
        response = await client.post(url, json=body)
        assert response.status_code == 200, response.text
        first = response.json(); await settle(runtime)
        assert store.get_task(first['task_id'])['agent_id'] == 'writer'
        assert store.get_task(first['task_id'])['status'] == 'done'
        messages = store.list_conversation_messages(cid)
        assert len(messages) == 2 and messages[-1]['agent_id'] == 'writer'
        prompt = json.dumps(gateway.calls[0]['messages'], ensure_ascii=False)
        assert '示例乙' in prompt and '示例甲' in prompt
        runtime.config.agents.agents['writer'].name = '示例 丁'
        retry = await client.post(url, json=body)
        assert retry.status_code == 200 and retry.json()['task_id'] == first['task_id']
        assert not retry.json()['created'] and len(gateway.calls) == 1
        conflict = await client.post(url, json={**body, 'agent_id': 'chief'})
        assert conflict.status_code == 409
        invalid = await client.post(url, json={'content': '@不存在 请回复', 'request_id': 'invalid'})
        assert invalid.status_code == 422 and len(store.list_conversation_tasks(cid)) == 1
        second = await client.post(url, json={'content': '请总结', 'agent_id': 'chief', 'request_id': 'mention-two'})
        assert second.status_code == 200
        await settle(runtime)
        assert any('[示例 丁 的回复]' in str(m['content']) for m in gateway.calls[1]['messages'])
    await runtime.shutdown(); store.close()


async def test_group_mention_validation(root):
    runtime, store, _ = setup(root, [])
    runtime.config.agents.agents['chief'].name = '示例甲'
    runtime.config.agents.agents['writer'].name = '示例 丁'
    group = store.create_conversation(['chief', 'writer'])
    for content in ['@示例 丁 请回复', '请回答，@示例 丁：这个问题', '@writer 请回复']:
        assert runtime._conversation_recipients(group, content) == ['writer']
    assert runtime._conversation_recipients(group, '@示例甲 @示例 丁 请回复') == ['chief', 'writer']
    assert runtime._conversation_recipients(group, 'all') == ['chief', 'writer']
    bad = [('@Outside 请回复', ''), ('@没有此人 请回复', ''),
           ('@示例 丁 请回复', 'chief'), ('请回复', 'outside'), ('@', '')]
    for index, (content, target) in enumerate(bad):
        try: await runtime.submit_message(group['id'], content, f'bad-{index}', target)
        except ValueError: pass
        else: raise AssertionError((content, target))
    assert not store.list_conversation_tasks(group['id'])
    runtime.config.agents.agents['chief'].name = '示例 丁'
    try: runtime._conversation_recipients(group, '@示例 丁 请回复')
    except ValueError as exc: assert '同名' in str(exc)
    else: raise AssertionError('ambiguous name accepted')
    assert runtime._conversation_recipients(group, '@示例 丁 请回复', 'writer') == ['writer']
    assert runtime._conversation_recipients(group, 'explicit choice', 'writer') == ['writer']
    direct = store.create_conversation(['chief'])
    assert runtime._conversation_recipients(direct, '@示例 丁 请回复') == ['chief']
    await runtime.shutdown(); store.close()


async def test_group_mention_literals(root):
    runtime, store, _ = setup(root, [])
    group = store.create_conversation(['chief', 'writer'])
    literals = ['mail@writer.example', 'https://example.invalid/@writer', '`@writer`',
                '```text\n@writer\n```\n请解释', '~~~text\n@writer\n~~~',
                '```text\n@writer', '> @writer\n请解释引用', '“@writer”', '「@writer」',
                '"@writer"', "'@writer'", 'address+tag@writer.example', r'\@writer', '``@writer``']
    for content in literals:
        assert runtime._conversation_recipients(group, content) == ['chief', 'writer'], content
    assert runtime._conversation_recipients(group, '> @chief\n@writer 请回复') == ['writer']
    await runtime.shutdown(); store.close()


async def test_group_mention_switch_running(root):
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked():
        entered.set(); await release.wait(); return LLMResponse(text='chief reply')
    runtime, store, gateway = setup(root, [blocked, LLMResponse(text='writer reply')])
    cid = store.create_conversation(['chief', 'writer'])['id']
    first = await runtime.submit_message(cid, 'start', 'first', 'chief')
    await asyncio.wait_for(entered.wait(), 3)
    second = await runtime.submit_message(cid, '@Writer reply', 'second')
    assert second['task_id'] != first['task_id'] and not second['steering']
    assert store.get_task(second['task_id'])['agent_id'] == 'writer'
    assert not runtime._inboxes.get(first['task_id'])
    release.set(); await settle(runtime)
    assert len(gateway.calls) == 2
    await runtime.shutdown(); store.close()


async def test_group_mention_pi_names(root):
    runtime, store, _ = setup(root, [])
    runtime.config.agents.agents['chief'].name = '示例甲'
    writer = runtime.config.agents.agents['writer']; writer.name = '示例乙'; writer.engine = 'pi'
    cid = store.create_conversation(['chief', 'writer'])['id']
    old = store.create_conversation_turn(cid, 'chief', 'first', 'first')['task_id']
    store.add_conversation_message(cid, 'chief', 'assistant', 'answer', task_id=old)
    current = store.create_conversation_turn(cid, 'writer', 'second', 'second')['task_id']
    await runtime._history(store.get_task(current), writer)
    meta = runtime._meta(store.get_task(current))['pi_session']
    folder = store.pi_session_directory(cid, 'writer', meta['epoch'])
    bootstrap = json.loads((folder / 'bootstrap.json').read_text())
    assert any('[示例甲 的回复]' in row['content'] for row in bootstrap)
    (folder / 'session.jsonl').write_text('')
    await runtime._history(store.get_task(current), writer)
    external = json.loads((folder / 'external.json').read_text())
    assert any('[示例甲 的会话消息]' in row['content'] for row in external)
    await runtime.shutdown(); store.close()


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
    first = await runtime.submit_message(cid, "协作写作", "group", "chief")
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
    from carme.security import TASK_INTERRUPTED
    assert "预算" in store.get_task(first["task_id"])["error"]
    assert store.get_task(first["task_id"])["result"] == TASK_INTERRUPTED
    duplicate = await runtime.submit_message(cid, "整理", "budget")
    assert not duplicate["created"] and duplicate["task_id"] == first["task_id"]
    assert len(gateway.calls) == 1
    await runtime.shutdown()
    store.close()


async def test_queued_cancel(root):
    runtime, store, gateway = setup(root, [])
    cid = store.create_conversation(["chief"])["id"]
    first = await runtime.submit_message(cid, "马上取消", "cancel")
    message_id = store.add_conversation_message(cid, "chief", "assistant", "未完成的文字",
        task_id=first["task_id"], status="streaming")
    approval_id = store.create_approval(task_id=first["task_id"], agent_id="chief", kind="test", summary="pending")
    # 在新协程拿到执行机会前取消。
    assert await runtime.cancel(first["task_id"])
    await settle(runtime)
    assert not gateway.calls
    assert store.get_task(first["task_id"])["status"] == "cancelled", "Unstarted job remained queued"
    message = store.list_conversation_messages(cid, [message_id])[0]
    assert message["status"] == "interrupted" and message["content"] == "未完成的文字"
    assert any(event["type"] == "conversation.message" and event["payload"].get("message_id") == message_id
               for event in store.list_events(task_id=first["task_id"]))
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


async def test_streaming_failure(root):
    runtime, store, gateway = setup(root, [])
    cid = store.create_conversation(["chief"])["id"]
    message_id = "failed-stream"
    async def fail_after_stream(messages, **kwargs):
        await kwargs["on_stream"]({"message_id": message_id, "content": "仅部分输出",
            "model": "fixture", "provider": "fixture", "status": "streaming"})
        raise RuntimeError("synthetic model failure")
    gateway.chat = fail_after_stream
    task = await runtime.submit_message(cid, "触发失败", "stream-fail")
    await settle(runtime)
    assert store.get_task(task["task_id"])["status"] == "failed"
    message = store.list_conversation_messages(cid, [message_id])[0]
    assert message["status"] == "interrupted" and message["content"] == "仅部分输出"
    assert message["model"] == "fixture" and message["provider"] == "fixture"
    events = [event for event in store.list_events(task_id=task["task_id"])
        if event["type"] == "conversation.message" and event["payload"].get("message_id") == message_id]
    assert len(events) == 2, events
    await runtime.shutdown()
    store.close()


async def test_streaming_running_cancel(root):
    runtime, store, gateway = setup(root, [])
    cid = store.create_conversation(["chief"])["id"]
    streamed = asyncio.Event()
    message_id = "cancelled-stream"
    async def block_after_stream(messages, **kwargs):
        await kwargs["on_stream"]({"message_id": message_id, "content": "尚未完成",
            "model": "fixture", "provider": "fixture", "status": "streaming"})
        streamed.set()
        await asyncio.Event().wait()
    gateway.chat = block_after_stream
    task = await runtime.submit_message(cid, "触发取消", "stream-cancel")
    await asyncio.wait_for(streamed.wait(), 3)
    assert await runtime.cancel(task["task_id"])
    await settle(runtime)
    assert store.get_task(task["task_id"])["status"] == "cancelled"
    message = store.list_conversation_messages(cid, [message_id])[0]
    assert message["status"] == "interrupted" and message["content"] == "尚未完成"
    events = [event for event in store.list_events(task_id=task["task_id"])
        if event["type"] == "conversation.message" and event["payload"].get("message_id") == message_id]
    assert len(events) == 2, events
    await runtime.shutdown()
    store.close()


async def test_streaming_normal_completion(root):
    runtime, store, gateway = setup(root, [])
    cid = store.create_conversation(["chief"])["id"]
    message_id = "completed-stream"
    async def finish_stream(messages, **kwargs):
        await kwargs["on_stream"]({"message_id": message_id, "content": "部分输出",
            "model": "fixture", "provider": "fixture", "status": "streaming"})
        await kwargs["on_stream"]({"message_id": message_id, "content": "完整输出",
            "model": "fixture", "provider": "fixture", "status": "done"})
        return LLMResponse(text="完整输出", streamed=True)
    gateway.chat = finish_stream
    task = await runtime.submit_message(cid, "正常完成", "stream-done")
    await settle(runtime)
    assert store.get_task(task["task_id"])["status"] == "done"
    message = store.list_conversation_messages(cid, [message_id])[0]
    assert message["status"] == "done" and message["content"] == "完整输出"
    events = [event for event in store.list_events(task_id=task["task_id"])
        if event["type"] == "conversation.message" and event["payload"].get("message_id") == message_id]
    assert len(events) == 2, events
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


async def test_chat_and_parent_deadlines(root):
    runtime, store, _ = setup(root, [])
    runtime.config.sandbox.limits.update(max_task_seconds=1800, max_chat_seconds=600)
    now=time.time()
    chat_node, chat=runtime._task_agent_snapshot('writer')
    assert not chat['policy']['tools']
    assert 0 < chat['deadline']-now <= 601
    node, tool=runtime._task_agent_snapshot('chief')
    assert 1700 < tool['deadline']-now <= 1801
    parent=store.create_task('chief','parent',meta={'node':node,**tool,'deadline':now+100})
    _, child=runtime._task_agent_snapshot('writer',store.get_task(parent))
    assert child['deadline'] <= now+100
    chat_id=store.create_task('writer','resume chat',meta={'node':chat_node,**chat,'deadline':now-1})
    store.checkpoint(chat_id,{'messages':[],'next_step':1,'memory_refs':[],'tool_calls':2})
    store.finish_task(chat_id,'synthetic interruption',status='failed')
    with patch.object(runtime,'_schedule'):
        await runtime.resume(chat_id)
    resumed=runtime._meta(store.get_task(chat_id))
    assert 0 < resumed['deadline']-time.time() <= 601
    assert store.checkpoint(chat_id)['tool_calls'] == 2
    store.finish_task(chat_id,'synthetic interruption',status='failed')
    store.update_task_meta(chat_id,{'deadline':now-1,'deadline_explicit':True})
    try:await runtime.resume(chat_id)
    except ValueError:pass
    else:raise AssertionError('explicit deadline extended')
    store.close()


async def test_group_rounds(root):
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked():
        entered.set(); await release.wait(); return LLMResponse(text='ROUND_ONE_CHIEF')
    runtime, store, gateway = setup(root, [blocked, LLMResponse(text='ROUND_ONE_WRITER'),
                                          LLMResponse(text='ROUND_TWO_CHIEF'), LLMResponse(text='ROUND_TWO_WRITER')])
    store.remember('chief', 'private', 'PRIVATE_CHIEF_MEMORY')
    store.remember('writer', 'private', 'PRIVATE_WRITER_MEMORY')
    runtime.config.agents.get('chief').prompt = 'ROLE_CHIEF_PLAN'
    runtime.config.agents.get('writer').prompt = 'ROLE_WRITER_REVIEW'
    cid = store.create_conversation(['chief', 'writer'])['id']
    first = await runtime.submit_message(cid, 'ROUND_ONE_USER', 'one')
    assert len(first['task_ids']) == 2
    await entered.wait()
    store.set_task_status(first['task_ids'][0], 'waiting_approval')
    await asyncio.sleep(0)
    assert len(gateway.calls) == 1
    assert store.get_task(first['task_ids'][1])['status'] == 'queued'
    second = await runtime.submit_message(cid, 'ROUND_TWO_USER', 'two', agent_ids=['writer', 'chief'])
    assert not second['steering'] and not runtime._inboxes
    retry = await runtime.submit_message(cid, 'ROUND_TWO_USER', 'two', agent_ids=['chief', 'writer'])
    assert not retry['created'] and retry['task_ids'] == second['task_ids']
    # Waiting for a peer does not consume this recipient's default execution allowance.
    store.update_task_meta(first['task_ids'][1], {'deadline': time.time()-1})
    assert len(gateway.calls) == 1
    store.set_task_status(first['task_ids'][0], 'running')
    release.set(); await settle(runtime)
    assert [store.get_task(t)['status'] for t in first['task_ids']+second['task_ids']] == ['done']*4
    assert len(gateway.calls) == 4
    for i, call in enumerate(gateway.calls):
        contents = [m['content'] for m in call['messages']]
        encoded = json.dumps(contents)
        own, other = ('CHIEF', 'WRITER') if i % 2 == 0 else ('WRITER', 'CHIEF')
        assert 'PRIVATE_'+own+'_MEMORY' in encoded
        assert 'PRIVATE_'+other+'_MEMORY' not in encoded
        assert 'ROLE_'+own in call['system_extra'] and 'ROLE_'+other not in call['system_extra']
        current = 'ROUND_ONE_USER' if i < 2 else 'ROUND_TWO_USER'
        assert sum(current in str(c) for c in contents) == 1, (i, contents)
        if i < 2: assert 'ROUND_TWO_USER' not in encoded
        if i == 1: assert 'ROUND_ONE_CHIEF' in encoded
        if i >= 2: assert 'ROUND_ONE_WRITER' in encoded and 'ROUND_ONE_USER' in encoded
    messages = store.list_conversation_messages(cid)
    assert len([m for m in messages if m['role']=='user']) == 2
    assert [m['agent_ids'] for m in messages if m['role']=='user'] == [['chief', 'writer']]*2
    assert len(store._query("SELECT * FROM events WHERE type='task.created'")) == 4
    assert len(store._query("SELECT * FROM conversation_deliveries")) == 4
    await runtime.shutdown(); store.close()


async def test_group_published_memory_boundary(root):
    runtime, store, _ = setup(root, [])
    cid = store.create_conversation(['chief', 'writer'])['id']
    store.remember('chief', 'private', 'NEVER_INJECT_RAW_MEMORY')
    with patch.object(runtime, '_schedule'):
        batch = await runtime.submit_message(cid, 'GROUP_QUESTION', 'one')
    chief, writer = batch['task_ids']
    _, refs = store.memory_context('chief', task_id=chief)
    store.update_task_meta(chief, {'memory_refs': refs})
    store.add_conversation_message(cid, 'chief', 'assistant', 'PUBLISHED_CONCLUSION', task_id=chief)
    store.finish_task(chief, 'PUBLISHED_CONCLUSION')
    prior = store.get_task(chief)
    assert not store.memory_refs_available(refs, 'writer', writer)
    assert store.conversation_reply_available(prior, 'writer', writer)
    spec = runtime.config.agents.get('writer')
    history = await runtime._history(store.get_task(writer), spec)
    assert 'PUBLISHED_CONCLUSION' in str(history) and 'NEVER_INJECT_RAW_MEMORY' not in str(history)
    private = store.create_conversation(['writer'])['id']
    other = store.create_task('writer', 'PRIVATE', conversation_id=private)
    assert not store.conversation_reply_available(prior, 'writer', other)
    store.remember('chief', 'private', 'UPDATED_PRIVATE_VALUE')
    assert store.conversation_reply_available(prior, 'writer', writer)
    spec.engine = 'pi'
    await runtime._history(store.get_task(writer), spec)
    before = runtime._meta(store.get_task(writer))['pi_session']['epoch']
    folder = store.pi_session_directory(cid, 'writer', before)
    assert 'PUBLISHED_CONCLUSION' in (folder/'bootstrap.json').read_text()
    store.update_task_meta(writer, {'agent_engine':'pi', 'pi_summary':{'content':'PUBLIC_SUMMARY'}})
    assert store.visible_summary(cid)['content'] == 'PUBLIC_SUMMARY'
    store.forget('chief', 'private')
    assert not store.conversation_reply_available(prior, 'writer', writer)
    assert store.visible_summary(cid) is None
    await runtime._history(store.get_task(writer), spec)
    after = runtime._meta(store.get_task(writer))['pi_session']['epoch']
    assert before != after
    assert 'PUBLISHED_CONCLUSION' not in (store.pi_session_directory(cid,'writer',after)/'bootstrap.json').read_text()
    spec.engine = 'api'
    assert 'PUBLISHED_CONCLUSION' not in str(await runtime._history(store.get_task(writer), spec))
    for tid in (writer, other): store.finish_task(tid, 'fixture')
    await runtime.shutdown(); store.close()


async def test_group_atomic_files(root):
    from carme.attachments import save_file
    runtime, store, gateway = setup(root, [LLMResponse(text='first'), LLMResponse(text='second')])
    cid = store.create_conversation(['chief', 'writer', 'outside'])['id']
    upload = save_file(store, cid, 'shared.txt', b'EXPLICIT_SHARED_INPUT')
    original = runtime.bind_envelope
    def fail_second(tid, *args, **kwargs):
        if store.get_task(tid)['agent_id']=='writer': raise ValueError('synthetic_contract_failure')
        return original(tid, *args, **kwargs)
    with patch.object(runtime, 'bind_envelope', side_effect=fail_second):
        try: await runtime.submit_message(cid, 'read', 'one', agent_ids=['chief','writer'], attachment_ids=[upload['id']])
        except ValueError: pass
        else: raise AssertionError('injected failure ignored')
    for table in ('tasks', 'conversation_messages', 'conversation_requests', 'conversation_deliveries', 'task_artifacts', 'attachment_shares'):
        assert not store._query('SELECT * FROM '+table), table
    assert not store.get_attachment(upload['id'])['message_id']
    runtime.config.sandbox.limits['max_daily_tasks']=1
    try: await runtime.submit_message(cid, 'read', 'one', agent_ids=['chief','writer'])
    except ValueError as exc: assert 'max_daily' in str(exc)
    else: raise AssertionError('per-member daily budget bypassed')
    assert not store.list_conversation_tasks(cid)
    runtime.config.sandbox.limits['max_daily_tasks']=20
    result = await runtime.submit_message(cid, 'read', 'one', agent_ids=['writer','chief'], attachment_ids=[upload['id']])
    await settle(runtime)
    for tid in result['task_ids']:
        assert store.get_task(tid)['status']=='done'
        assert store.artifact_access(tid, upload['id'])['id']==upload['id']
    assert all('EXPLICIT_SHARED_INPUT' in json.dumps(call['messages']) for call in gateway.calls)
    foreign = store.create_task('outside', 'must not read', conversation_id=cid)
    try: store.artifact_access(foreign, upload['id'])
    except ValueError: pass
    else: raise AssertionError('attachment leaked to unselected member')
    store.finish_task(foreign, 'fixture')
    await runtime.shutdown(); store.close()


async def test_group_failure_cancel_deadline(root):
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_failure():
        entered.set(); await release.wait(); raise RuntimeError('synthetic_private_failure')
    runtime, store, gateway = setup(root, [blocked_failure, LLMResponse(text='third still replies')])
    cid = store.create_conversation(['chief', 'writer', 'outside'])['id']
    first = await runtime.submit_message(cid, 'all', 'one')
    await entered.wait()
    assert await runtime.cancel(first['task_ids'][1])
    release.set(); await settle(runtime)
    assert [store.get_task(t)['status'] for t in first['task_ids']] == ['failed','cancelled','done']
    assert len(gateway.calls)==2
    assert all('synthetic_private_failure' not in m['content'] for m in store.list_conversation_messages(cid))
    # Explicit contract deadlines are never extended when another member holds the queue.
    entered.clear(); release.clear()
    async def wait_ok(): entered.set(); await release.wait(); return LLMResponse(text='late')
    gateway.responses=[wait_ok]
    second = await runtime.submit_message(cid, 'wait', 'two', 'chief')
    await entered.wait()
    third = await runtime.submit_message(cid, 'hard deadline', 'three', 'writer', envelope={'deadline':time.time()+0.03})
    await asyncio.sleep(0.06); release.set(); await settle(runtime)
    assert store.get_task(second['task_id'])['status']=='done'
    assert store.get_task(third['task_id'])['status']=='failed'
    assert len(gateway.calls)==3
    await runtime.shutdown(); store.close()


async def test_group_restart_queue(root):
    from unittest.mock import AsyncMock
    entered = asyncio.Event()
    async def blocked(): entered.set(); await asyncio.Event().wait()
    runtime, store, gateway = setup(root, [blocked])
    cid = store.create_conversation(['chief','writer'])['id']
    result = await runtime.submit_message(cid, 'restart', 'one')
    await entered.wait(); await runtime.shutdown()
    assert [store.get_task(t)['status'] for t in result['task_ids']]==['failed','queued']
    config=runtime.config; path=store.path; store.close()
    store=Store(path); store.mark_stale_running_as_failed()
    resumed=Runtime(config,store,EventBus()); resumed.gateway=ScriptedGateway([LLMResponse(text='pending member recovered')])
    with patch.object(resumed.mcp,'start_all',new=AsyncMock()):
        await resumed.start(); await resumed.start(); await settle(resumed)
    assert [store.get_task(t)['status'] for t in result['task_ids']]==['failed','done']
    assert len(resumed.gateway.calls)==1
    retry=await resumed.submit_message(cid,'restart','one')
    assert not retry['created'] and retry['task_ids']==result['task_ids']
    assert len(store._query('SELECT * FROM conversation_deliveries'))==2
    await resumed.shutdown(); store.close()


async def test_group_pi_context(root):
    runtime, store, _ = setup(root, [])
    for spec in runtime.config.agents.agents.values(): spec.engine='pi'
    cid=store.create_conversation(['chief','writer'])['id']
    with patch.object(runtime,'_schedule'):
        first=await runtime.submit_message(cid,'FIRST_GROUP_INPUT','one')
        future=await runtime.submit_message(cid,'FUTURE_INPUT','two')
    chief, writer = first['task_ids']
    await runtime._history(store.get_task(chief), runtime.config.agents.get('chief'))
    store.add_conversation_message(cid,'chief','assistant','CHIEF_GROUP_REPLY',task_id=chief)
    store.finish_task(chief,'done')
    await runtime._history(store.get_task(writer), runtime.config.agents.get('writer'))
    meta=runtime._meta(store.get_task(writer))['pi_session']
    folder=store.pi_session_directory(cid,'writer',meta['epoch'])
    bootstrap=json.loads((folder/'bootstrap.json').read_text())
    assert len(bootstrap)==1 and 'CHIEF_GROUP_REPLY' in bootstrap[0]['content']
    assert 'FIRST_GROUP_INPUT' not in str(bootstrap) and 'FUTURE_INPUT' not in str(bootstrap)
    (folder/'session.jsonl').write_text('')
    store.add_conversation_message(cid,'writer','assistant','WRITER_GROUP_REPLY',task_id=writer)
    store.finish_task(writer,'done')
    next_writer=future['task_ids'][1]
    await runtime._history(store.get_task(next_writer), runtime.config.agents.get('writer'))
    external=json.loads((folder/'external.json').read_text())
    own_input = next(row for row in external if 'FIRST_GROUP_INPUT' in row['content'])
    assert own_input['native_task_ids'] == [writer] # Bridge checks actual native acceptance before deduplication.
    assert 'FUTURE_INPUT' not in str(external)
    assert 'CHIEF_GROUP_REPLY' in str(external)
    assert 'WRITER_GROUP_REPLY' not in str(external)
    private=store.create_conversation(['writer'])['id']
    assert store.pi_session_directory(private,'writer',meta['epoch']) != folder
    for tid in future['task_ids']: store.finish_task(tid,'fixture')
    await runtime.shutdown(); store.close()


async def test_group_long_context(root):
    runtime, store, gateway=setup(root,[LLMResponse(text='SUMMARY_RETAINS_SHARED_GOAL'),
        LLMResponse(text='CHIEF_CURRENT_REPLY'),LLMResponse(text='WRITER_CURRENT_REPLY')])
    cid=store.create_conversation(['chief','writer'])['id']
    old=store.create_task('chief','old',conversation_id=cid)
    for i in range(48):
        store.add_conversation_message(cid,'chief','user' if i%2==0 else 'assistant',f'PRIOR_{i}',task_id=old)
    store.finish_task(old,'fixture')
    with patch.object(runtime,'_schedule'):
        first=await runtime.submit_message(cid,'CURRENT_SHARED_REQUEST','current')
        future=await runtime.submit_message(cid,'FUTURE_NOT_YET_REQUEST','future')
    for tid in first['task_ids']:runtime._schedule(tid)
    await settle(runtime)
    assert len(gateway.calls)==3
    assert all('FUTURE_NOT_YET_REQUEST' not in json.dumps(call['messages']) for call in gateway.calls)
    assert 'CURRENT_SHARED_REQUEST' not in json.dumps(gateway.calls[0]['messages'])
    for call in gateway.calls[1:]:
        assert json.dumps(call['messages']).count('CURRENT_SHARED_REQUEST')==1
        assert 'SUMMARY_RETAINS_SHARED_GOAL' in json.dumps(call['messages'])
    assert 'CHIEF_CURRENT_REPLY' in json.dumps(gateway.calls[2]['messages'])
    boundary=next(m['seq'] for m in store.list_conversation_messages(cid) if m['id']==first['message_id'])
    assert store.get_summary(cid)['through_seq']<boundary
    assert len(store.list_conversation_messages(cid))==52
    for tid in future['task_ids']:store.finish_task(tid,'fixture')
    store.update_conversation(cid,{'deleted':True});store.purge_conversations([cid])
    assert not store._query('SELECT * FROM conversation_deliveries')
    await runtime.shutdown();store.close()


async def test_group_concurrent_http_retry(root):
    import httpx
    from fastapi import FastAPI
    from carme.api.routes import build_router
    runtime, store, gateway=setup(root,[LLMResponse(text='a'),LLMResponse(text='b')])
    cid=store.create_conversation(['chief','writer','outside'])['id']
    app=FastAPI();app.include_router(build_router(runtime.config,store,runtime))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
        url=f'/api/conversations/{cid}/messages'
        body={'content':'one request','request_id':'same','agent_ids':['chief','writer']}
        responses=await asyncio.gather(*(client.post(url,json=body) for _ in range(5)))
        assert all(r.status_code==200 for r in responses)
        assert sum(r.json()['created'] for r in responses)==1
        assert len({tuple(r.json()['task_ids']) for r in responses})==1
        invalid=await client.post(url,json={**body,'agent_ids':['chief','chief']})
        assert invalid.status_code==422
        conflict=await client.post(url,json={**body,'agent_ids':['outside']})
        assert conflict.status_code==409
        await settle(runtime)
        detail=(await client.get(f'/api/conversations/{cid}')).json()
        human=[m for m in detail['messages'] if m['role']=='user']
        assert len(human)==1 and human[0]['agent_ids']==['chief','writer']
        assert len(detail['tasks'])==2 and len(gateway.calls)==2
        events=store._query("SELECT * FROM events WHERE type='conversation.message'")
        assert sum(json.loads(e['payload'])['message_id']==human[0]['id'] for e in events)==1
    await runtime.shutdown();store.close()


async def main(root):
    failures = []
    for check in (test_group_published_memory_boundary, test_group_concurrent_http_retry, test_group_long_context, test_group_rounds, test_group_atomic_files, test_group_failure_cancel_deadline, test_group_restart_queue, test_group_pi_context, test_followup, test_group_mention_delivery, test_group_mention_validation,
                  test_group_mention_literals, test_group_mention_switch_running, test_group_mention_pi_names,
                  test_steering, test_delegate, test_budget, test_queued_cancel,
                  test_waiting_cancel, test_streaming_failure, test_streaming_running_cancel,
                  test_streaming_normal_completion, test_restart, test_chat_and_parent_deadlines):
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
