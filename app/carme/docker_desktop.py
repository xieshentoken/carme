"""A bot's Linux desktop. Docker stays in Broker; model credentials stay in Control."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from .tools.base import Tool

ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z')
UI_TOOLS = {'find_roots', 'observe_ui', 'search_ui', 'expand_ui', 'inspect_ui', 'act_ui', 'read_text', 'wait_for', 'navigate_browser'}
BOT_OPS = UI_TOOLS | {'status', 'shell', 'fetch', 'publish', 'software_list', 'help'}
READ_OPS = {'status', 'screenshot', 'software_list', 'help'}
DESKTOP_READ_ONLY_OPS = READ_OPS | {'find_roots', 'observe_ui', 'search_ui', 'expand_ui', 'inspect_ui', 'read_text', 'wait_for'}
MAX_FRAME = 4 * 1024 * 1024


class DesktopNoEffectError(RuntimeError):
    """The Linux download failed before its destination was published."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def validate(op, args):
    if op not in BOT_OPS | {'screenshot', 'mouse', 'keyboard', 'release', 'web'}:
        raise ValueError('desktop_operation_denied')
    if not isinstance(args, dict) or len(json.dumps(args).encode()) > 65536:
        raise ValueError('desktop_arguments_limit')
    if set(args) & {'bot_id', 'run_id', 'account', 'mount', 'image', 'env', 'cwd', 'control_id'}:
        raise ValueError('desktop_identity_override_denied')
    if op == 'shell' and (set(args) != {'command'} or not isinstance(args['command'], str) or len(args['command']) > 16384):
        raise ValueError('desktop_shell_fields_denied')
    if op == 'keyboard':
        if set(args) - {'keys', 'text'} or bool(args.get('keys')) == bool(args.get('text')):
            raise ValueError('desktop_keyboard_fields_denied')
        if len(args.get('text', '')) > 4096 or len(args.get('keys', '')) > 80:
            raise ValueError('desktop_keyboard_limit')
    if op == 'mouse':
        if set(args) - {'action', 'x', 'y', 'dx', 'dy', 'button', 'clicks', 'delta_y'}:
            raise ValueError('desktop_mouse_fields_denied')
        if args.get('action') not in {'move', 'move_rel', 'click', 'scroll', 'press', 'release'}:
            raise ValueError('desktop_mouse_action_denied')
        for key in ('x', 'y', 'dx', 'dy', 'delta_y'):
            value = args.get(key)
            if value is not None and (type(value) not in {int, float} or not math.isfinite(value) or abs(value) > 100000):
                raise ValueError('desktop_mouse_number_denied')
        if args.get('button', 'left') not in {'left', 'right', 'middle'} or type(args.get('clicks', 1)) is not int or not 1 <= args.get('clicks', 1) <= 3:
            raise ValueError('desktop_mouse_button_denied')
    return args


class BotComputerTool(Tool):
    name = 'bot_computer'
    description = ('操作当前授权的 Bot 电脑。默认是独立 Linux；main 经后台限时授权后可用界面工具操作宿主 macOS。Linux 才支持 shell/fetch/publish。help(name) 返回上游工具参数格式。先用 find_roots → observe_ui，再用 act_ui 和 stateId。'
        'status 查看磁盘；shell 在 /home/bot 离线执行命令，不能直接 curl、pip、npm 联网；fetch 经受控通道下载公开 HTTPS 文件到 /home/bot/Downloads（url、name），连续 403 请换来源或交付已完成结果；'
        'publish 发布已测试的软件目录（name、version、path，path 相对 /home/bot）；'
        'software_list 查看账号共享软件。软件位于 /software，只读；/task-files/<task> 是只读任务输入；配置与工作文件位于 /home/bot。Action 的 /workspace、/out 与这里不互通；离线安装包可从 Downloads 安装。'
        '自己的 Linux 内可直接安装、修改、运行、删除文件和软件，无需逐步询问；对外提交数据仍需批准。'
        '外部控制开启时 Bot 操作暂停。没有宿主机或其他 Bot 的访问权限。')
    parameters = {'type': 'object', 'properties': {
        'operation': {'type': 'string', 'enum': sorted(BOT_OPS)},
        'arguments': {'type': 'object', 'description': '上游 computer-use 参数；shell: command；fetch: url/name；publish: name/version/path'}},
        'required': ['operation', 'arguments']}

    def __init__(self, execution): self.execution = execution

    async def run(self, ctx, operation, arguments):
        validate(operation, arguments)
        if operation == 'fetch':
            from urllib.parse import urlsplit
            url = str(arguments.get('url', ''))
            source = urlsplit(url).netloc.lower()
            failures = ctx.extras.setdefault('desktop_fetch_failures', {})
            source_key = hashlib.sha256(('source:' + source).encode()).hexdigest()
            url_key = hashlib.sha256(('url:' + url).encode()).hexdigest()
            if failures.get(source_key) or failures.get(url_key, 0) >= 2:
                raise DesktopNoEffectError('[下载来源暂不可用] 本任务已遇到同源 HTTP 403 或同一网址连续失败；请换来源或说明限制。')
        if not ctx.local_autonomy and operation not in READ_OPS | {'find_roots', 'observe_ui', 'search_ui', 'expand_ui', 'inspect_ui', 'read_text', 'wait_for'}:
            decision = await ctx.request_approval(kind='bot_computer', summary='操作 Bot 当前授权的电脑（含宿主访问）',
                                                   detail={'operation': operation, 'arguments': arguments})
            if not decision.approved: return '[已拒绝] ' + decision.note
        try:
            result = await self.execution.desktop_request(ctx.agent.id, operation, arguments, ctx=ctx)
        except DesktopNoEffectError as exc:
            if operation == 'fetch':
                failures[url_key] = failures.get(url_key, 0) + 1
                if exc.status_code == 403:
                    failures[source_key] = 1
            raise
        return await self.execution.desktop_result(ctx, result)


async def run_program(*args, stdin=None, timeout=20, limit=MAX_FRAME):
    from .security import bounded_output
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    if stdin is not None:
        proc.stdin.write(stdin); await proc.stdin.drain(); proc.stdin.close()
    out, err = await bounded_output(proc, timeout=timeout, limit=limit)
    if proc.returncode: raise RuntimeError('desktop_program_failed:' + Path(args[0]).name + ':' + err.decode(errors='replace')[:400])
    return out


async def stop_desktop(broker, bot_id):
    entry = broker.desktops.pop(bot_id, None)
    if not entry: return
    for key in ('reader', 'heartbeat'):
        task = entry.get(key)
        if task: task.cancel()
    for future in entry['pending'].values():
        if not future.done(): future.set_exception(RuntimeError('desktop_session_closed'))
    if entry['proc'].returncode is None:
        with contextlib.suppress(Exception):
            await entry['send']({'shutdown': True})
            await asyncio.wait_for(entry['proc'].wait(), 12)
    raw = await broker.docker('inspect', '--format', '{{json .Config.Labels}}', entry['name'])
    labels = json.loads(raw)
    if labels.get('carme.instance') != broker.config['instance_id'] or labels.get('carme.bot') != bot_id or labels.get('carme.role') != 'desktop':
        raise RuntimeError('desktop_cleanup_owner_mismatch')
    await broker.docker('rm', '-f', entry['name'])
    if entry['proc'].returncode is None: await entry['proc'].wait()
    if entry.get('stderr'):
        with contextlib.suppress(Exception): await asyncio.wait_for(entry['stderr'], 2)


async def start_desktop(broker, bot_id):
    async with broker.desktop_start_lock:
        return await _start_desktop(broker, bot_id)


async def _start_desktop(broker, bot_id):
    from .account_disk import bot_home, checked
    if not ID.fullmatch(bot_id): raise ValueError('desktop_bot_id_denied')
    old = broker.desktops.get(bot_id)
    if old and old['proc'].returncode is None:
        await asyncio.shield(old['ready'])
        old['last_used'] = time.monotonic(); return old
    if old: await stop_desktop(broker, bot_id)
    if len(broker.desktops) >= broker.config.get('max_desktop', 2):
        raise RuntimeError('desktop_capacity_busy: close an idle desktop first')
    disk = checked(broker.home); home = bot_home(broker.home, bot_id)
    apps = Path(disk['mount']) / 'apps'
    runs = broker.directory('storage', 'mount', 'runs', bot_id); runs.chmod(0o755)
    if apps.is_symlink() or apps.resolve() != apps: raise ValueError('desktop_apps_path_denied')
    name = f"carme-{broker.config['instance_id']}-desktop-{bot_id}"
    image = broker.config['images'].get('desktop', '')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image): raise ValueError('desktop_image_digest_required')
    args = ['create', '--name', name, '--pull=never', '--label', 'carme.instance=' + broker.config['instance_id'],
        '--label', 'carme.bot=' + bot_id, '--label', 'carme.role=desktop', '--network=none', '--read-only',
        '--user=1000:1000', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
        '--security-opt=seccomp=' + str(Path(__file__).resolve().parents[1] / 'deploy/docker/browser-seccomp.json'),
        '--pids-limit=512', '--memory=1536m', '--memory-swap=1536m', '--cpus=2', '--init', '--log-driver=none',
        '--shm-size=256m', '--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777',
        '--tmpfs=/runtime:rw,nosuid,nodev,size=192m,uid=1000,gid=1000,mode=700', '--interactive',
        '--mount', f'type=bind,src={home},dst=/home/bot',
        '--mount', f'type=bind,src={runs},dst=/task-files,readonly',
        '--mount', f'type=bind,src={apps},dst=/software,readonly', image]
    await broker.docker(*args)
    proc = await asyncio.create_subprocess_exec(broker.config['docker_binary'], '--config', broker.config['docker_config'],
        '--context', broker.config['docker_context'], 'start', '-ai', name,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=broker.env, limit=16 * 1024 * 1024)
    entry = {'name': name, 'proc': proc, 'pending': {}, 'lock': asyncio.Lock(), 'write_lock': asyncio.Lock(),
             'ready': asyncio.get_running_loop().create_future(), 'last_used': time.monotonic(), 'session_id': secrets.token_hex(16)}
    broker.desktops[bot_id] = entry
    async def send(value):
        async with entry['write_lock']:
            proc.stdin.write((json.dumps(value) + '\n').encode()); await proc.stdin.drain()
    entry['send'] = send
    async def relay(message):
        try:
            result = await broker.call({'op': 'desktop_rpc', 'bot_id': bot_id, 'session_id': entry['session_id'],
                                       'kind': message['rpc'], 'payload': message['payload']}, timeout=660)
            await send({'id': message['id'], 'result': result})
        except Exception as exc:
            with contextlib.suppress(Exception): await send({'id': message['id'], 'error': str(exc)[:200]})
    async def read():
        relays = set()
        try:
            while line := await proc.stdout.readline():
                message = json.loads(line)
                if message == {'ready': True}:
                    if not entry['ready'].done(): entry['ready'].set_result(True)
                elif message.get('rpc') in {'browser_fetch', 'approve', 'download'}:
                    if len(relays) >= 32: raise RuntimeError('desktop_network_concurrency_limit')
                    task = asyncio.create_task(relay(message)); relays.add(task); task.add_done_callback(relays.discard)
                elif message.get('id') in entry['pending']:
                    future = entry['pending'][message['id']]
                    if not future.done():
                        if 'error' in message: future.set_exception(RuntimeError(message['error']))
                        else: future.set_result(message['result'])
                else: raise ValueError('desktop_protocol_denied')
            raise RuntimeError('desktop_worker_exited')
        except Exception as exc:
            for future in [entry['ready'], *entry['pending'].values()]:
                if not future.done(): future.set_exception(exc)
        finally:
            for task in relays: task.cancel()
    async def heartbeat():
        while proc.returncode is None:
            await send({'lease': True}); await asyncio.sleep(5)
    async def stderr():
        tail = bytearray()
        while chunk := await proc.stderr.read(4096):
            tail.extend(chunk); del tail[:-8192]
        if tail:
            from .engines import _redact
            path = broker.directory('runtime', 'broker') / ('desktop-' + bot_id + '.log')
            path.write_text(_redact(tail.decode(errors='replace')))
    entry['reader'] = asyncio.create_task(read()); entry['heartbeat'] = asyncio.create_task(heartbeat())
    entry['stderr'] = asyncio.create_task(stderr())
    try: await asyncio.wait_for(asyncio.shield(entry['ready']), 50)
    except BaseException:
        await stop_desktop(broker, bot_id); raise
    return entry


def publish_software(broker, bot_id, args):
    from .account_disk import checked, bot_home
    if set(args) != {'name', 'version', 'path'} or not all(isinstance(v, str) for v in args.values()):
        raise ValueError('software_publish_fields_denied')
    if not all(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}', args[k]) for k in ('name', 'version')) or args['name'] == 'chrome':
        raise ValueError('software_identity_denied')
    home = bot_home(broker.home, bot_id)
    parts = args['path'].split('/')
    if not parts or any(not p or p in {'.', '..'} for p in parts): raise ValueError('software_source_denied')
    apps = Path(checked(broker.home)['mount']) / 'apps'; parent = apps / args['name']
    parent.mkdir(mode=0o755, exist_ok=True)
    target = parent / args['version']
    if target.exists(): raise ValueError('software_version_exists')
    pending = parent / ('.pending-' + secrets.token_hex(8)); pending.mkdir(mode=0o755)
    files = {}; total = 0; entries = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    source_fd = os.open(home, directory_flags)
    def copy_tree(fd, relative=Path(), depth=0):
        nonlocal total, entries
        if depth > 32: raise ValueError('software_tree_depth_limit')
        for name in os.listdir(fd):
            entries += 1
            if entries > 10000: raise ValueError('software_bundle_limit')
            info = os.stat(name, dir_fd=fd, follow_symlinks=False); rel = relative / name
            destination = pending / rel
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, directory_flags, dir_fd=fd)
                try:
                    destination.mkdir(mode=0o755); copy_tree(child, rel, depth + 1)
                finally: os.close(child)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise ValueError('software_special_file_denied')
            total += info.st_size
            if total > 512 * 1024 * 1024 or len(files) >= 10000: raise ValueError('software_bundle_limit')
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, 'rb') as inp, destination.open('xb') as out:
                before = os.fstat(inp.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1: raise ValueError('software_file_changed')
                digest = hashlib.sha256(); count = 0
                while chunk := inp.read(1024 * 1024):
                    count += len(chunk)
                    if count > info.st_size: raise ValueError('software_file_changed')
                    out.write(chunk); digest.update(chunk)
                after = os.fstat(inp.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or count != info.st_size:
                    raise ValueError('software_file_changed')
            destination.chmod(0o555 if info.st_mode & 0o111 else 0o444)
            files[str(rel)] = digest.hexdigest()
    try:
        for part in parts:
            child = os.open(part, directory_flags, dir_fd=source_fd); os.close(source_fd); source_fd = child
        copy_tree(source_fd)
        if not files: raise ValueError('software_empty_bundle')
        receipt = {'name': args['name'], 'version': args['version'], 'bot_id': bot_id,
                   'bytes': total, 'files': files, 'published_at': time.time()}
        (pending / '.carme-software.json').write_text(json.dumps(receipt))
        pending.rename(target)
        return {'published': True, 'path': '/software/' + args['name'] + '/' + args['version'], 'bytes': total,
                'sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
    finally:
        os.close(source_fd)
        if pending.exists(): shutil.rmtree(pending)


async def download_software(broker, request, entry, safety):
    from .account_disk import bot_home
    from .docker_browser import fetch_public
    from urllib.parse import urljoin
    args = request['arguments']
    if set(args) != {'url', 'name'} or not isinstance(args['name'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', args['name']):
        raise ValueError('desktop_download_fields_denied')
    home = bot_home(broker.home, request['bot_id'])
    root_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    directory_fd = None; temporary = '.download-' + secrets.token_hex(12)
    digest = hashlib.sha256(); total = 0
    def check():
        if time.time() >= request['deadline']: raise RuntimeError('desktop_download_expired')
    async def monitor():
        while True:
            await asyncio.sleep(1)
            permit = await broker.call({'op': 'desktop_check', 'id': request['id'], 'session_id': entry['session_id']})
            if not permit.get('active'): raise RuntimeError('desktop_download_revoked')
    async def transfer(out):
        nonlocal total
        url = args['url']
        def write(chunk):
            nonlocal total
            out.write(chunk); digest.update(chunk); total += len(chunk)
        for _ in range(6):
            result = await fetch_public({'url': url, 'method': 'GET', 'headers': {}, 'body': ''}, safety, check,
                                        sink=write, max_bytes=512 * 1024 * 1024)
            if result['status'] in {301, 302, 303, 307, 308}:
                headers = {k.lower(): v for k, v in result['headers']}
                url = urljoin(url, headers['location']); continue
            if result['status'] != 200:
                raise DesktopNoEffectError('desktop_download_http_' + str(result['status']), status_code=result['status'])
            return
        raise ValueError('desktop_download_redirect_limit')
    tasks = []; published = False
    try:
        with contextlib.suppress(FileExistsError): os.mkdir('Downloads', 0o777, dir_fd=root_fd)
        directory_fd = os.open('Downloads', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=directory_fd)
        with os.fdopen(fd, 'wb') as out:
            tasks = [asyncio.create_task(transfer(out)), asyncio.create_task(monitor())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
            check(); out.flush(); os.fsync(out.fileno())
        # Exclusive link publication refuses an existing name and never follows it.
        os.link(temporary, args['name'], src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        published = True
        return {'path': '/home/bot/Downloads/' + args['name'], 'bytes': total, 'sha256': digest.hexdigest()}
    except Exception as exc:
        if published:
            raise
        # The only persistent destination is the exclusive link above. Cancellation is a
        # BaseException and deliberately remains uncertain across the process boundary.
        raise DesktopNoEffectError(str(exc), status_code=exc.status_code if isinstance(exc, DesktopNoEffectError) else None) from exc
    finally:
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        if directory_fd is not None:
            with contextlib.suppress(FileNotFoundError): os.unlink(temporary, dir_fd=directory_fd)
            os.close(directory_fd)
        os.close(root_fd)


async def broker_desktop_request(broker, request):
    bot_id, op, args = request['bot_id'], request['operation'], request['arguments']; validate(op, args)
    if time.time() > request['deadline']: raise RuntimeError('desktop_request_expired')
    entry = await start_desktop(broker, bot_id)
    async with (contextlib.nullcontext() if op in {'status', 'screenshot'} else entry['lock']):
        # Recheck after startup/queueing; toggling control invalidates queued input.
        permit = await broker.call({'op': 'desktop_check', 'id': request['id'], 'session_id': entry['session_id']})
        if not permit.get('active'): raise RuntimeError('desktop_request_revoked')
        if op == 'publish': return await asyncio.to_thread(publish_software, broker, bot_id, args)
        if op == 'fetch': return await download_software(broker, request, entry, permit['safety'])
        identity = secrets.token_hex(16); future = asyncio.get_running_loop().create_future()
        entry['pending'][identity] = future; entry['last_used'] = time.monotonic()
        try:
            await entry['send']({'id': identity, 'op': op, 'arguments': args})
            while not future.done():
                await asyncio.wait({future}, timeout=1)
                permit = await broker.call({'op': 'desktop_check', 'id': request['id'], 'session_id': entry['session_id']})
                if time.time() >= permit.get('deadline', request['deadline']) or not permit.get('active'):
                    raise RuntimeError('desktop_request_revoked')
            return future.result()
        except BaseException:
            await stop_desktop(broker, bot_id)
            if future.done() and not future.cancelled(): future.exception()
            raise
        finally: entry['pending'].pop(identity, None)


async def worker():
    from .approval import ApprovalOutcome
    from .browser import BrowserManager
    from .tools.base import ToolContext
    from .tools.browser import FetchPageTool, WebSearchTool
    from .tools.web import WEB_TOOLS
    from .docker_browser import WEB_TOOLS as WEB_NAMES

    home = Path('/home/bot'); runtime = Path('/runtime')
    for path in (home / 'Downloads', home / 'Desktop', home / '.config', runtime / 'screenshots', runtime / 'xdg'):
        path.mkdir(parents=True, exist_ok=True)
    os.environ.update(XDG_RUNTIME_DIR='/runtime/xdg', TMPDIR='/runtime',
        PI_CODING_AGENT_DIR='/runtime/pi', PI_COMPUTER_USE_CURSOR_OVERLAY='false')
    loop = asyncio.get_running_loop(); reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    pending = {}; commands = set(); command_lock = asyncio.Lock(); last_lease = time.monotonic(); last_input = time.monotonic(); held = False
    def emit(value): print(json.dumps(value, ensure_ascii=False), flush=True)
    async def rpc(kind, payload):
        identity = secrets.token_hex(16); future = loop.create_future(); pending[identity] = future
        emit({'rpc': kind, 'id': identity, 'payload': payload})
        try: return await asyncio.wait_for(future, 80)
        finally: pending.pop(identity, None)
    async def spawn(*args):
        return await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=sys.stderr)
    dbus = (await run_program('dbus-daemon', '--session', '--fork', '--print-address=1')).decode().strip()
    os.environ['DBUS_SESSION_BUS_ADDRESS'] = dbus
    await spawn('Xvfb', ':99', '-screen', '0', '1280x800x24', '-nolisten', 'tcp')
    for _ in range(60):
        try: await run_program('xdotool', 'getdisplaygeometry', timeout=2); break
        except Exception: await asyncio.sleep(.1)
    else: raise RuntimeError('desktop_display_unavailable')
    await spawn('openbox'); await spawn('tint2'); await spawn('pcmanfm', '--desktop')
    # Each user's helper lives in a fresh private runtime; its executable is pinned
    # in the image, not loaded from a user-writable HOME or package directory.
    helper_dir = Path('/runtime/pi/helpers/pi-computer-use'); helper_dir.mkdir(parents=True)
    native = list(Path('/opt/desktop/node_modules/@injaneity/pi-computer-use/prebuilt/linux').rglob('*arm64*'))
    native = next((p for p in native if p.is_file() and 'bridge' in p.name), None)
    if native is None:
        native = next((p for p in Path('/opt/desktop/node_modules/@injaneity/pi-computer-use/prebuilt/linux').rglob('linux-bridge') if 'arm64' in str(p)), None)
    if native is None: raise RuntimeError('pinned_linux_helper_missing')
    (helper_dir / 'linux-bridge').symlink_to(native)
    manager = BrowserManager({'enabled': True, 'headless': False, 'default_profile': 'default',
        # Outbound submissions are approved at Control, including native UI/shell clicks.
        'safety': {'idle_close_seconds': 86400, 'require_confirmation': False}, 'screenshots': {'dir': '/runtime/screenshots'},
        'profiles': {'default': {'user_data_dir': '/home/bot/browser/default', 'headless': False,
            'desktop_runtime': True, 'viewport': {'width': 1280, 'height': 720}, 'docker_relay': rpc}}}, home)
    await manager._get_session('default')
    node = await asyncio.create_subprocess_exec('/usr/local/bin/node', '--import', '/opt/desktop/node_modules/tsx/dist/loader.mjs',
        '/opt/desktop/computer-use.ts', stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=sys.stderr,
        limit=8 * 1024 * 1024)
    web_tools = {t.name: t for t in [*WEB_TOOLS, FetchPageTool(), WebSearchTool()]}
    async def approve(**kwargs): return ApprovalOutcome(**await rpc('approve', kwargs))
    async def request_url(url):
        import httpx
        for _ in range(6):
            result = await rpc('browser_fetch', {'url': url, 'method': 'GET', 'headers': {}, 'body': ''})
            response = httpx.Response(result['status'], headers=result['headers'], content=base64.b64decode(result['body']), request=httpx.Request('GET', url))
            if response.is_redirect:
                url = str(response.url.join(response.headers['location'])); continue
            response.raise_for_status(); return response
        raise ValueError('browser_redirect_limit')
    ctx = ToolContext(agent=SimpleNamespace(id='desktop', tools=list(WEB_NAMES)), task_id='desktop', store=None,
        browser_manager=manager, approve=approve, extras={'http_request': request_url, 'docker_browser': True})

    async def cursor():
        raw = (await run_program('xdotool', 'getmouselocation', '--shell')).decode()
        values = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
        return {'x': int(values['X']), 'y': int(values['Y'])}

    async def operate(op, args):
        nonlocal held, last_input
        if op == 'status':
            disk = os.statvfs(home); point = await cursor()
            return {'enabled': True, 'available': True, 'mode': 'docker', 'screen_width': 1280, 'screen_height': 800,
                    'cursor_x': point['x'], 'cursor_y': point['y'], 'free_bytes': disk.f_bavail * disk.f_frsize,
                    'usable_before_chrome': 2 * 1024 ** 3}
        if op == 'screenshot':
            raw = await run_program('import', '-window', 'root', '-quality', '75', 'jpeg:-')
            return {'body': base64.b64encode(raw).decode(), 'mime': 'image/jpeg', 'at': time.time(),
                    'state': await operate('status', {})}
        if op == 'release':
            held = False
            await run_program('xdotool', 'mouseup', '1', 'mouseup', '2', 'mouseup', '3')
            return {'ok': True}
        if op == 'mouse':
            last_input = time.monotonic()
            if args['action'] == 'press': held = True
            elif args['action'] == 'release': held = False
            action = args['action']; button = {'left': '1', 'middle': '2', 'right': '3'}[args.get('button', 'left')]
            if action == 'move_rel':
                point = await cursor(); x = point['x'] + (args.get('dx') or 0); y = point['y'] + (args.get('dy') or 0)
                await run_program('xdotool', 'mousemove', str(round(max(0, min(1279, x)))), str(round(max(0, min(799, y)))))
            else:
                if args.get('x') is not None and args.get('y') is not None:
                    await run_program('xdotool', 'mousemove', str(round(max(0, min(1279, args['x'])))), str(round(max(0, min(799, args['y'])))))
                if action == 'click': await run_program('xdotool', 'click', '--repeat', str(args.get('clicks', 1)), button)
                elif action in {'press', 'release'}: await run_program('xdotool', 'mousedown' if action == 'press' else 'mouseup', button)
                elif action == 'scroll': await run_program('xdotool', 'click', '--repeat', str(min(20, max(1, round(abs(args.get('delta_y') or 1) / 80)))), '4' if (args.get('delta_y') or 0) > 0 else '5')
            return {'ok': True, **await cursor()}
        if op == 'keyboard':
            if args.get('text'):
                await run_program('xdotool', 'type', '--clearmodifiers', '--delay', '12', '--', args['text'], timeout=60)
            else:
                aliases = {'cmd': 'ctrl', 'control': 'ctrl', 'option': 'alt', 'enter': 'Return', 'return': 'Return', 'backspace': 'BackSpace',
                           'escape': 'Escape', 'tab': 'Tab', 'space': 'space', 'delete': 'BackSpace', 'forwarddelete': 'Delete', 'pageup': 'Prior', 'pagedown': 'Next',
                           'home': 'Home', 'end': 'End', 'left': 'Left', 'right': 'Right', 'up': 'Up', 'down': 'Down'}
                keys = args['keys'].split('+')
                if len(keys) > 5 or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,16}', k) for k in keys): raise ValueError('desktop_keys_denied')
                await run_program('xdotool', 'key', '--clearmodifiers', '+'.join(aliases.get(k.lower(), k) for k in keys))
            return {'ok': True}
        if op == 'shell':
            raw = await run_program('/bin/sh', '-c', 'cd /home/bot && ' + args['command'], timeout=60, limit=65536)
            return {'text': raw.decode(errors='replace')}
        if op == 'software_list':
            return {'software': sorted(str(p.relative_to('/software')) for p in Path('/software').glob('*/*') if p.is_dir())}
        if op == 'fetch':
            if set(args) != {'url', 'name'} or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', args['name']):
                raise ValueError('desktop_download_fields_denied')
            result = await rpc('download', args)
            raw = base64.b64decode(result['body'], validate=True)
            path = home / 'Downloads' / args['name']
            if path.exists() or path.is_symlink(): raise ValueError('download_destination_exists')
            path.write_bytes(raw)
            return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        if op == 'web':
            name = args.get('name'); arguments = args.get('arguments', {})
            if name not in WEB_NAMES or arguments.get('profile', 'default') not in {'', 'default'}:
                raise ValueError('desktop_browser_scope_denied')
            return {'text': await web_tools[name].run(ctx, **arguments)}
        if op in UI_TOOLS | {'help'}:
            identity = secrets.token_hex(16)
            node.stdin.write((json.dumps({'id': identity, 'name': op, 'arguments': args}) + '\n').encode()); await node.stdin.drain()
            reply = json.loads(await asyncio.wait_for(node.stdout.readline(), 650))
            if reply.get('id') != identity: raise RuntimeError('computer_use_protocol_mismatch')
            if reply.get('error'): raise RuntimeError(reply['error'])
            return reply['result']
        raise ValueError('desktop_operation_denied')

    async def dispatch(request):
        try:
            op, args = request['op'], validate(request['op'], request.get('arguments', {}))
            async with (contextlib.nullcontext() if op in {'status', 'screenshot'} else command_lock):
                result = await operate(op, args)
                files = []
                for path in ([] if op in READ_OPS else Path('/runtime/screenshots').glob('*.png')):
                    raw = path.read_bytes()
                    if len(raw) <= MAX_FRAME: files.append({'name': path.name, 'body': base64.b64encode(raw).decode()})
                    path.unlink()
                if files: result['files'] = files
                emit({'id': request['id'], 'result': result})
        except Exception as exc: emit({'id': request['id'], 'error': str(exc)[:2000]})

    async def watch():
        nonlocal held
        while True:
            await asyncio.sleep(2)
            if held and time.monotonic() - last_input > 5:
                await operate('release', {})
            if time.monotonic() - last_lease > 30: os._exit(70)
    watcher = asyncio.create_task(watch()); emit({'ready': True})
    try:
        while line := await reader.readline():
            message = json.loads(line)
            if message == {'shutdown': True}: break
            elif message == {'lease': True}: last_lease = time.monotonic()
            elif message.get('id') in pending:
                future = pending[message['id']]
                if not future.done():
                    if 'error' in message: future.set_exception(RuntimeError(message['error']))
                    else: future.set_result(message['result'])
            elif set(message) == {'id', 'op', 'arguments'}:
                task = asyncio.create_task(dispatch(message)); commands.add(task); task.add_done_callback(commands.discard)
            else: raise ValueError('desktop_worker_protocol_denied')
    finally:
        watcher.cancel()
        for task in commands: task.cancel()
        await asyncio.gather(*commands, return_exceptions=True)
        await manager.close_all(); transport.close()


if __name__ == '__main__' and sys.argv[1:] == ['worker']:
    asyncio.run(worker())
