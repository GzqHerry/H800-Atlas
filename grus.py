"""Publish inference checkpoints to the configured SSH model stores."""
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

SSH = ['ssh', '-o', 'StrictHostKeyChecking=yes',
       '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=4']
SIDECARS = (
    'checkpoint_manifest.json', 'normalization.json', 'vla_metadata.json', 'trainer_state.json',
    'policy_tokenizer.model', 'tokenizer.model', 'tokenizer.json', 'tokenizer_config.json',
    'special_tokens_map.json', 'added_tokens.json', 'vocab.json', 'vocab.txt', 'merges.txt',
    'generation_config.json', 'preprocessor_config.json', 'processor_config.json',
)
CONTAINERS = {'checkpoints', 'checkpoint', 'ckpts', 'output', 'outputs', 'preflight', 'train_runs', 'enactive-runs'}


def valid_task(name):
    return (isinstance(name, str) and bool(name.strip()) and name == name.strip()
            and not name.startswith('.') and '/' not in name and chr(92) not in name
            and name.isprintable() and len(name.encode()) <= 200
            and name.casefold() not in CONTAINERS and not re.fullmatch(r'(?:checkpoint-|global_step_)?[0-9]+', name))


def suggested_task(source, roots):
    parent = source.parent
    while parent.name.casefold() in CONTAINERS:
        parent = parent.parent
    return parent.name if any(root in parent.parents for root in roots) and valid_task(parent.name) else ''


def json_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def source_keys(path):
    # Scratch paths on different nodes identify different files.
    node = os.uname().nodename if path.parts[1:2] == ('scratch',) else ''
    task = json_digest(['H800', Path.home().name, node, str(path.parent)])
    return task, json_digest([task, path.name])


def source_stamp(inventory):
    return json_digest({name: record[1:] for name, record in inventory.items()})


REMOTE = """
import fcntl, hashlib, json, os, pathlib, re, shutil, socket, sys
q = json.load(sys.stdin)
label = q['target']
hostname, directory = {'Grus': ('grus', '/mnt/data0/arx_ckpts'),
                       'Tianji': ('marvin-thor', '/WD/tianji/models')}[label]
if socket.gethostname().split('.')[0].lower() != hostname:
    raise ValueError('Unexpected SSH host for ' + label)
root = pathlib.Path(directory)
group, name = q['group'], q['name']
if group in ('', '.', '..') or '/' in group or not re.fullmatch(r'(?:checkpoint-|global_step_)?[0-9]+', name):
    raise ValueError('Invalid checkpoint destination')
parent = root / group
final = parent / name
if not re.fullmatch(r'[a-f0-9]{32}',q['version']):
    raise ValueError('Invalid transfer version')
stage = parent / ('.' + name + '.atlas-' + q['version'])
for path in (root, parent, final, stage):
    if path.resolve() != path:
        raise ValueError('Unexpected link in checkpoint destination')
if not root.is_dir():
    raise ValueError('Checkpoint volume unavailable on ' + label)
proof_name = '.atlas-grus-source.json'
if label == 'Grus':
    owner = q['taskKey']
    if not all(re.fullmatch('[0-9a-f]{64}', q[k]) for k in ('taskKey','sourceKey','payload')):
        raise ValueError('Invalid source identity')
    # Reserve a new task under the root lock. Never adopt an unmarked legacy directory.
    fd = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        task_file = parent / '.atlas-grus-task'
        if not parent.exists():
            import tempfile
            tmp = pathlib.Path(tempfile.mkdtemp(prefix='.atlas-task-', dir=root))
            try:
                (tmp / task_file.name).write_text(owner)
                tmp.chmod(0o755)
                tmp.rename(parent)
            finally:
                if tmp.exists(): shutil.rmtree(tmp)
        if task_file.is_symlink() or not task_file.is_file() or task_file.read_text() != owner:
            raise ValueError('Grus 任务目录已存在，来源不同或待确认；请选择其他任务目录名')
    finally:
        os.close(fd)
else:
    parent.mkdir(exist_ok=True)
files = q['files']
if not files or any(not n or n in ('.','..') or pathlib.Path(n).name != n for n in files):
    raise ValueError('Invalid file list')
def verify(folder):
    for n, record in files.items():
        path = folder / n
        if path.is_symlink() or not path.is_file() or path.stat().st_size != record['size']:
            raise ValueError('File missing or size differs: ' + n)
        h = hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda: f.read(8*1024*1024), b''):
                h.update(block)
        if h.hexdigest() != record['sha256']:
            raise ValueError('Checksum differs: ' + n)
    extra = {p.name for p in folder.iterdir()} - set(files)
    if label == 'Grus':
        extra.discard(proof_name)
    if extra - {'checkpoint_validation.json','training_complete.json','SHA256SUMS'}:
        raise ValueError('Unexpected files at destination')
def file_stamp(path):
    st = path.stat()
    return [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]

def proof(folder):
    return dict(schema=1, taskKey=q['taskKey'], sourceKey=q['sourceKey'], payload=q['payload'],
                destination=str(final), files=files, stats={n:file_stamp(folder/n) for n in files})

def require_source():
    if label != 'Grus': return
    marker = final / proof_name
    stored = json.loads(marker.read_text()) if marker.is_file() and not marker.is_symlink() else {}
    stored.pop('sourceStamp', None)
    if stored != proof(final):
        raise ValueError('Grus 目标已存在，来源或内容不一致；已保留现有文件')

if q['action'] == 'prepare':
    if final.exists(): require_source()
    if not final.exists():
        stage.mkdir(mode=0o700, exist_ok=True)
        for child in stage.iterdir():
            if (child.name not in files and not (label == 'Grus' and child.name == proof_name)) or child.is_symlink() or not child.is_file():
                raise ValueError('Unexpected staging file')
        needed = sum(max(0, rec['size'] - (stage/n).stat().st_size)
                     if (stage/n).exists() else rec['size'] for n, rec in files.items())
        if shutil.disk_usage(root).free < needed:
            raise ValueError('Insufficient space on ' + label)
elif q['action'] == 'publish':
    if not final.exists():
        verify(stage)
    fd = os.open(parent, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if final.exists():
            require_source()
            verify(final)
        else:
            mask = os.umask(0)
            os.umask(mask)
            stage.chmod(0o777 & ~mask)
            if label == 'Grus':
                (stage / proof_name).write_text(json.dumps(dict(proof(stage), sourceStamp=q['sourceStamp']),sort_keys=True))
            stage.rename(final)
    finally:
        os.close(fd)
else:
    raise ValueError('Unknown transfer request')
print(json.dumps(dict(done=final.is_dir(), stage=str(stage), final=str(final))))
"""


def inference_files(path, *, descriptor_view=False, include_ctime=False):
    names = {'config.json'}
    for stem, suffix in (('model', '.safetensors'), ('pytorch_model', '.bin')):
        weight = stem + suffix
        index = path / (weight + '.index.json')
        if index.is_file():
            if index.is_symlink() and not descriptor_view:
                raise ValueError('不支持链接权重索引')
            content = json.loads(index.read_text())
            mapping = content.get('weight_map') if isinstance(content, dict) else None
            if not isinstance(mapping, dict) or not mapping:
                raise ValueError('权重分片索引为空')
            for shard in mapping.values():
                if not isinstance(shard, str) or Path(shard).name != shard or not shard.endswith(suffix):
                    raise ValueError('权重分片索引中的文件名无效')
                names.add(shard)
            names.add(index.name)
            break
        if (path / weight).is_file():
            names.add(weight)
            break
    else:
        raise ValueError('没有找到可供推理使用的模型权重或分片索引')
    names.update(n for n in SIDECARS if (path / n).exists())
    result = {}
    for name in sorted(names):
        file = path / name
        if (file.is_symlink() and not descriptor_view) or not file.is_file():
            raise ValueError('推理文件缺失或为链接：' + name)
        st = file.stat()
        result[name] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if include_ctime:
            result[name] += (st.st_ctime_ns,)
    return result


def connection(target):
    if target == 'Grus':
        host = 'gzq@100.64.0.41' if Path.home().name == 'gzqherry' else '100.64.0.41'
    elif target == 'Tianji':
        host = 'user@172.30.1.208'
        password = Path.home()/'.config/h800-atlas/tianji.password'
        if password.exists():
            info = password.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError('Tianji 密码文件须为本人所有，并设置为 600 权限')
            return host, ['sshpass', '-f', str(password), *SSH, '-o', 'NumberOfPasswordPrompts=1']
    else:
        raise ValueError('未知的推理权重上传目标')
    return host, [*SSH, '-o', 'BatchMode=yes']


def checkpoints():
    host, ssh = connection('Grus')
    script = Path(__file__).with_name('grus_catalog.py').read_text()
    result = subprocess.run(ssh + [host, 'python3 -'], input=script, capture_output=True,
                            text=True, timeout=12, check=True)
    return json.loads(result.stdout)


def upload(path, job, update, transfer, target_name='Grus', *, source_fds=(), source_uid=None):
    group = Path(job['meta']['group']).name
    identity = {}
    if target_name == 'Grus':
        group = job['meta'].get('grusTask')
        if not valid_task(group):
            raise ValueError('请选择 Grus 训练任务目录后重新上传')
        task_key, source_key = source_keys(Path(job['source']))
        identity = dict(taskKey=task_key, sourceKey=source_key)
    host, ssh = connection(target_name)
    def inventory():
        return inference_files(path, descriptor_view=bool(source_fds), include_ctime=bool(identity))
    source = inventory()
    size = sum(st[2] for st in source.values())
    files = {}
    update('计算 SHA-256', bytes=size)
    for name, st in source.items():
        digest = hashlib.sha256()
        with (path / name).open('rb') as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
                digest.update(block)
        files[name] = dict(size=st[2], sha256=digest.hexdigest())
    if inventory() != source:
        raise ValueError('校验期间推理文件发生变化，请重试')
    version = hashlib.sha256(json.dumps([job['source'], os.getuid() if source_uid is None else source_uid, files], sort_keys=True).encode()).hexdigest()[:32]
    if identity:
        version = json_digest([version, job['id']])[:32]
        identity.update(payload=json_digest(files), sourceStamp=source_stamp(source))
    def remote(action):
        request = dict(action=action, target=target_name, group=group, name=path.name,
                       version=version, files=files, **identity)
        proc = subprocess.run(ssh + [host, 'python3 -c ' + shlex.quote(REMOTE)],
                              input=json.dumps(request), capture_output=True, text=True, timeout=3600)
        if proc.returncode:
            reason = (proc.stderr.strip().splitlines() or ['SSH 请求失败'])[-1]
            raise RuntimeError(target_name + '：' + reason[:300])
        return json.loads(proc.stdout)
    target = remote('prepare')
    update('校验中' if target['done'] else '传输中', destination=target['final'])
    if not target['done']:
        cmd = ['rsync', '-rt', '--checksum', '--partial', '--protect-args', '--info=progress2',
               '--outbuf=L', '-e', shlex.join(ssh), '--']
        if source_fds:
            cmd.insert(1, '--copy-links')
        args = cmd + [str(path / n) for n in files] + [host + ':' + target['stage'] + '/']
        if source_fds:
            transfer(args, update, pass_fds=source_fds)
        else:
            transfer(args, update)
    if inventory() != source:
        raise ValueError('传输期间推理文件发生变化，请重试')
    update('校验中')
    remote('publish')
    receipt = dict(grusSource=source_key, grusPayload=identity['payload'], grusStamp=source_stamp(source)) if identity else {}
    update('完成', percent=100, bytes=size, verified=True, **receipt)
