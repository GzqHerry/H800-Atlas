"""Discover W&B runs in a worker's personal roots using job metadata or log evidence."""
import json
import os
import re
import subprocess
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
_embedded = {}
READER = Path(__file__).resolve().parents[2]/'runtime/wandb-0.23.1/bin/python'


def markers(line):
    clean=ANSI.sub(b'',line)
    for m in ANNOUNCE.finditer(clean):
        if m[1]:
            path=Path(os.fsdecode(m[1].strip()))
            if RUN.fullmatch(path.name):yield str(path)
        else:yield '@'+m[2].decode()


def sidecar_job(folder,safe):
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

def embedded_jobs(folders,safe):
    owners={}
    pending={}
    for folder in folders:
        try:
            match=RUN.fullmatch(folder.name)
            if not match:continue
            path=safe(folder/f'run-{match[1]}.wandb')
            if not path.is_file():continue
            st=path.stat()
        except (OSError,RuntimeError):continue
        signature=(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)
        cached=_embedded.get(path)
        if cached and (cached[0]==signature or (cached[1] and cached[0][:2]==signature[:2] and st.st_size>cached[0][2])):
            owners[folder]=cached[1]
        else:pending[str(path)]=(folder,signature)
    if pending:
        try:
            response=subprocess.run([str(READER),'-I','-B',str(Path(__file__).with_name('wandb_history.py')),'--jobs'],
                                    input=json.dumps(list(pending)),capture_output=True,text=True,check=True,timeout=12)
            for name,job in json.loads(response.stdout).items():
                folder,signature=pending[name]
                owner=job if re.fullmatch(r'\d+(?:_\d+)?',job) else ''
                _embedded[Path(name)]=(signature,owner)
                owners[folder]=owner
        except (OSError,ValueError,subprocess.SubprocessError):pass
    return owners

def identity(folder,safe):
    return sidecar_job(folder,safe) or embedded_jobs([folder],safe).get(folder,'')

def register(folders,safe):
    owners={}
    for folder in folders:
        try:
            path=safe(folder)
            match=RUN.fullmatch(path.name)
            if match and safe(path/f'run-{match[1]}.wandb').is_file():
                owners[path]=sidecar_job(path,safe)
        except (OSError,RuntimeError):continue
    owners.update(embedded_jobs([p for p,owner in owners.items() if not owner],safe))
    with _lock:
        for path,owner in owners.items():
            _by_run[RUN.fullmatch(path.name)[1]].add(path)
            if owner and owner!='invalid':_by_job[owner].add(path)


def probe(directory,safe):
    # Avoid reopening every run's metadata on every ten-second refresh.
    try:
        directory=safe(directory)
        stamp=directory.stat().st_mtime_ns
        now=time.monotonic()
        seen,when=_probed.get(directory,(None,0))
        if stamp==seen and now-when<120:return
        register((p for p in directory.iterdir() if RUN.fullmatch(p.name)),safe)
        _probed[directory]=(stamp,now)
    except (OSError,RuntimeError):pass


def scan(roots,safe):
    global _scanning,_finished
    try:
        for root in roots:
            if not root.is_dir():continue
            for folder,dirs,_ in os.walk(root,followlinks=False):
                parent=Path(folder)
                runs=[parent/name for name in dirs if RUN.fullmatch(name)]
                if runs:register(runs,safe)
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
