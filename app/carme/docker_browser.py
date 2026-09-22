"""Docker-only web tools and a bounded public HTTP relay. No host browser/CDP.

Chromium has --network=none. Each intercepted request is resolved, checked and
connected to that exact public IP by Control; redirects re-enter the same check.
The relay never loads Control credentials, environment proxies or browser files.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .tools.base import TOOL_GROUPS

WEB_TOOLS = frozenset(TOOL_GROUPS['computer'] + TOOL_GROUPS['browser'])
PROFILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
MAX_BODY = 4 * 1024 * 1024
MAX_UPLOAD = 1024 * 1024
DEFAULT_DANGER = ['删除', '移除', '注销', '支付', '付款', '转账', '提现', '购买', '提交',
                  '发送', '发布', '授权', 'delete', 'remove', 'pay', 'purchase', 'checkout',
                  'transfer', 'unsubscribe', 'authorize', 'submit', 'send', 'publish']


def web_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise ValueError('browser_url_denied')
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', value):
        value = 'https://' + value
    url = urlsplit(value)
    if (url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password
            or url.port not in (None, 80 if url.scheme == 'http' else 443)):
        raise ValueError('browser_url_denied: public HTTP/HTTPS standard ports only')
    host = url.hostname.lower().rstrip('.')
    if (host in {'localhost', 'host.docker.internal', 'gateway.docker.internal'}
            or host.endswith(('.localhost', '.local', '.internal', '.home.arpa'))):
        raise ValueError('browser_private_destination_denied')
    return value


async def public_address(host, port):
    addresses = {r[4][0] for r in await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)}
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast or getattr(ip, 'ipv4_mapped', None):
            raise ValueError('browser_private_destination_denied')
    if not addresses:
        raise ValueError('browser_dns_empty')
    return sorted(addresses)[0]


async def fetch_public(data, safety, check):
    """No redirects in this call, no pooling/re-resolution, no credential injection."""
    if not isinstance(data, dict) or set(data) != {'url', 'method', 'headers', 'body'}:
        raise ValueError('browser_request_fields_denied')
    value = web_url(data['url'])
    url = httpx.URL(value)
    host = url.host.lower().rstrip('.')
    matches = lambda p: host == p.lower().lstrip('.') or host.endswith('.' + p.lower().lstrip('.'))
    if (any(matches(p) for p in safety.get('blocked_domains', [])) or
            (safety.get('allowed_domains') and not any(matches(p) for p in safety['allowed_domains']))):
        raise ValueError('browser_domain_denied')
    if data['method'] not in {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}:
        raise ValueError('browser_method_denied')
    if not isinstance(data['headers'], dict) or len(data['headers']) > 100:
        raise ValueError('browser_headers_denied')
    raw = base64.b64decode(data['body'], validate=True)
    if len(raw) > MAX_UPLOAD:
        raise ValueError('browser_upload_limit')
    # Site cookies and Authorization may pass; never use ambient Control headers.
    excluded = {'host', 'connection', 'proxy-authorization', 'proxy-connection', 'transfer-encoding',
                'content-length', 'accept-encoding', 'te', 'trailer', 'upgrade'}
    headers = {k: v for k, v in data['headers'].items() if k.lower() not in excluded}
    if len(json.dumps(headers).encode()) > 32768:
        raise ValueError('browser_headers_limit')
    headers['Host'] = url.netloc.decode('ascii')
    headers['Accept-Encoding'] = 'identity'
    address = await public_address(host, url.port or (443 if url.scheme == 'https' else 80))
    check()
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=20) as client:
        async with client.stream(data['method'], url.copy_with(host=address), headers=headers,
                                 content=raw, extensions={'sni_hostname': host}) as response:
            output = bytearray()
            async for chunk in response.aiter_bytes():
                check()
                output.extend(chunk)
                if len(output) > MAX_BODY:
                    raise ValueError('browser_response_limit')
            # Decoded bytes: strip compression/framing. Preserve separate Set-Cookie fields.
            response_headers = [[k, v] for k, v in response.headers.multi_items()
                if k.lower() not in {'content-encoding', 'content-length', 'transfer-encoding',
                                     'connection', 'alt-svc', 'upgrade'}]
            if len(json.dumps(response_headers).encode()) > 65536:
                raise ValueError('browser_response_headers_limit')
            return {'status': response.status_code, 'headers': response_headers,
                    'body': base64.b64encode(output).decode()}


async def serve(payload, rpc):
    """Fixed Browser image entry point, one Bot/profile and one run per container."""
    import fcntl
    from types import SimpleNamespace
    from .approval import ApprovalOutcome
    from .browser import BrowserManager
    from .tools.base import ToolContext
    from .tools.web import WEB_TOOLS as AUTOMATION_TOOLS
    from .tools.browser import FetchPageTool, WebSearchTool

    lock = Path('/profile/.carme-lock').open('a')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return {'error': 'browser_identity_busy: another run owns this Bot/profile'}
    channel = payload.get('browser_channel') or ''
    if not isinstance(channel, str) or not re.fullmatch(r'(?:[a-z][a-z0-9-]{0,19})?', channel):
        return {'error': 'browser_channel_denied'}
    # stealth 必须开：Docker 这条通道恰恰是最容易被风控看穿的一条（无头壳 + 机房 IP），
    # 关掉它只会让 navigator.plugins / languages 这类自报字段更难解释。宿主路径本来
    # 就默认开着（session.py 里 settings.get("stealth", True)），这里不再是例外。
    settings = {'enabled': True, 'default_profile': payload['profile'], 'headless': True,
        'safety': payload['safety'], 'screenshots': {'dir': '/runtime/screenshots', 'keep': 4},
        'profiles': {payload['profile']: {'user_data_dir': '/profile', 'stealth': True,
                                        'docker_relay': rpc, 'channel': channel}}}
    manager = BrowserManager(settings, Path('/runtime'))
    tools = {t.name: t for t in [*AUTOMATION_TOOLS, FetchPageTool(), WebSearchTool()]}
    async def approve(**kwargs):
        return ApprovalOutcome(**await rpc('browser_approve', kwargs))
    async def request(url):
        for _ in range(6):
            result = await rpc('browser_fetch', {'url': url, 'method': 'GET', 'headers': {}, 'body': ''})
            if 'denied' in result:
                raise ValueError(result['denied'])
            response = httpx.Response(result['status'], headers=result['headers'],
                content=base64.b64decode(result['body']), request=httpx.Request('GET', url))
            if response.is_redirect:
                url = str(response.url.join(response.headers['location']))
                continue
            response.raise_for_status()
            return response
        raise ValueError('browser_redirect_limit')
    ctx = ToolContext(agent=SimpleNamespace(id=payload['bot_id'], tools=list(WEB_TOOLS)),
        task_id=payload['run_id'], store=None, browser_manager=manager, approve=approve,
        extras={'http_request': request, 'docker_browser': True})
    try:
        while True:
            command = await rpc('browser_next', {})
            if not command:
                continue
            if command == {'close': True}:
                return {'closed': True}
            result = {'text': '', 'files': []}
            try:
                name, arguments = command['name'], command['arguments']
                if name not in WEB_TOOLS or arguments.get('profile', payload['profile']) not in {'', payload['profile']}:
                    raise ValueError('browser_command_scope_denied')
                # No arbitrary methods, JavaScript, selectors, paths or launch arguments.
                async with asyncio.timeout(float(payload.get('tool_timeout') or 240)):
                    result['text'] = await tools[name].run(ctx, **arguments)
                for path in Path('/runtime/screenshots').rglob('*.png'):
                    raw = path.read_bytes()
                    if len(raw) > MAX_BODY:
                        raise ValueError('browser_screenshot_limit')
                    result['files'].append({'name': path.name, 'body': base64.b64encode(raw).decode()})
                    path.unlink()
                if len(result['text'].encode()) > payload['max_output_bytes']:
                    result['text'] = result['text'].encode()[:payload['max_output_bytes']].decode(errors='ignore') + '\n[output_limit_exceeded]'
            except Exception as exc:
                result = {'text': '[Docker Browser 失败] ' + type(exc).__name__ + ': ' + str(exc)[:300], 'files': []}
            await rpc('browser_result', {'id': command['id'], **result})
    finally:
        await manager.close_all()
        lock.close()
