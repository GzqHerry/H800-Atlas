"""Discover W&B runs in a worker's personal roots using job metadata or log evidence."""
import json
import os
import re
import threading
import tempfile
import time
from collections import defaultdict
from pathlib import Path

RUN = re.compile(r'(?:offline-)?run-\d{8}_\d{6}-([\w-]+)$')
# Only actual SDK announcements, not arbitrary links quoted by sync scripts.
ANNOUNCE = re.compile(rb'wandb:\s*(?:Run data is saved locally in\s+([^\r\n\x1b]+)|(?:[^\r\n]{0,10})View run(?: [^\r\n]*? at[: ]+| at[: ]+)\s*https?://[^\s]+/runs/([\w-]+))')
ANSI = re.compile(rb'\x1b\[[0-?]*[ -/]*[@-~]')
SKIP = {'.git','.cache','.venv','node_modules','site-packages','__pycache__','.conda','miniconda3','anaconda3'}
_lock = threading.Lock()
_by_run = defaultdict(set)
_by_job = defaultdict(set)
_probed = {}
_scanning = False
_finished = 0


def markers(line):
    clean=ANSI.sub(b'',line)
    for m in ANNOUNCE.finditer(clean):
        if m[1]:
            path=Path(os.fsdecode(m[1].strip()))
            if RUN.fullmatch(path.name):yield str(path)
        else:yield '@'+m[2].decode()


def identity(folder,safe):
    """Use Slurm metadata when present. An exact attempts/<job> export also has provenance."""
    parts=folder.parts
    job=next((parts[i+1] for i,p in enumerate(parts[:-1]) if p=='attempts' and re.fullmatch(r'\d+(?:_\d+)?',parts[i+1])),None)
    try:
        p=safe(folder/'files/wandb-metadata.json')
        if p.stat().st_size>2**20:return 'invalid'
        data=json.loads(p.read_bytes())
        slurm=data.get('slurm',{})
        if 'array_job_id' in slurm and 'array_task_id' in slurm:
            return f"{slurm['array_job_id']}_{slurm['array_task_id']}"
        return str(slurm.get('job_id') or slurm.get('jobid') or job or '')
    except FileNotFoundError:return job or ''
    except (OSError,ValueError,RuntimeError,AttributeError,TypeError):return 'invalid'


def register(folder,safe):
    try:
        path=safe(folder)
        match=RUN.fullmatch(path.name)
        if not match or not safe(path/f'run-{match[1]}.wandb').is_file():return
        owner=identity(path,safe)
        with _lock:
            _by_run[match[1]].add(path)
            if owner and owner!='invalid':_by_job[owner].add(path)
    except (OSError,RuntimeError):pass


def probe(directory,safe):
    # Avoid reopening every run's metadata on every ten-second refresh.
    try:
        directory=safe(directory)
        stamp=directory.stat().st_mtime_ns
        now=time.monotonic()
        seen,when=_probed.get(directory,(None,0))
        if stamp==seen and now-when<120:return
        for folder in directory.iterdir():
            if RUN.fullmatch(folder.name):register(folder,safe)
        _probed[directory]=(stamp,now)
    except (OSError,RuntimeError):pass


def scan(roots,safe):
    global _scanning,_finished
    try:
        for root in roots:
            if not root.is_dir():continue
            for folder,dirs,_ in os.walk(root,followlinks=False):
                parent=Path(folder)
                for name in dirs:
                    if RUN.fullmatch(name):register(parent/name,safe)
                dirs[:]=[n for n in dirs if not (RUN.fullmatch(n) or n in SKIP or n.startswith('checkpoint-'))]
                # One low-priority scan per worker; never hold up the HTTP request.
                time.sleep(.002)
    finally:
        with _lock:_scanning=False;_finished=time.monotonic()


def binding_file(home,job):
    if not isinstance(job,str) or not re.fullmatch(r'\d+(?:_\d+)?',job):raise ValueError('作业号无效')
    return home/'.local/share/h800-atlas/wandb-links'/(job+'.json')


def validate_path(value,job,safe):
    if not isinstance(value,str) or not Path(value).is_absolute() or len(value)>4096:
        raise ValueError('请输入 run 目录或 .wandb 文件的绝对路径')
    path=safe(value)
    folder=path if path.is_dir() else path.parent
    match=RUN.fullmatch(folder.name)
    if not match:raise ValueError('请选择具体的 run 或 offline-run 目录，或其中的 .wandb 文件')
    stream=safe(folder/f'run-{match[1]}.wandb')
    if not stream.is_file() or (path!=folder and path!=stream):raise ValueError('请选择该 run 对应的 .wandb 文件')
    if identity(folder,safe) not in ('',job):raise ValueError('此 run 的作业信息与当前作业不匹配')
    with stream.open('rb') as f:
        if f.read(4)!=b':W&B':raise ValueError('文件没有有效的 W&B 历史记录头')
    return folder


def save_binding(file,folder):
    file.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',dir=file.parent,delete=False) as tmp:
        try:
            json.dump(str(folder),tmp);tmp.flush()
            os.replace(tmp.name,file)
        finally:Path(tmp.name).unlink(missing_ok=True)


def resolve(job,hints,bases,roots,safe,allow_index=True):
    global _scanning
    paths=set()
    ids=set()
    for hint in hints:
        if hint.startswith('@'):
            ids.add(hint[1:]);continue
        candidate=Path(hint)
        match=RUN.fullmatch(candidate.name)
        if not match:continue
        ids.add(match[1])
        for p in ([candidate] if candidate.is_absolute() else [b/candidate for b in bases]):
            try:
                folder=safe(p)
                if safe(folder/f'run-{match[1]}.wandb').is_file() and identity(folder,safe) in ('',job):
                    paths.add(folder)
            except (OSError,RuntimeError):pass
    if len(paths)==1 and len(ids)==1:return paths.pop(),'由当前作业日志定位 W&B run。',[]
    if not allow_index and not ids:return None,'',[]
    # Probe the submit directory, log directory, and this allocation's export first.
    for base in dict.fromkeys(bases):
        for relative in ('wandb','.wandb',f'attempts/{job}/wandb',f'attempts/{job}/wandb/wandb'):
            probe(base/relative,safe)
    with _lock:
        candidates=set().union(*(_by_run.get(i,()) for i in ids)) if ids else set(_by_job.get(job,()))
    matches=set(paths)
    for p in candidates:
        try:
            owner=identity(p,safe)
            if (owner==job or (ids and not owner)) and safe(p/f'run-{RUN.fullmatch(p.name)[1]}.wandb').is_file():matches.add(p)
        except (OSError,RuntimeError):pass
    if len(matches)==1 and len(ids)<=1:return matches.pop(),('按日志中的 W&B run ID 匹配。' if ids else '按 Slurm 作业号匹配 W&B run。'),[]
    if len(matches)>1:return None,'发现多个匹配的 W&B 目录，请选择本次训练的 run。',sorted(map(str,matches))
    with _lock:
        if not _scanning and (not _finished or time.monotonic()-_finished>120):
            _scanning=True
            threading.Thread(target=scan,args=(roots,safe),daemon=True).start()
        scanning=_scanning
    note='正在个人目录中查找与此作业匹配的 W&B run。' if scanning else '未找到能确认属于此作业的本地 W&B run。'
    if len(ids)>1:note='日志记录了多个 W&B run，可选择已有候选或填写路径。'
    return None,note,sorted(map(str,matches))
