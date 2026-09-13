"""默认隔离验收；显式 --live-profile 才使用指定连接发送合成测试内容。"""
from __future__ import annotations
import asyncio
import copy
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['CARME_LOAD_ENV'] = '0'
import httpx
import yaml
from PIL import Image
from fastapi import Depends, FastAPI
from carme import config as config_module
from carme.api.routes import build_router, require_token
from carme.agents.base import Agent
from carme.bus import EventBus
from carme.runtime import Runtime
from carme.store import Store


async def main(root: Path):
    config_module.CONFIG_DIR = root / 'config'
    config_module.ENV_FILE = root / '.env'
    config_module.CONFIG_DIR.mkdir()
    (root / '.env').write_text("# preserved comment\nEXISTING_KEY='keep-me'\n")
    (root / 'config' / 'models.yaml').write_text(yaml.safe_dump({'providers': {}, 'tiers': {'balanced': {'candidates': []}}, 'models': [], 'budget': {'daily_usd': 12}}))
    (root / 'config' / 'agents.yaml').write_text(yaml.safe_dump({'agents': {'chief': {'name': 'Chief', 'title': 'Research', 'prompt': 'Keep my role unchanged', 'tier': 'balanced', 'sandbox': 'none', 'entry': True, 'tools': []}}}))
    config = config_module.load(reload=True)
    store = Store(root / 'data' / 'carme.db')
    runtime = Runtime(config, store, EventBus())
    received = []
    rate_limited = False
    def upstream(request):
        auth = request.headers.get('authorization', '') or request.headers.get('x-api-key', '')
        if 'fixture-key' not in auth:
            return httpx.Response(401, json={'error': {'message': 'bad credentials'}})
        if request.method == 'GET':
            assert '/v1/v1/' not in request.url.path
            models = [{'id': 'chat-one'}, {'id': 'chat-two'}, {'id': 'ignored-effort'}, {'id': 'denied'}]
            if 'anthropic' in str(request.url):
                models = [{'id': 'claude-fixture', 'capabilities': {'effort': {'supported': True, 'low': {'supported': True}, 'high': {'supported': True}, 'max': {'supported': False}}}}]
            return httpx.Response(200, json={'data': models})
        body = json.loads(request.content)
        received.append({'path': request.url.path, 'body': body})
        effort = body.get('reasoning_effort') or body.get('output_config', {}).get('effort') or body.get('reasoning', {}).get('effort')
        if rate_limited and effort == 'medium':
            return httpx.Response(429, json={'error': 'rate limit'})
        if body['model'] == 'denied':
            return httpx.Response(403, json={'error': 'model forbidden'})
        if body['model'] != 'ignored-effort' and effort and effort not in ('low', 'high'):
            return httpx.Response(400, json={'error': 'unsupported effort'})
        if any((tool.get('function') or tool).get('name') == 'fixture_tool' for tool in body.get('tools', [])):
            if request.url.path.endswith('/responses'):
                if any(item.get('type') == 'function_call_output' for item in body['input']):
                    assert any(item.get('encrypted_content') == 'fixture-encrypted' for item in body['input'])
                    assert any(item.get('output') == 'fixture tool result' for item in body['input'])
                else:
                    assert 'reasoning.encrypted_content' in body['include']
                    return httpx.Response(200, json={'status': 'completed', 'output': [
                        {'id': 'rs_fixture', 'type': 'reasoning', 'summary': [], 'encrypted_content': 'fixture-encrypted'},
                        {'type': 'function_call', 'call_id': 'call_fixture', 'name': 'fixture_tool', 'arguments': '{"value": 7}'}]})
            elif request.url.path.endswith('/messages'):
                if any(isinstance(item['content'], list) and any(block.get('type') == 'tool_result' for block in item['content']) for item in body['messages']):
                    assert body['messages'][-2]['content'][0]['signature'] == 'fixture-signature'
                    assert body['messages'][-1]['content'][0]['content'] == 'fixture tool result'
                else:
                    return httpx.Response(200, json={'type': 'message', 'stop_reason': 'tool_use', 'content': [
                        {'type': 'thinking', 'thinking': 'fixture reasoning', 'signature': 'fixture-signature'},
                        {'type': 'tool_use', 'id': 'call_fixture', 'name': 'fixture_tool', 'input': {'value': 7}}]})
            elif not any(item['role'] == 'tool' for item in body['messages']):
                return httpx.Response(200, json={'choices': [{'message': {'content': '', 'tool_calls': [
                    {'id': 'call_fixture', 'type': 'function', 'function': {'name': 'fixture_tool', 'arguments': '{"value": 7}'}}]}, 'finish_reason': 'tool_calls'}]})
        if request.url.path.endswith('/messages'):
            return httpx.Response(200, json={'type': 'message', 'content': [{'type': 'text', 'text': 'OK anthropic'}], 'stop_reason': 'end_turn'})
        if request.url.path.endswith('/responses'):
            return httpx.Response(200, json={'id': 'r_test', 'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'OK responses'}]}]})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK chat'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})
    runtime.gateway._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = FastAPI()
    app.include_router(build_router(config, store, runtime), dependencies=[Depends(require_token)])
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://carme.test')
    async def post(path, body):
        result = await client.post('/api' + path, json=body)
        assert result.status_code < 500, result.text
        return result
    draft = {'id': 'fixture', 'label': 'Isolated API', 'type': 'openai', 'base_url': 'https://api.fixture.test/v1/chat/completions', 'api_key': "fixture-key'${UNCHANGED}\\suffix"}
    bad = await post('/models/connections/test', {**draft, 'api_key': 'invalid'})
    assert bad.json()['ok'] is False
    assert (await post('/models/connections/test', {**draft, 'base_url': 'https://api.fixture.test/v1?key=hidden'})).status_code == 422
    tested = (await post('/models/connections/test', draft)).json()
    assert tested['ok'] and len(tested['models']) == 4
    assert tested['models'][0]['effort_source'] == 'unknown'
    proof = {**draft, 'probe_id': tested['probe_id']}
    detected = (await post('/models/connections/efforts', {**proof, 'model_id': 'chat-one'})).json()
    assert detected['effort_options'] == ['low', 'high'], detected
    rate_limited = True
    partial = await runtime.gateway.detect_efforts(config_module.Provider('limited', 'openai', 'https://api.fixture.test/v1', '', 'fixture-key'), 'chat-one')
    assert partial['effort_source'] == 'partial' and partial['effort_options'] == ['low', 'high']
    rate_limited = False
    ignored = (await post('/models/connections/efforts', {**proof, 'model_id': 'ignored-effort'})).json()
    assert ignored['effort_source'] == 'unknown' and not ignored['effort_options']
    selection = [{'id': 'chat-one', 'effort': 'high'}, {'id': 'chat-two', 'effort': ''}]
    before = (root / '.env').read_text()
    denied = await post('/models/connections', {**proof, 'models': [{'id': 'denied'}]})
    assert not denied.json()['ok'] and (root / '.env').read_text() == before
    assert (await post('/models/connections', {**proof, 'base_url': 'https://changed.test/v1', 'models': selection})).status_code == 409
    assert (await post('/models/connections', {**proof, 'models': [{'id': 'chat-one', 'effort': 'max'}]})).status_code == 422
    saved = (await post('/models/connections', {**proof, 'models': selection})).json()
    assert saved['ok'], saved
    env_bytes = (root / '.env').read_bytes()
    assert b'EXISTING_KEY' in env_bytes and (root / '.env').stat().st_mode & 0o777 == 0o600
    model_yaml = (root / 'config' / 'models.yaml').read_text()
    assert draft['api_key'] not in model_yaml and yaml.safe_load(model_yaml)['budget']['daily_usd'] == 12
    assert config.models.providers['fixture'].api_key == draft['api_key']
    env_name = config.models.providers['fixture'].api_key_env
    del os.environ[env_name]
    os.environ['CARME_LOAD_ENV'] = '1'
    config = config_module.load(reload=True)
    assert config.models.providers['fixture'].api_key == draft['api_key'], 'key must survive .env reload literally'
    os.environ['CARME_LOAD_ENV'] = '0'
    result = (await client.get('/api/models')).json()
    assert 'api_key' not in json.dumps(result) and draft['api_key'] not in json.dumps(result)
    assert result['models'][0]['effort'] == 'high'
    kept_key = (await post('/models/connections/test', {k: v for k, v in draft.items() if k != 'api_key'})).json()
    assert kept_key['ok']
    kept_model = next(model for model in kept_key['models'] if model['id'] == 'chat-one')
    assert kept_model['effort_options'] == ['low', 'high'] and kept_model['effort_source'] == 'saved'
    changed_connection = (await post('/models/connections/test', {**draft, 'base_url': 'https://different.test/v1'})).json()
    assert all(model['effort_source'] == 'unknown' for model in changed_connection['models'])
    assert (await post('/models/connections/test', {**{k: v for k, v in draft.items() if k != 'api_key'}, 'base_url': 'https://different.test'})).status_code == 422
    print('PASS: connection discovery, failure states, effort detection, proof binding, secret persistence/redaction')

    for api_type, model_id, base in [('anthropic', 'claude-fixture', 'https://anthropic.fixture.test/v1'), ('openai_responses', 'chat-two', 'https://responses.fixture.test/v1'), ('openai_compatible', 'chat-two', 'https://compatible.fixture.test/v1')]:
        connection = {**draft, 'id': api_type, 'type': api_type, 'base_url': base}
        result = (await post('/models/connections/test', connection)).json()
        if api_type == 'anthropic':
            assert result['models'][0]['effort_options'] == ['low', 'high']
        response = (await post('/models/connections', {**connection, 'probe_id': result['probe_id'], 'models': [{'id': model_id, 'effort': 'high' if api_type == 'anthropic' else ''}]})).json()
        assert response['ok'], response
    assert any(item['path'] == '/v1/messages' and item['body'].get('output_config') == {'effort': 'high'} for item in received)
    assert any(item['path'] == '/v1/responses' and item['body']['store'] is False for item in received)
    assert any('max_tokens' in item['body'] and item['path'] == '/v1/chat/completions' for item in received)
    assert (await runtime.gateway.probe())['anthropic']['models'] == ['claude-fixture']
    print('PASS: OpenAI Chat/Responses, Anthropic, compatible protocol payloads')
    roles_before = copy.deepcopy(runtime.config.agents)
    assert (await client.patch('/api/models/routing', json={'tiers': {'balanced': ['missing/model']}, 'allow_mock': False})).status_code == 422
    routing = await client.patch('/api/models/routing', json={'tiers': {'balanced': ['fixture/chat-one']}, 'allow_mock': False})
    assert routing.status_code == 200, routing.text
    routed = (await client.get('/api/models')).json()
    assert routed['tiers']['balanced'] == ['fixture/chat-one'] and routed['allow_mock'] is False
    assert runtime.config.agents == roles_before and runtime.config.models.budget_daily_usd == 12
    print('PASS: default model routing persists without changing Bot roles or budget')

    tool_schema = [{'type': 'function', 'function': {'name': 'fixture_tool', 'parameters': {'type': 'object', 'properties': {'value': {'type': 'integer'}}}}}]
    for ref in ['fixture/chat-one', 'openai_responses/chat-two', 'anthropic/claude-fixture', 'openai_compatible/chat-two']:
        messages = [{'role': 'user', 'content': 'Use the fixture tool'}]
        first = await runtime.gateway.chat(messages, model=ref, tools=tool_schema)
        assert first.tool_calls[0].arguments == {'value': 7}
        messages.extend([Agent._assistant_message(first), {'role': 'tool', 'tool_call_id': first.tool_calls[0].id, 'content': 'fixture tool result'}])
        final = await runtime.gateway.chat(messages, model=ref, tools=tool_schema)
        assert final.text.startswith('OK') and not final.tool_calls
    print('PASS: complete tool call round trips preserve Responses reasoning and Anthropic signatures')

    cid = store.create_conversation(['chief'])['id']
    store.remember('chief', 'favorite', 'teal')
    original = copy.deepcopy(runtime.config.agents.get('chief'))
    async def run_turn(content, rid):
        result = await post(f'/conversations/{cid}/messages', {'content': content, 'request_id': rid})
        task_id = result.json()['task_id']
        while runtime._jobs:
            await asyncio.gather(*list(runtime._jobs.values()), return_exceptions=True)
        assert store.get_task(task_id)['status'] == 'done', store.get_task(task_id)
    assert (await client.patch('/api/agents/chief', json={'model': 'fixture/chat-one'})).status_code == 200
    await run_turn('Remember our first conversation', 'first')
    assert (await client.patch('/api/agents/chief', json={'model': 'anthropic/claude-fixture'})).status_code == 200
    await run_turn('Continue after switching models', 'second')
    current = runtime.config.agents.get('chief')
    assert (current.id, current.name, current.prompt, current.title) == (original.id, original.name, original.prompt, original.title)
    assert store.recall('chief')[0]['value'] == 'teal'
    last = received[-1]['body']
    assert last['model'] == 'claude-fixture' and last['output_config']['effort'] == 'high'
    assert 'Remember our first conversation' in json.dumps(last['messages']) and 'OK chat' in json.dumps(last['messages'])
    assert 'teal' in json.dumps(last['messages']) and original.prompt in last['system']
    assert len(store.list_conversation_messages(cid)) == 4
    print('PASS: model switching preserves real Runtime history, memory and role')

    image = io.BytesIO()
    Image.new('RGB', (720, 400), '#18bfae').save(image, 'PNG')
    image_data = image.getvalue()
    uploaded = await client.post('/api/avatars', content=image_data, headers={'Content-Type': 'image/png'})
    assert uploaded.status_code == 201, uploaded.text
    avatar = uploaded.json()['avatar']
    target = root / 'data' / 'avatars' / avatar['file']
    with Image.open(target) as result:
        assert result.size == (512, 512) and result.format == 'WEBP' and not result.getexif()
    assert (await client.patch('/api/agents/chief', json={'avatar': avatar})).status_code == 200
    assert (await client.get('/api/agents/chief')).json()['avatar'] == avatar
    assert (await client.get('/api/avatars/' + avatar['file'])).status_code == 200
    os.environ['CARME_TOKEN'] = 'fixture-access'
    assert (await client.get('/api/avatars/' + avatar['file'])).status_code == 401
    assert (await client.get('/api/avatars/' + avatar['file'], headers={'Authorization': 'Bearer fixture-access'})).status_code == 200
    os.environ.pop('CARME_TOKEN')
    assert (await client.post('/api/avatars', content=b'not a PNG', headers={'Content-Type': 'image/png'})).status_code == 422
    assert (await client.post('/api/avatars', content=b'<svg/>', headers={'Content-Type': 'image/svg+xml'})).status_code == 415
    assert (await client.post('/api/avatars', content=b'x' * (5 * 1024 * 1024 + 1), headers={'Content-Type': 'image/png'})).status_code == 413
    small = io.BytesIO(); Image.new('RGB', (16, 16)).save(small, 'PNG')
    assert (await client.post('/api/avatars', content=small.getvalue(), headers={'Content-Type': 'image/png'})).status_code == 422
    assert (await client.patch('/api/agents/chief', json={'avatar': {'kind': 'image', 'file': '../secret'}})).status_code == 422
    assert (await client.patch('/api/agents/chief', json={'avatar': {'kind': 'bot', 'shape': 'hexagon', 'color': '#18bfae'}})).status_code == 200
    assert (await client.patch('/api/agents/chief', json={'avatar': {}})).status_code == 200
    assert (await client.get('/api/agents/chief')).json()['avatar'] == {}
    assert store.recall('chief')[0]['value'] == 'teal'
    print('PASS: image upload/normalisation, auth, size/type/path checks, presets and reset')
    await client.aclose(); await runtime.shutdown(); store.close()


async def live_check(profile: Path, model: str):
    """显式指定时才调用真实 API；只发送合成内容，数据库和角色完全隔离。"""
    from carme.config import AgentSpec, AgentsConfig, BrowserConfig, Config, SandboxConfig
    config_module.CONFIG_DIR = profile / 'config'
    config_module.ENV_FILE = profile / '.env'
    os.environ['CARME_LOAD_ENV'] = '1'
    models = copy.deepcopy(config_module.load(reload=True).models)
    _, provider, _ = models.resolve(model)
    assert provider.available and provider.type != 'mock', 'A real configured model is required'
    models.allow_mock = False
    with tempfile.TemporaryDirectory(prefix='carme-live-check-') as directory:
        root = Path(directory)
        agents = AgentsConfig(defaults={'max_steps': 4}, agents={
            'chief': AgentSpec('chief', '验收主控', model=model, entry=True, sandbox='none', tools=['team'], can_delegate=True,
                prompt='这是合成验收任务。严格按任务要求调用工具，不虚构工具结果。'),
            'research': AgentSpec('research', '验收成员', model=model, sandbox='none', tools=['remember'],
                prompt='必须先调用 remember 保存指定的记忆键和值，然后仅返回保存的值。'),
        })
        config = Config(agents, models, SandboxConfig(), BrowserConfig(enabled=False), root=root)
        store = Store(root / 'check.db')
        runtime = Runtime(config, store, EventBus())
        nonce = 'CARME-CHECK-' + os.urandom(4).hex()
        original_chat = runtime.gateway.chat
        calls = 0
        async def bounded_chat(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 7:
                raise RuntimeError('Live acceptance call limit reached')
            kwargs.update(max_tokens=4096, retries_per_model=1)
            response = await original_chat(*args, **kwargs)
            print(json.dumps({'call': calls, 'stop': response.stop_reason,
                              'tools': [{'name': tool.name, 'fields': sorted(tool.arguments),
                                         'contains_test_value': nonce in json.dumps(tool.arguments),
                                         'correct_memory_key': tool.arguments.get('key') == 'synthetic_check',
                                         'shared': tool.arguments.get('shared', False)} for tool in response.tool_calls],
                              'text_length': len(response.text), 'output_tokens': response.usage.completion_tokens}), flush=True)
            return response
        runtime.gateway.chat = bounded_chat
        try:
            cid = store.create_conversation(['chief', 'research'])['id']
            member_goal = '调用一次 remember，严格使用此 JSON 参数：' + json.dumps({'key': 'synthetic_check', 'value': nonce, 'shared': False}) + '。注意 key 是记忆名称，value 才是内容，两者不可互换。执行成功后仅回复 value 的内容。'
            submitted = await runtime.submit_message(cid, '请调用一次 delegate，agent 为 research，goal 原样传递以下成员任务：\n' + member_goal + '\n收到成员结果后只转述该结果。', 'live-check')
            async def settle():
                while runtime._jobs:
                    await asyncio.gather(*list(runtime._jobs.values()), return_exceptions=True)
            await asyncio.wait_for(settle(), 150)
            task = store.get_task(submitted['task_id'])
            children = store.children_of(task['id'])
            print(json.dumps({'status': task['status'], 'result': task['result'][:500], 'error': task['error'][:500],
                              'children': [{'status': child['status'], 'goal_has_test_value': nonce in child['goal'],
                                            'result': child['result'][:300], 'error': child['error'][:300],
                                            'tools': [{'tool': message['tool_name'], 'result': message['content'][:300]} for message in store.list_messages(child['id']) if message['role'] == 'tool']}
                                           for child in children]}, ensure_ascii=False).replace(provider.api_key, '[redacted]'), flush=True)
            assert task['status'] == 'done' and task['result'], 'Live task did not complete'
            assert children and all(child['status'] == 'done' for child in children), 'Delegation did not complete'
            remembered = store.recall('research', 'synthetic_check')
            assert remembered and remembered[0]['value'] == nonce, 'Member did not persist the requested private memory value'
            print(json.dumps({'execution_ok': True, 'summary_matches_expected_value': nonce in task['result'],
                              'model': model, 'calls': calls, 'tasks': 1 + len(children),
                              'tokens': sum(item['tokens'] for item in [task, *children]), 'verified': ['real model', 'delegate', 'remember', 'summary'],
                              'data': 'synthetic only; isolated database'}, ensure_ascii=False))
        finally:
            await runtime.shutdown()
            store.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--live-profile', type=Path)
    parser.add_argument('--model')
    args = parser.parse_args()
    if args.live_profile:
        if not args.model:
            parser.error('--live-profile requires --model')
        asyncio.run(live_check(args.live_profile.resolve(), args.model))
    else:
        with tempfile.TemporaryDirectory(prefix='carme-model-settings-') as directory:
            asyncio.run(main(Path(directory)))
