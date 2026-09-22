"""Web/Mac routing boundaries. Only synthetic identities and temporary data."""
import asyncio, base64, json, os, socket, sys, time, unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_isolation_m1 as m1
import httpx
from carme.broker import Broker, validate_spec
from carme.docker_browser import WEB_TOOLS, fetch_public, public_address, web_url
from carme.engines import BRIDGE_TOOL_NAMES
from carme.execution import build_execution_router
from carme.tools.base import ToolContext


class Browser(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = m1.M1.asyncSetUp
    asyncTearDown = m1.M1.asyncTearDown
    app = m1.M1.app

    def configure(self):
        spec = self.config.agents.get('bot')
        spec.tools = ['browser', 'computer', 'mac_action']
        spec.execution_target, spec.execution_target_id = 'container', 'action'
        self.config.browser.enabled = True
        key = self.root / 'key';key.write_bytes(b'x' * 32)
        self.config.isolation = {'broker': {'key_file': str(key), 'instance_id': 'fixture'},
            'browser': {'image_digest': 'sha256:' + '0' * 64},
            'targets': {'action': {'image_digest': 'sha256:' + '0' * 64, 'tools': ['browser', 'computer']}}}
        _, meta = self.runtime._task_agent_snapshot('bot')
        run = self.store.create_task('bot', 'fixture', meta=meta)
        self.store.set_task_status(run, 'running')
        return ToolContext(spec, run, self.store, browser_manager=self.runtime.browsers,
            extras={'policy': meta['policy'], 'check_policy': lambda: self.runtime.check_task_policy(run)})

    async def test_api_pi_same_web_ceiling_no_native_escalation(self):
        self.configure()
        spec = self.config.agents.get('bot')
        policies = []
        for engine in ('api', 'pi'):
            spec.engine = engine
            policies.append(self.runtime.policy_for('bot', target='container', node={})['tools'])
        self.assertEqual(policies[0], policies[1])
        self.assertEqual(set(policies[0]), WEB_TOOLS)
        self.assertTrue(WEB_TOOLS <= BRIDGE_TOOL_NAMES)
        self.assertNotIn('mac_action', policies[0])
        for target in ('none', 'ssh', 'macos'):
            self.assertFalse(WEB_TOOLS & set(self.runtime.policy_for('bot', target=target, node={})['tools']))

    async def test_registry_routes_only_granted_web_into_docker(self):
        ctx = self.configure()
        with patch.object(self.runtime.execution, 'browser_tool', AsyncMock(return_value='container result')) as docker, \
             patch('carme.browser.session.BrowserSession.start', side_effect=AssertionError('host browser started')):
            self.assertEqual(await self.runtime.registry.execute(ctx, 'web_open', {'url': 'https://example.com'}), 'container result')
            docker.assert_awaited_once()
            self.config.agents.get('bot').tools = []
            self.assertIn('permission_version_changed', await self.runtime.registry.execute(ctx, 'web_open', {'url': 'https://example.com'}))
            docker.assert_awaited_once()

    async def test_unavailable_missing_target_no_fallback(self):
        ctx = self.configure()
        with patch('asyncio.create_subprocess_exec', side_effect=AssertionError('host spawn')):
            self.assertIn('docker_browser_unavailable', await self.runtime.registry.execute(ctx, 'web_open', {'url': 'https://example.com'}))
            self.assertFalse(self.runtime.execution.jobs)
            ctx.extras['policy']['tools'] = []
            self.assertIn('capability_denied', await self.runtime.registry.execute(ctx, 'web_open', {'url': 'https://example.com'}))

    async def test_profile_traversal_and_control_probe_never_launch_browser(self):
        ctx = self.configure()
        for value in ('../bob', '/Users/example/.pi', 'a/b', 'a,b', '.ssh'):
            with self.assertRaisesRegex(ValueError, 'browser_profile_denied'):
                await self.runtime.execution.browser_tool(ctx, 'web_open', {'profile': value})
        self.runtime.browsers.enabled = True
        with patch('carme.browser.session.BrowserSession.start', side_effect=AssertionError('host spawn')):
            result = await self.runtime.browsers.probe()
            self.assertEqual(result['execution'], 'docker-browser')
            with self.assertRaisesRegex(RuntimeError, 'docker_browser_task_required'):
                await self.runtime.browsers._get_session('default')

    async def test_no_direct_desktop_bypass(self):
        self.configure()
        with patch.dict(os.environ, {'CARME_TOKEN': 'synthetic-test-admin', 'CARME_DEV_NO_AUTH': '0'}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url='http://test', headers={'Authorization': 'Bearer synthetic-test-admin'}) as client:
                for path, body in [('/desktop/control', {'enabled': True}), ('/desktop/keyboard', {'keys': 'tab'}),
                                   ('/desktop/mouse', {'action': 'click', 'x': 1, 'y': 1})]:
                    self.assertEqual((await client.post('/api' + path, json=body)).status_code, 409)
                self.assertEqual((await client.get('/api/desktop/screenshot')).status_code, 409)

    async def test_url_schemes_ports_credentials_local_aliases(self):
        for value in ('file:///profile/Cookies', 'data:text/html,x', 'chrome://settings', 'ftp://example.org',
                      'https://u:p@example.org', 'http://example.org:8899', 'http://host.docker.internal',
                      'http://abc.localhost', 'http://localhost', 'https://example.org/\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):web_url(value)
        self.assertEqual(web_url('example.org'), 'https://example.org')

    async def test_private_ipv6_metadata_mixed_dns_and_rebinding(self):
        for addresses in [('127.0.0.1',), ('169.254.169.254',), ('10.1.2.3',), ('::1',), ('fc00::1',),
                          ('::ffff:127.0.0.1',), ('224.0.0.1',), ('93.184.216.34', '192.168.1.1')]:
            with patch('socket.getaddrinfo', return_value=[(0, 0, 0, '', (a, 80)) for a in addresses]):
                with self.subTest(addresses=addresses), self.assertRaisesRegex(ValueError, 'private_destination'):
                    await public_address('fixture.test', 80)
        with patch('socket.getaddrinfo', side_effect=[[(0, 0, 0, '', ('93.184.216.34', 80))], [(0, 0, 0, '', ('127.0.0.1', 80))]]):
            self.assertEqual(await public_address('rebind.test', 80), '93.184.216.34')
            with self.assertRaises(ValueError):await public_address('rebind.test', 80)

    async def test_public_relay_pins_ip_no_env_secrets_or_redirect_follow(self):
        seen = []
        def request(req):
            seen.append(req)
            return httpx.Response(302, headers={'Location': 'http://169.254.169.254/', 'Set-Cookie': 'fixture=1'}, content=b'ok')
        client_type = httpx.AsyncClient
        def client(**kwargs):
            self.assertFalse(kwargs['trust_env']);self.assertFalse(kwargs['follow_redirects'])
            return client_type(transport=httpx.MockTransport(request), **kwargs)
        with patch('carme.docker_browser.public_address', AsyncMock(return_value='93.184.216.34')), \
             patch('carme.docker_browser.httpx.AsyncClient', side_effect=client), \
             patch.dict(os.environ, {'HTTP_PROXY': 'http://localhost:1', 'CARME_TOKEN': 'MUST_NOT_LEAK'}):
            result = await fetch_public({'url': 'https://example.org/a?q=b', 'method': 'GET', 'headers': {'Host': 'localhost'}, 'body': ''}, {}, lambda: None)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.host, '93.184.216.34')
        self.assertEqual(seen[0].headers['host'], 'example.org')
        self.assertEqual(seen[0].extensions['sni_hostname'], 'example.org')
        self.assertNotIn('authorization', seen[0].headers)
        self.assertEqual(result['status'], 302)

    async def test_browser_token_cannot_call_model_shell_or_replay(self):
        ctx = self.configure();ex = self.runtime.execution
        ex.broker_seen = time.time();ex.broker_health = {'browser': 'ready'}
        task = asyncio.create_task(ex.browser_tool(ctx, 'web_open', {'url': 'https://example.org'}))
        await asyncio.sleep(.02);job = next(iter(ex.jobs.values()))
        try:
            for kind in ('model', 'tool', 'shell'):
                with self.assertRaisesRegex(ValueError, 'audience_denied'):
                    await ex.message(ctx.task_id, job['token'], {'sequence': len(job.get('browser_seen_events', [])) + 1,
                        'event_id': os.urandom(16).hex(), 'kind': kind, 'payload': {}})
            message = {'sequence': 10, 'event_id': 'e' * 32, 'kind': 'browser_next', 'payload': {}}
            self.assertEqual((await ex.message(ctx.task_id, job['token'], message))['name'], 'web_open')
            with self.assertRaisesRegex(ValueError, 'replay_denied'):
                await ex.message(ctx.task_id, job['token'], message)
        finally:
            task.cancel();await asyncio.gather(task, return_exceptions=True)
            await ex.close_browsers(ctx.task_id)


if __name__ == '__main__':unittest.main()
