"""Directory usage summaries, shared by authenticated Atlas sessions."""
import copy
import json
import os
import pwd
import re
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

MOUNTS = ('home', 'workspace', 'data', 'scratch')
TTL = 900
STORAGE_API = os.environ.get('ATLAS_STORAGE_API_URL', 'http://127.0.0.1:8919').rstrip('/')
API_INTERVAL = 60
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
            if mount == 'scratch':
                return kind == 'xfs'
            return kind == 'nfs4' and source == '1.1.1.253:/' + mount
    return False


def measure(mount, user):
    path = Path('/') / mount / user
    return measure_path(mount, path)


def measure_path(mount, path, timeout=25):
    try:
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            return dict(status='unsupported', bytes=None)
        if mount == 'data':
            result = subprocess.run(['juicefs', 'info', '-r', str(path)],
                                    capture_output=True, text=True, timeout=timeout)
            if result.returncode:
                denied = re.search(r'permission denied|operation not permitted', result.stderr, re.I)
                return dict(status='permission' if denied else 'error', bytes=None)
            length = re.search(r'^\s*length:\s*(?:.*\()?([0-9]+) Bytes\)?\s*$', result.stdout, re.M)
            if not length:
                return dict(status='error', bytes=None)
            size = int(length.group(1))
        elif mount == 'scratch':
            result = subprocess.run(['nice', '-n', '15', 'du', '-sx', '--apparent-size', '-B1', '--', str(path)],
                                    capture_output=True, text=True, timeout=timeout)
            if result.returncode:
                return dict(status='permission' if 'Permission denied' in result.stderr else 'error', bytes=None)
            size = int(result.stdout.split()[0])
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
    except (OSError, ValueError):
        return dict(status='error', bytes=None)


def api_read(endpoint, **params):
    url = STORAGE_API + '/v1/' + endpoint + '?' + urlencode(params)
    with urlopen(url, timeout=130) as response:
        return json.load(response)


def api_ranking(mount):
    root = '/' + mount
    reading = api_read('usage', path=root)
    # JuiceFS can have readings for its children without a quota on the root.
    if reading['path'] != root or reading['status'] not in ('ok', 'unmeasured', 'partial'):
        raise ValueError('Root unavailable')
    rows = []
    seen = set()
    offset = 0
    total = None
    while True:
        page = api_read('children', path=root, limit=1000, offset=offset)
        if page['path'] != root or page['offset'] != offset or total not in (None, page['total']):
            raise ValueError('Directory list changed')
        total = page['total']
        for item in page['items']:
            path = Path(item['path'])
            if str(path.parent) != root or path in seen:
                raise ValueError('Inconsistent directory list')
            seen.add(path)
            try:
                account = pwd.getpwnam(path.name)
            except KeyError:
                continue
            if account.pw_uid < 1000 or account.pw_uid == 65534 or account.pw_name != path.name:
                continue
            status = item['status']
            if status == 'missing':
                continue
            size = item['bytes'] if status == 'ok' else None
            if status == 'ok' and (type(size) is not int or size < 0):
                raise ValueError('Invalid directory size')
            quota = item['quota_bytes'] if item['quota_mode'] == 'limited' else None
            stamp = int(datetime.fromisoformat(item['observed_at']).timestamp())
            rows.append(dict(user=path.name, bytes=size, stamp=stamp, quota=quota,
                             status='stale' if status == 'ok' and not item['cached'] else status))
        offset += len(page['items'])
        if not page['truncated']:
            if offset != total:
                raise ValueError('Incomplete directory list')
            break
        if not page['items'] or offset >= total:
            raise ValueError('Invalid directory page')
    warning = '目录列表可能不完整' if reading['status'] == 'partial' else ''
    return dict(rows=rows, stamp=int(datetime.fromisoformat(reading['observed_at']).timestamp()), error=warning)


def collect_api(mount):
    try:
        result = api_ranking(mount)
    except Exception:
        result = dict(error='存储接口读取失败，稍后自动重试')
    with _lock:
        _cache[mount].update(result, refreshing=False, next=time.monotonic() + API_INTERVAL)


def collect_local(mount):
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
            collector = collect_local if mount == 'scratch' else collect_api
            threading.Thread(target=collector, args=(mount,), daemon=True).start()
        result = copy.deepcopy(data)
    result.pop('next')
    result.update(interval=TTL if mount == 'scratch' else API_INTERVAL,
                  node=os.uname().nodename if mount == 'scratch' else None)
    return result


_details = {}
_detail_slots = threading.Semaphore(2)


def category(path, names=None):
    name = path.name.lower()
    if names is not None:
        if re.fullmatch(r'checkpoint[-_]?[0-9]+', name) or name in ('checkpoints', 'ckpts', 'weights', 'models') or names & {'model.safetensors', 'pytorch_model.bin', 'model.safetensors.index.json', 'trainer_state.json'}:
            return 'checkpoints'
        if name in ('dataset', 'datasets', 'raw_data') or names & {'dataset_info.json', 'dataset_dict.json'} or ('meta' in names and (path / 'meta' / 'info.json').is_file()):
            return 'datasets'
        if name in ('wandb', 'swanlog', 'logs', 'tensorboard', 'tb_logs'):
            return 'logs'
        if name in ('venv', '.venv', 'envs', 'miniconda3', 'anaconda3', '.conda') or names & {'pyvenv.cfg', 'conda-meta'}:
            return 'environments'
        if name in ('.cache', '__pycache__', 'node_modules', '.git'):
            return 'cache'
        return None
    if path.suffix.lower() in ('.safetensors', '.pt', '.pth', '.ckpt'):
        return 'checkpoints'
    if path.suffix.lower() in ('.parquet', '.hdf5', '.h5', '.tfrecord'):
        return 'datasets'
    if path.suffix.lower() in ('.log', '.out', '.err') or name.startswith('events.out.tfevents'):
        return 'logs'
    if path.suffix.lower() in ('.py', '.sh', '.ipynb', '.js', '.ts', '.cpp', '.c', '.yaml', '.yml', '.toml', '.md'):
        return 'code'
    return 'other'


def collect_detail(mount, user):
    key = (mount, user)
    with _detail_slots:
        root = Path('/') / mount / user
        deadline = time.monotonic() + 60
        stack = deque([(root, 0)])
        visited = 0
        partial = False
        try:
            if not mount_supported(mount):
                raise OSError('Unsupported filesystem')
            while stack and visited < 5000 and time.monotonic() < deadline:
                path, depth = stack.popleft()
                visited += 1
                try:
                    metadata = path.lstat()
                    if stat.S_ISLNK(metadata.st_mode):
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        entries = []
                        with os.scandir(path) as stream:
                            for entry in stream:
                                entries.append(entry.name)
                                if len(entries) > 2000:
                                    break
                        kind = category(path, set(entries))
                        if kind is None and depth < 4 and len(entries) <= 2000 and path.name not in ('.ssh', '.codex', '.config'):
                            priority = {'output', 'outputs', 'datasets', 'dataset', 'checkpoints', 'ckpts', 'data', 'logs'}
                            important = [name for name in sorted(entries) if name.lower() in priority]
                            stack.extendleft((path / name, depth + 1) for name in reversed(important))
                            stack.extend((path / name, depth + 1) for name in sorted(entries) if name.lower() not in priority)
                            continue
                        value = measure_path(mount, path, timeout=max(1, min(8, deadline - time.monotonic())))
                        kind = kind or 'other'
                    elif stat.S_ISREG(metadata.st_mode):
                        kind = category(path)
                        value = dict(bytes=metadata.st_size, status='ok', stamp=int(time.time()))
                    else:
                        continue
                except PermissionError:
                    kind, value = 'other', dict(bytes=None, status='permission')
                except OSError:
                    kind, value = 'other', dict(bytes=None, status='error')
                row = dict(path=str(path), name=str(path.relative_to(root)) or '.', category=kind, **value)
                with _lock:
                    _details[key]['items'].append(row)
                partial |= value['status'] != 'ok'
            partial |= bool(stack)
        except OSError:
            partial = True
        finally:
            with _lock:
                _details[key].update(refreshing=False, partial=partial, stamp=int(time.time()), next=time.monotonic()+TTL)


def detail_snapshot(mount, user):
    if mount not in MOUNTS or not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}', user):
        raise ValueError('Invalid storage user')
    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise ValueError('Unknown storage user') from exc
    if account.pw_uid < 1000 or account.pw_uid == 65534:
        raise ValueError('Unsupported storage user')
    key = (mount, user)
    with _lock:
        if key not in _details and len(_details) >= 64:
            expired = next((k for k, v in _details.items() if not v['refreshing']), None)
            if expired is None:
                raise ValueError('Storage scans busy')
            del _details[expired]
        data = _details.setdefault(key, dict(mount=mount, user=user, items=[], refreshing=False, stamp=None, partial=False, next=0))
        if not data['refreshing'] and time.monotonic() >= data['next']:
            data.update(refreshing=True, items=[], partial=False)
            threading.Thread(target=collect_detail, args=key, daemon=True).start()
        result = copy.deepcopy(data)
    result.pop('next')
    return result
