"""Real Docker/Chromium with isolated Control fixtures and signed Broker transport.

HTML responses at fixture.invalid are synthetic; public example.com and denied
destinations use the real production HTTP relay. No personal browser/profile.
"""
import asyncio, base64, copy, hashlib, json, os, secrets, sys, time
from pathlib import Path
from unittest.mock import patch
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_isolation_m1 as m1
from test_docker_browser import Browser
from carme.approval import ApprovalOutcome
from carme.broker import Broker
from carme.docker_browser import fetch_public as real_fetch
from carme.execution import build_execution_router
from carme.tools.base import ToolContext

params = json.loads(Path(sys.argv[1]).read_text())
work = Path(params['work']).resolve();assert str(work).startswith('/private/tmp/carme-browser-')
out = Path(params['evidence']);out.mkdir(parents=True, exist_ok=True)
checks, captures, inspected = [], [], []
def record(name, **details):
    checks.append({'name': name, 'status': 'pass', **details})
    (out/'live-results.json').write_text(json.dumps({'checks': checks, 'count': len(checks),
        'layer': 'real Docker Browser; synthetic Control ASGI transport and fixture HTML; real public HTTP/negative egress'}, indent=2))
    print('PASS', name, flush=True)

HTML = '''<!doctype html><html><head><title>Carme isolated browser fixture</title></head><body>
<h1>Fixture page</h1><p id="count">count=0</p><label>Input<input aria-label="Fixture input"></label>
<button onclick="document.getElementById('count').textContent='count=1'">Increment</button>
<button onclick="document.getElementById('count').textContent='DELETED'">Delete fixture</button>
<p id="cookie"></p><script>document.getElementById('cookie').textContent=document.cookie || 'NO_COOKIE';</script>
</body></html>'''
async def fixture_fetch(data, safety, check):
    if data.get('url', '').startswith('https://fixture.invalid/'):
        check();captures.append({'url': data['url'], 'headers': data['headers']})
        headers = [['content-type', 'text/html']]
        if data['url'].endswith('/alice'):
            headers.append(['set-cookie', 'carme_fixture=ALICE_ONLY; Path=/; Max-Age=3600; Secure; SameSite=Lax'])
        if data['url'].endswith('/hop'):
            # 相对 Location 的重定向。容器里的 Chromium 是 --network=none，跟不了跳：
            # 一旦把 3xx 交给它，主文档导航必挂 net::ERR_INTERNET_DISCONNECTED。
            # 所以重定向必须由中继自己跟到底（session.py relay_follow）。这条就是哨兵。
            headers.append(['location', '/target'])
            return {'status': 302, 'headers': headers, 'body': base64.b64encode(b'moved').decode()}
        return {'status': 200, 'headers': headers, 'body': base64.b64encode(HTML.encode()).decode()}
    return await real_fetch(data, safety, check)


async def main():
    accounts = []
    async def new_account(label):
        fixture = m1.M1();await fixture.asyncSetUp();fixture.root = fixture.root.resolve()
        ctx = Browser.configure(fixture)
        (fixture.root / 'key').write_text(secrets.token_hex(32))
        runtime = fixture.runtime;runtime.config.isolation['broker']['instance_id'] = label + '-' + secrets.token_hex(4)
        runtime.config.isolation['browser']['image_digest'] = params['images']['browser']
        # Source policy is recomputed after the image binding; no stale permission version.
        home = fixture.root / 'dedicated';home.mkdir()
        cfg = {'home': str(home), 'instance_id': runtime.config.isolation['broker']['instance_id'],
            'key_file': str(fixture.root/'key'), 'control_url': 'http://127.0.0.1:9999',
            'docker_binary': params['docker'], 'docker_context': params['docker_context'],
            'docker_config': params['docker_config'], 'images': params['images'], 'targets': ['action'], 'max_browser': 2}
        app = fixture.app();app.include_router(build_execution_router(runtime.execution))
        broker = Broker(cfg);await broker.client.aclose()
        broker.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=cfg['control_url'])
        broker_task = asyncio.create_task(broker.serve())
        accounts.append((fixture, broker, broker_task))
        for _ in range(100):
            if runtime.execution.health()['browser'] == 'ready':break
            if broker_task.done():broker_task.result()
            await asyncio.sleep(.1)
        else:raise RuntimeError('Browser broker startup timeout')
        return fixture, broker

    def context(fixture, bot='bot', approve=False):
        if bot not in fixture.config.agents.agents:
            spec = copy.deepcopy(fixture.config.agents.get('bot'));spec.id = bot
            fixture.config.agents.agents[bot] = spec
        spec = fixture.config.agents.get(bot)
        _, meta = fixture.runtime._task_agent_snapshot(bot)
        cid = fixture.store.create_conversation([bot])['id']
        tid = fixture.store.create_task(bot, 'synthetic browser test', meta=meta, conversation_id=cid)
        fixture.store.set_task_status(tid, 'running')
        async def approval(**kwargs):
            assert kwargs['detail']['action_digest']
            captures.append({'approval': kwargs['kind'], 'approved': approve})
            return ApprovalOutcome(approve, 'synthetic fixture only')
        return ToolContext(spec, tid, fixture.store, browser_manager=fixture.runtime.browsers, approve=approval,
            extras={'policy': meta['policy'], 'check_policy': lambda: fixture.runtime.check_task_policy(tid)})

    async def call(fixture, ctx, tool_name, **arguments):
        result = await asyncio.wait_for(fixture.runtime.registry.execute(ctx, tool_name, arguments), timeout=60)
        (out/'last-operation.json').write_text(json.dumps({'tool': tool_name, 'result': result}, ensure_ascii=False, indent=2))
        return result

    try:
        a, ba = await new_account('alice');b, bb = await new_account('bob')
        ca, cb = context(a), context(b)
        with patch('carme.docker_browser.fetch_public', side_effect=fixture_fetch):
            first = await call(a, ca, 'web_open', url='https://fixture.invalid/alice')
            assert 'Fixture page' in first, first
            record('real_chromium_navigates_through_signed_task_relay')
            for broker in (ba,):
                ids = await broker.docker('ps', '-q', '--filter', 'label=carme.instance=' + broker.config['instance_id'], '--filter', 'label=carme.role=browser')
                assert ids
                for identity in ids.splitlines():
                    row = json.loads(await broker.docker('inspect', identity))[0]
                    host, config = row['HostConfig'], row['Config']
                    assert host['NetworkMode'] == 'none' and host['ReadonlyRootfs'] and config['User'] == '1000:1000'
                    assert host['CapDrop'] == ['ALL'] and not host['Privileged'] and host['PidsLimit'] == 256
                    assert host['Memory'] == host['MemorySwap'] == 1024**3
                    assert len(row['Mounts']) == 1 and row['Mounts'][0]['Destination'] == '/profile'
                    assert 'no-new-privileges:true' in host['SecurityOpt'] and any(s.startswith('seccomp=') for s in host['SecurityOpt'])
                    assert not any(k.split('=')[0] in {'CARME_TOKEN', 'ANTHROPIC_API_KEY', 'TAVILY_API_KEY'} for k in config['Env'])
                    report = await broker.docker('exec', identity, 'python', '-c',
                        "import json,os,socket;from pathlib import Path;print(json.dumps({'uid':os.getuid(),'visible':{p:Path(p).exists() for p in ['/run/secrets','/control-state','/var/run/docker.sock','/workspace','/Users/you/.pi']}}))")
                    assert json.loads(report)['uid'] == 1000 and not any(json.loads(report)['visible'].values())
                    network = await broker.docker('exec', identity, 'python', '-c',
                        "import json,socket\nr={}\nfor h in ['1.1.1.1','169.254.169.254','127.0.0.1','host.docker.internal','::1']:\n try:\n  s=socket.create_connection((h,80),timeout=.2);s.close();r[h]=True\n except OSError:r[h]=False\nprint(json.dumps(r))")
                    assert not any(json.loads(network).values())
                    sandbox = await broker.docker('exec', identity, 'python', '-c',
                        "import json;from pathlib import Path\na=[]\nfor p in Path('/proc').glob('[0-9]*/cmdline'):\n try:\n  b=p.read_bytes()\n  args=b.split(bytes([0]))\n  if p.parent.joinpath('comm').read_text().strip().startswith('chrome') and b'--remote-debugging-pipe' in b:a.append(b'--no-sandbox' not in b)\n except OSError:pass\nprint(json.dumps(a))")
                    assert json.loads(sandbox) and all(json.loads(sandbox)), sandbox
                    inspected.append({k: row[k] for k in ('Name', 'Config', 'HostConfig', 'Mounts')})
            record('actual_container_caps_mounts_network_identity_and_secrets')
            assert '输入' in await call(a, ca, 'web_type', ref=1, text='SYNTHETIC_FORM_VALUE')
            assert 'count=1' in await call(a, ca, 'web_click', ref=2)
            denied = await call(a, ca, 'web_click', ref=3)
            assert '已拦截' in denied and 'DELETED' not in await call(a, ca, 'web_snapshot')
            record('real_form_type_click_and_bound_approval_refusal')
            shot = await call(a, ca, 'web_screenshot', name='fixture')
            assert '已归档截图' in shot, shot
            task = a.store.get_task(ca.task_id)
            files = a.store.list_attachments(task['conversation_id'])
            assert files and files[-1]['mime'] == 'image/png' and files[-1]['sha256']
            record('screenshot_archived_with_conversation_owner_and_hash')
            second = await call(b, cb, 'web_open', url='https://fixture.invalid/check')
            assert 'NO_COOKIE' in second and 'ALICE_ONLY' not in second
            peer = context(a, 'peer')
            peer_result = await call(a, peer, 'web_open', url='https://fixture.invalid/check')
            assert 'NO_COOKIE' in peer_result and 'ALICE_ONLY' not in peer_result
            record('same_profile_name_different_accounts_and_bots_do_not_share_cookies')
            await a.runtime.execution.close_browsers(peer.task_id)
            concurrent = context(a)
            busy = await call(a, concurrent, 'web_open', url='https://fixture.invalid/check')
            assert 'browser_identity_busy' in busy, busy
            await a.runtime.execution.close_browsers(concurrent.task_id)
            record('same_bot_profile_concurrent_run_refused_without_wait_deadlock')
            await call(a, ca, 'web_close')
            assert not any(k[0] == ca.task_id for k in a.runtime.execution.browser_sessions)
            resumed = context(a)
            saved = await call(a, resumed, 'web_open', url='https://fixture.invalid/check')
            assert 'ALICE_ONLY' in saved, saved
            record('cookie_persists_after_graceful_browser_stop_within_own_identity')
            for url in ('file:///profile/Default/Cookies', 'http://127.0.0.1', 'http://host.docker.internal',
                        'http://169.254.169.254', 'http://[::1]', 'http://10.0.0.1'):
                denied = await call(a, resumed, 'web_open', url=url)
                assert '失败' in denied or 'denied' in denied, (url, denied)
            record('real_browser_denies_file_loopback_host_metadata_ipv6_private')
            external = await call(a, resumed, 'web_open', url='https://example.com')
            assert 'Example Domain' in external, external
            record('real_public_https_through_pinned_dns_relay')
            hopped = await call(a, resumed, 'web_open', url='https://fixture.invalid/hop')
            assert 'Fixture page' in hopped, hopped
            hops = [c['url'] for c in captures if '/hop' in c.get('url', '') or '/target' in c.get('url', '')]
            assert hops == ['https://fixture.invalid/hop', 'https://fixture.invalid/target'], hops
            record('redirect_followed_inside_relay_not_by_chromium', hops=hops)
            ids = await ba.docker('ps', '-q', '--filter', 'label=carme.run=' + resumed.task_id,
                                  '--filter', 'label=carme.role=browser')
            assert ids
            await ba.docker('kill', ids.splitlines()[0])
            await asyncio.sleep(1)
            failed = await call(a, resumed, 'web_snapshot')
            assert 'container_execution_failed' in failed or 'browser_interrupted' in failed, failed
            record('real_browser_container_crash_never_replays_on_host')
            await a.runtime.execution.close_browsers(resumed.task_id)
            reopened = context(a)
            assert 'ALICE_ONLY' in await call(a, reopened, 'web_open', url='https://fixture.invalid/check')
            await a.runtime.execution.close_browsers(reopened.task_id)
            record('explicit_new_run_after_crash_preserves_identity_without_action_replay')
            a.runtime.execution.broker_seen = 0
            offline = await call(a, context(a), 'web_open', url='https://example.com')
            assert 'unavailable' in offline, offline
            record('offline_browser_does_not_fall_back_to_host')
            assert 'NO_COOKIE' in await call(b, cb, 'web_snapshot')
            record('stopping_alice_does_not_interrupt_bob_browser')
            b.store.set_task_status(cb.task_id, 'cancelled')
            await b.runtime.execution.close_browsers(cb.task_id)
        for _, broker, _ in accounts:
            for _ in range(60):
                ids = await broker.docker('ps', '-aq', '--filter', 'label=carme.instance=' + broker.config['instance_id'])
                if not ids:break
                await asyncio.sleep(.2)
            assert not ids
        record('both_accounts_leave_no_browser_containers_after_close')
        (out/'container-inspect.json').write_text(json.dumps(inspected, indent=2))
    finally:
        for fixture, broker, task in reversed(accounts):
            for run, _ in list(fixture.runtime.execution.browser_sessions):
                await fixture.runtime.execution.close_browsers(run)
            broker.stopping = True
            await asyncio.gather(task, return_exceptions=True)
            await fixture.asyncTearDown()


if __name__ == '__main__':asyncio.run(main())
