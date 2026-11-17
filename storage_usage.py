"""Directory usage summaries, shared by authenticated Atlas sessions."""
import copy
import os
import pwd
import re
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

MOUNTS = ('home', 'workspace', 'data')
TTL = 900
QUOTA = 30 * 1024**3
_lock = threading.Lock()
_cache = {}


def users():
    names = set()
    for mount in MOUNTS:
        try:
            with os.scandir('/' + mount) as entries:
                for entry in entries:
                    try:
                        account = pwd.getpwnam(entry.name)
                        if account.pw_uid >= 1000 and account.pw_uid != 65534:
                            names.add(account.pw_name)
                    except KeyError:
                        continue
        except OSError:
            continue
    return sorted(names)


def mount_supported(mount):
    with open('/proc/self/mountinfo') as stream:
        for line in stream:
            location, filesystem = line.rstrip().split(' - ', 1)
            if location.split()[4] != '/' + mount:
                continue
            kind, source = filesystem.split()[:2]
            if mount == 'data':
                return kind == 'fuse.juicefs'
            return kind == 'nfs4' and source == '1.1.1.253:/' + mount
    return False


def measure(mount, user):
    path = Path('/') / mount / user
    try:
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            return dict(status='unsupported', bytes=None)
        if mount == 'data':
            result = subprocess.run(['juicefs', 'info', '-r', str(path)],
                                    capture_output=True, text=True, timeout=25)
            if result.returncode:
                denied = re.search(r'permission denied|operation not permitted', result.stderr, re.I)
                return dict(status='permission' if denied else 'error', bytes=None)
            length = re.search(r'^\s*length:\s*(?:.*\()?([0-9]+) Bytes\)?\s*$', result.stdout, re.M)
            if not length:
                return dict(status='error', bytes=None)
            size = int(length.group(1))
        else:
            # These verified NFS exports expose recursive logical bytes in st_size.
            size = metadata.st_size
        return dict(status='ok', bytes=size, stamp=int(time.time()))
    except FileNotFoundError:
        return dict(status='absent', bytes=None)
    except PermissionError:
        return dict(status='permission', bytes=None)
    except subprocess.TimeoutExpired:
        return dict(status='timeout', bytes=None)
    except OSError:
        return dict(status='error', bytes=None)


def collect(mount):
    try:
        names = users()
        if not names:
            raise OSError('User directories unavailable')
        supported = mount_supported(mount)
        with _lock:
            snapshot = _cache[mount]
            previous = {row['user']: row for row in snapshot['rows']}
            snapshot['rows'] = [dict(previous.get(name, dict(user=name, bytes=None, stamp=None)),
                                     status='pending') for name in names]
            rows = {row['user']: row for row in snapshot['rows']}
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = {pool.submit(measure, mount, name): name for name in names} if supported else {}
            if not supported:
                with _lock:
                    for row in rows.values():
                        row.update(status='unsupported', bytes=None, stamp=None)
            for future in as_completed(pending):
                value = future.result()
                with _lock:
                    row = rows[pending[future]]
                    if value['status'] in ('ok', 'absent', 'unsupported'):
                        row.update(bytes=value['bytes'], stamp=value.get('stamp'))
                    row.update(status=value['status'], attempted=int(time.time()))
        with _lock:
            snapshot['stamp'] = int(time.time())
            snapshot['error'] = ''
    except Exception:
        with _lock:
            _cache[mount]['error'] = '目录统计暂时不可用'
            for row in _cache[mount]['rows']:
                if row['status'] == 'pending':
                    row['status'] = 'error'
    finally:
        with _lock:
            _cache[mount]['refreshing'] = False
            _cache[mount]['next'] = time.monotonic() + TTL


def snapshot(mount='home'):
    if mount not in MOUNTS:
        raise ValueError('Unsupported storage mount')
    with _lock:
        data = _cache.setdefault(mount, dict(mount=mount, rows=[], stamp=None,
                                            refreshing=False, next=0, error=''))
        if not data['refreshing'] and time.monotonic() >= data['next']:
            data['refreshing'] = True
            threading.Thread(target=collect, args=(mount,), daemon=True).start()
        result = copy.deepcopy(data)
    result.pop('next')
    result.update(quota=QUOTA if mount == 'home' else None, approximate=mount == 'data', interval=TTL)
    return result
