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
    if extra - {'checkpoint_validation.json','training_complete.json','SHA256SUMS'}:
        raise ValueError('Unexpected files at destination')
if q['action'] == 'prepare':
    if not final.exists():
        stage.mkdir(mode=0o700, exist_ok=True)
        for child in stage.iterdir():
            if child.name not in files or child.is_symlink() or not child.is_file():
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
            verify(final)
        else:
            mask = os.umask(0)
            os.umask(mask)
            stage.chmod(0o777 & ~mask)
            stage.rename(final)
    finally:
        os.close(fd)
else:
    raise ValueError('Unknown transfer request')
print(json.dumps(dict(done=final.is_dir(), stage=str(stage), final=str(final))))
"""


def inference_files(path):
    names = {'config.json'}
    for stem, suffix in (('model', '.safetensors'), ('pytorch_model', '.bin')):
        weight = stem + suffix
        index = path / (weight + '.index.json')
        if index.is_file():
            if index.is_symlink():
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
        if file.is_symlink() or not file.is_file():
            raise ValueError('推理文件缺失或为链接：' + name)
        st = file.stat()
        result[name] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
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


def upload(path, job, update, transfer, target_name='Grus'):
    host, ssh = connection(target_name)
    source = inference_files(path)
    size = sum(st[2] for st in source.values())
    files = {}
    update('计算 SHA-256', bytes=size)
    for name, st in source.items():
        digest = hashlib.sha256()
        with (path / name).open('rb') as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
                digest.update(block)
        files[name] = dict(size=st[2], sha256=digest.hexdigest())
    if inference_files(path) != source:
        raise ValueError('校验期间推理文件发生变化，请重试')
    version = hashlib.sha256(json.dumps([str(path), os.getuid(), files], sort_keys=True).encode()).hexdigest()[:32]
    def remote(action):
        request = dict(action=action, target=target_name, group=Path(job['meta']['group']).name, name=path.name,
                       version=version, files=files)
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
        transfer(cmd + [str(path / n) for n in files] + [host + ':' + target['stage'] + '/'], update)
    if inference_files(path) != source:
        raise ValueError('传输期间推理文件发生变化，请重试')
    update('校验中')
    remote('publish')
    update('完成', percent=100, bytes=size, verified=True)
