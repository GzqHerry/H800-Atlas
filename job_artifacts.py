"""Read scalar artifacts only within a directory attributed to this Slurm job."""
import json
import math
import os
import re
import subprocess
import sys
import threading
from collections import OrderedDict
from pathlib import Path

READER = Path('/workspace/gzqherry/h800-atlas-team/runtime/tensorboard-2.20.0/bin/python')
STEP = re.compile(r'\bGlobal Step:\s*(\d+)(?:\s*/\s*(\d+))?', re.I)
CELL = re.compile(r'([\w./-]+)\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)')
JOB_DIR = re.compile(r'(?:slurm|job)[-_](\d+(?:_\d+)?)$', re.I)
CONTAINERS = ('runs', 'logs', 'results', 'outputs', 'tensorboard')
_cache = OrderedDict()
_lock = threading.Lock()


def manifest_ids(folder):
    ids = set()
    for name in ('SUBMISSION.json', 'submission.json', 'allocation.json'):
        path = folder / name
        try:
            if path.is_symlink() or not path.is_file():
                continue
            with path.open('rb') as stream:
                record = json.loads(stream.read(128 * 1024))
            if isinstance(record, dict) and record.get('job_id') is not None:
                ids.add(str(record['job_id']))
        except (OSError, ValueError):
            continue
    return ids


def roots_for(job, bases, safe):
    roots = set()
    for candidate in bases:
        try:
            base = safe(candidate)
            if not base.is_dir():
                continue
            names = (job, 'slurm-' + job, 'job-' + job, 'job_' + job, 'slurm_' + job)
            for parent in (base, *(base / name for name in CONTAINERS)):
                for name in names:
                    child = parent / name
                    if child.is_dir() and not child.is_symlink() and safe(child) == child:
                        if manifest_ids(child) <= {job}:
                            roots.add(child)
            ids = manifest_ids(base)
            scoped = base.name in names and ids <= {job}
            if ids == {job}:
                # Shared sibling results are ambiguous if this launch directory was reused.
                runs = base / 'runs'
                others = runs.is_dir() and any(
                    (match := JOB_DIR.fullmatch(entry.name)) and match[1] != job
                    for entry in runs.iterdir())
                scoped = not others
            if scoped:
                roots.add(base)
            for parent in base.parents:
                if parent.name in names and safe(parent) == parent and manifest_ids(parent) <= {job}:
                    roots.add(parent)
                    break
        except (OSError, RuntimeError, ValueError):
            continue
    return sorted(path for path in roots if not roots.intersection(path.parents))


def artifacts(job, roots, safe):
    found = {}
    for root in roots:
        for folder, dirs, files in os.walk(root):
            path = Path(folder)
            depth = len(path.relative_to(root).parts)
            dirs[:] = sorted(d for d in dirs if depth < 5 and not d.startswith('.')
                             and d not in ('repo', 'checkpoints', '__pycache__')
                             and not (path / d).is_symlink()
                             and (not JOB_DIR.fullmatch(d) or JOB_DIR.fullmatch(d)[1] == job))
            if manifest_ids(path) - {job}:
                dirs.clear()
                continue
            for name in sorted(files):
                if name != 'metrics.log' and not name.startswith('events.out.tfevents.'):
                    continue
                file = path / name
                try:
                    if not file.is_symlink() and safe(file) == file and file.is_file():
                        found.setdefault(file, root)
                except (OSError, ValueError, RuntimeError):
                    continue
    return found


def read_table(path, offset, step, section):
    rows = []
    with path.open('rb') as stream:
        stream.seek(offset)
        for raw in stream:
            if not raw.endswith(b'\n'):
                break
            offset += len(raw)
            line = raw.decode('utf-8', 'replace').strip()
            if line.startswith('{'):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                index = record.get('global_step', record.get('step'))
                metrics = record.get('metrics', record)
                if type(index) is int and isinstance(metrics, dict):
                    for tag, value in metrics.items():
                        if tag in ('global_step', 'step') or type(value) not in (int, float):
                            continue
                        try:
                            if math.isfinite(value):
                                rows.append((tag, index, value))
                        except OverflowError:
                            continue
                continue
            if line.startswith('├'):
                section = line.strip('├┤─ ').strip()
                if section == 'Metric Table':
                    step = None
                continue
            match = STEP.search(line)
            if match:
                step = int(match[1])
                section = ''
                continue
            if step is not None and section and line.startswith('│'):
                for cell in line.strip('│').split('│'):
                    match = CELL.fullmatch(cell.strip())
                    if match and math.isfinite(value := float(match[2])):
                        rows.append((section + '/' + match[1], step, value))
    return offset, step, section, rows


def read_events(path, offset):
    import struct
    from tensorboard.compat.proto import event_pb2
    from tensorboard.compat.tensorflow_stub.pywrap_tensorflow import masked_crc32c
    from tensorboard.util.tensor_util import make_ndarray

    rows = []
    with path.open('rb') as stream:
        stream.seek(offset)
        while len(header := stream.read(12)) == 12:
            length, crc = struct.unpack('<QI', header)
            if crc != masked_crc32c(header[:8]):
                raise ValueError('invalid event header')
            if length > 16 * 1024 * 1024:
                # Large summaries usually contain media. Skip only fully written records.
                stream.seek(length, 1)
                if len(stream.read(4)) < 4:
                    break
                offset = stream.tell()
                continue
            payload = stream.read(length)
            trailer = stream.read(4)
            if len(trailer) < 4:
                break
            if struct.unpack('<I', trailer)[0] != masked_crc32c(payload):
                raise ValueError('invalid event checksum')
            event = event_pb2.Event.FromString(payload)
            for item in event.summary.value:
                kind = item.WhichOneof('value')
                if kind == 'simple_value':
                    value = item.simple_value
                elif (kind == 'tensor' and not item.tensor.tensor_shape.dim
                      and not item.tensor.tensor_shape.unknown_rank
                      and item.metadata.plugin_data.plugin_name in ('', 'scalars')):
                    array = make_ndarray(item.tensor)
                    if array.dtype.kind not in 'fiu' or array.size != 1:
                        continue
                    value = float(array.item())
                else:
                    continue
                if math.isfinite(value):
                    rows.append((item.tag, event.step, value))
            offset = stream.tell()
    return offset, None, '', rows


def cached_points(path):
    stat = path.stat()
    identity = (stat.st_dev, stat.st_ino)
    with _lock:
        state = _cache.get(path)
        if state:
            ident, size, stamp, offset, step, section, points = state
            if (ident, size, stamp) == (identity, stat.st_size, stat.st_mtime_ns):
                _cache.move_to_end(path)
                return points
        if not state or ident != identity or stat.st_size <= size:
            offset, step, section, points = 0, None, '', {}
        if path.name == 'metrics.log':
            offset, step, section, rows = read_table(path, offset, step, section)
        else:
            proc = subprocess.run([str(READER), '-I', __file__, str(path), str(offset)],
                                  capture_output=True, timeout=35, check=True)
            offset, step, section, rows = json.loads(proc.stdout)
        # Return snapshots so a concurrent refresh cannot mutate a chart being serialized.
        changed = {}
        for tag, x, value in rows:
            if tag not in changed:
                changed[tag] = points.get(tag, {}).copy()
            changed[tag][x] = value
        points = {**points, **changed}
        _cache[path] = identity, stat.st_size, stat.st_mtime_ns, offset, step, section, points
        _cache.move_to_end(path)
        if len(_cache) > 32:
            _cache.popitem(last=False)
        return points


def collect(job, bases, safe, attempt=0):
    series, paths, errors = {}, [], []
    roots = roots_for(job, bases, safe)
    if attempt > 1:
        # A retained allocation can contain several independent training attempts.
        roots = [path for root in roots for path in
                 (root / 'attempts' / str(attempt), root / f'attempt-{attempt}', root / f'attempt_{attempt}')
                 if path.is_dir() and not path.is_symlink() and path.resolve() == path]
    for path, root in artifacts(job, roots, safe).items():
        tracker = 'metrics.log' if path.name == 'metrics.log' else 'TensorBoard'
        try:
            tags = cached_points(path)
        except (OSError, ValueError, subprocess.SubprocessError):
            errors.append(str(path))
            continue
        if not tags:
            continue
        paths.append(str(path))
        relative = path.parent.relative_to(root)
        scope = str(relative) if relative.parts else ''
        if len(roots) > 1:
            scope = '/'.join(filter(None, (str(root), scope)))
        for tag, points in tags.items():
            key = '/'.join(filter(None, (tracker, scope, tag)))
            series.setdefault(key, {}).update(points)
    def order(key):
        return (not key.endswith(('/Environment/reward', '/env/reward')),
                not key.startswith('metrics.log/'), key)
    return {key: series[key] for key in sorted(series, key=order)}, paths, errors


if __name__ == '__main__':
    print(json.dumps(read_events(Path(sys.argv[1]), int(sys.argv[2])), allow_nan=False))
