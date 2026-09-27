"""Account-owned, capacity-limited macOS disk images used by Linux workers.

Only the lifecycle CLI mounts images. A missing mount never becomes an ordinary
unlimited directory. No personal files, credentials, or global Docker settings.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

GIB = 1024 ** 3
CHROME_VERSION = '153.0.8010.52'
CHROME_PLATFORM = 'linux-arm64'
CHROME_SHA256 = '794441f3254273eb30d710b3eaab3cd1c7bbf1c88a40275470f6cee350881ce5'
CHROME_URL = f'https://storage.googleapis.com/chrome-for-testing-public/{CHROME_VERSION}/{CHROME_PLATFORM}/chrome-{CHROME_PLATFORM}.zip'


def save(path, value):
    temporary = path.with_suffix('.pending')
    with temporary.open('w') as out:
        os.chmod(temporary, 0o600)
        json.dump(value, out, indent=2)
        out.flush(); os.fsync(out.fileno())
    os.replace(temporary, path)


def command(*args):
    result = subprocess.run(args, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError('account_disk_command_failed:' + Path(args[0]).name + ':' + result.stderr.decode(errors='replace')[:240])
    return result.stdout


def mounted(image, mount):
    info = plistlib.loads(command('/usr/bin/hdiutil', 'info', '-plist'))
    for entry in info.get('images', []):
        if Path(entry.get('image-path', '/')) == image:
            return any(e.get('mount-point') == str(mount) for e in entry.get('system-entities', []))
    return False


def checked(account_home, *, require_mount=True):
    base = Path(account_home)
    manifest = base / 'storage/disk.json'
    account = json.loads((base / 'account.json').read_text())
    value = json.loads(manifest.read_text())
    mount = base / 'storage/mount'
    image = base / 'storage/account.sparseimage'
    if (base.resolve() != base or manifest.is_symlink() or image.is_symlink() or mount.is_symlink()
            or value.get('instance_id') != account['instance_id']
            or value.get('mount') != str(mount) or value.get('image') != str(image)
            or value.get('usable_before_chrome') != 2 * GIB):
        raise ValueError('account_disk_identity_mismatch')
    if require_mount and (not os.path.ismount(mount) or not mounted(image, mount)):
        raise RuntimeError('account_disk_not_mounted: refusing unlimited directory fallback')
    return value


def status(account_home):
    value = checked(account_home)
    disk = os.statvfs(value['mount'])
    return {'usable_before_chrome': value['usable_before_chrome'],
            'free_bytes': disk.f_bavail * disk.f_frsize, 'filesystem_bytes': disk.f_blocks * disk.f_frsize,
            'chrome_version': value.get('chrome_version'), 'chrome_bytes': value.get('chrome_bytes', 0),
            'quota': 'fixed-size-disk-image', 'mounted': True}


def ensure(account_home):
    if sys.platform != 'darwin':
        raise RuntimeError('account_disk_platform_unsupported: hard quota needs an explicitly supported filesystem')
    base = Path(account_home); account = json.loads((base / 'account.json').read_text())
    if base.resolve() != base or account['home'] != str(base):
        raise ValueError('account_home_mismatch')
    root = base / 'storage'; root.mkdir(mode=0o700, exist_ok=True)
    image = root / 'account.sparseimage'; mount = root / 'mount'; manifest = root / 'disk.json'
    if root.is_symlink(): raise ValueError('account_disk_symlink_denied')
    if not manifest.exists():
        if image.exists() or mount.exists():
            raise RuntimeError('unregistered_account_disk: inspect interrupted initialization first')
        save(manifest, {'version': 1, 'instance_id': account['instance_id'], 'image': str(image),
                        'mount': str(mount), 'usable_before_chrome': 2 * GIB, 'initialized': False})
    value = checked(base, require_mount=False)
    if not image.exists():
        command('/usr/bin/hdiutil', 'create', '-size', '2112m', '-type', 'SPARSE', '-layout', 'NONE', '-fs',
                'Case-sensitive HFS+', '-volname', 'Carme-' + account['id'], '-nospotlight', str(image))
        image.chmod(0o600)
    mount.mkdir(mode=0o700, exist_ok=True)
    if not mounted(image, mount):
        if os.path.ismount(mount) or any(mount.iterdir()):
            raise RuntimeError('account_disk_mountpoint_not_empty')
        command('/usr/bin/hdiutil', 'attach', '-nobrowse', '-noautoopen', '-mountpoint', str(mount), str(image))
    checked(base)
    if not value.get('initialized'):
        for name in ('apps', 'bots', 'downloads'):
            (mount / name).mkdir(mode=0o755, exist_ok=True)
        # Filesystem metadata is outside the promised usable budget. This reserve
        # is never mounted into a worker and cannot be removed by an account bot.
        reserve = mount / '.capacity-reserve'
        reserve.unlink(missing_ok=True)
        reserve.touch(mode=0o600)
        available = shutil.disk_usage(mount).free
        if available < 2 * GIB: raise RuntimeError('account_disk_too_small')
        with reserve.open('wb') as out:
            remaining = available - 2 * GIB
            while remaining:
                size = min(remaining, 1024 * 1024); out.write(bytes(size)); remaining -= size
            out.flush(); os.fsync(out.fileno())
        # Creating/growing the reserve can allocate another HFS catalog block.
        # Remove that overhead from the reserve, not the promised usable space.
        for _ in range(3):
            difference = shutil.disk_usage(mount).free - 2 * GIB
            if not difference: break
            size = reserve.stat().st_size + difference
            if size < 0: raise RuntimeError('account_disk_capacity_unavailable')
            with reserve.open('r+b') as out:
                out.truncate(size); out.flush(); os.fsync(out.fileno())
        if shutil.disk_usage(mount).free != 2 * GIB: raise RuntimeError('account_disk_capacity_mismatch')
        reserve.chmod(0o400)
        value.update(initialized=True, measured_before_chrome=shutil.disk_usage(mount).free)
        save(manifest, value)
    return value


def install_chrome(account_home):
    value = ensure(account_home); root = Path(value['mount']); destination = root / 'apps/chrome'
    if value.get('chrome_version'):
        if value['chrome_version'] != CHROME_VERSION or not (destination / 'chrome').is_file():
            raise RuntimeError('registered_chrome_missing_or_changed')
        return status(account_home)
    archive = root / 'downloads/chrome.zip'; pending = root / 'apps/chrome.pending'
    if pending.exists(): shutil.rmtree(pending)
    pending.mkdir(mode=0o755)
    digest = hashlib.sha256(); downloaded = 0
    with urllib.request.urlopen(CHROME_URL, timeout=60) as response, archive.open('wb') as out:
        while chunk := response.read(1024 * 1024):
            downloaded += len(chunk)
            if downloaded > 350 * 1024 * 1024: raise ValueError('chrome_download_limit')
            digest.update(chunk); out.write(chunk)
    if digest.hexdigest() != CHROME_SHA256: raise ValueError('chrome_download_hash_mismatch')
    total = 0
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            path = PurePosixPath(entry.filename)
            if path.parts[0] != 'chrome-' + CHROME_PLATFORM or path.is_absolute() or '..' in path.parts:
                raise ValueError('chrome_archive_path_denied')
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode): raise ValueError('chrome_archive_link_denied')
            target = pending.joinpath(*path.parts[1:]); total += entry.file_size
            if total > GIB: raise ValueError('chrome_archive_size_limit')
            if entry.is_dir(): target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open('xb') as out: shutil.copyfileobj(source, out)
                target.chmod(0o755 if mode & 0o111 else 0o644)
    if destination.exists(): raise RuntimeError('chrome_destination_conflict')
    pending.rename(destination); archive.unlink()
    value.update(chrome_version=CHROME_VERSION, chrome_url=CHROME_URL,
                 chrome_archive_sha256=digest.hexdigest(), chrome_bytes=total)
    save(Path(account_home) / 'storage/disk.json', value)
    return status(account_home)


def bot_home(account_home, bot_id):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', bot_id): raise ValueError('invalid_bot_id')
    value = checked(account_home); path = Path(value['mount']) / 'bots' / bot_id
    if path.is_symlink(): raise ValueError('bot_disk_symlink_denied')
    path.mkdir(mode=0o777, exist_ok=True); path.chmod(0o777)
    if path.resolve() != path: raise ValueError('bot_disk_escape_denied')
    return path


def migrate_browser_profiles(account_home):
    """Copy only this stopped account's profiles; retain originals for rollback."""
    base = Path(account_home); value = checked(base); root = Path(value['mount'])
    receipt = base / 'storage/browser-migration.json'
    if receipt.exists(): return json.loads(receipt.read_text())
    legacy = base / 'runtime/browser'; copied = []
    if legacy.exists():
        for source in sorted(legacy.iterdir()):
            if not source.is_dir() or source.is_symlink() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', source.name):
                raise ValueError('browser_migration_source_denied')
            destination = bot_home(base, source.name) / 'browser'
            if destination.exists(): raise RuntimeError('browser_migration_destination_exists')
            total = 0
            for path in source.rglob('*'):
                # Stale Chrome singleton links are process locks, not profile data.
                if path.name in {'SingletonLock', 'SingletonCookie', 'SingletonSocket', '.carme-lock'}: continue
                if path.is_symlink(): raise ValueError('browser_migration_symlink_denied')
                if path.is_file(): total += path.stat().st_size
            if total + 32 * 1024 * 1024 > shutil.disk_usage(root).free: raise RuntimeError('browser_migration_disk_full')
            pending = destination.with_name('browser.migrating')
            if pending.exists(): shutil.rmtree(pending)
            shutil.copytree(source, pending, ignore=shutil.ignore_patterns('SingletonLock','SingletonCookie','SingletonSocket','.carme-lock'))
            for path in pending.rglob('*'): path.chmod(0o777 if path.is_dir() else 0o666)
            pending.chmod(0o777); pending.rename(destination)
            copied.append({'bot_id': source.name, 'bytes': total})
    result = {'version': 1, 'instance_id': value['instance_id'], 'profiles': copied, 'originals_retained': True}
    save(receipt, result); return result
