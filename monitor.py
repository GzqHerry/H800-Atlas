"""Slurm accounting and saved training metadata for the H800 workbench."""
import json
import hashlib
import os
import tempfile
import training_eta
from scheduler import scheduling, command_details
from cpu_usage import add_cpu_usage
from gpu_usage import add_gpu_usage
from insights import gpu_counts, queue_forecasts, saved_time
import math
import re
import subprocess
import threading
import time
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

CK_NAME = re.compile(r'(?:checkpoint-|global_step_)?(\d+)$')
_cluster_lock = threading.Lock()
_cluster_sample = {}


def slurm(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=9, check=True)
    return result.stdout.splitlines()


@lru_cache(maxsize=256)
def expand_nodes(expr):
    if '[' not in expr:
        return expr.split(',')
    return slurm('scontrol', 'show', 'hostnames', expr)


def cluster():
    global _cluster_sample
    with _cluster_lock:
        if time.time() - _cluster_sample.get('stamp', 0) < 10:
            return _cluster_sample
        nodes = {}
        for line in slurm('sinfo', '-a', '-N', '-h', '-o', '%N|%T|%G|%E'):
            name, state, hardware, reason = line.split('|', 3)
            nodes[name] = dict(name=name, state=state, hardware=hardware, reason=reason, jobs=[])
        try:
            for line in slurm('scontrol','-d','-o','show','node'):
                fields=dict(re.findall(r'(\w+)=([^\s]*)',line))
                if fields.get('NodeName') in nodes:
                    node=nodes[fields['NodeName']]
                    node['gpu']=gpu_counts(fields)
                    node['cpu']={key:float(fields[field]) if re.fullmatch(r'\d+(?:\.\d+)?',fields.get(field,'')) else None
                                 for key,field in [('allocated','CPUAlloc'),('effective','CPUEfctv'),('total','CPUTot'),('load','CPULoad')]}
        except (OSError,subprocess.SubprocessError):
            pass
        fields = ['id','user','state','elapsed','limit','nodes','reason','partition',
                  'gres','expected','submitted','name','priority','qos','workdir','command']
        rows = []
        sep='\x1f'
        fmt=sep.join(['%i','%u','%T','%M','%l','%N','%R','%P','%b','%S','%V','%j','%Q','%q','%Z','%o'])
        owner=Path.home().name
        for line in slurm('squeue','-a','-r','-h','-o',fmt):
            job=dict(zip(fields,line.split(sep,len(fields)-1)))
            if job.get('command') in ('N/A','(null)','None','Unknown'):
                job['command']=''
            if job['user']!=owner and job['state']=='PENDING':job.pop('expected',None)
            rows.append(job)
            if job['state'] != 'PENDING' and job['nodes'] not in ('', '(null)', 'N/A'):
                for host in expand_nodes(job['nodes']):
                    if host in nodes:
                        nodes[host]['jobs'].append(job)
        training_eta.attach(rows)
        add_cpu_usage(nodes)
        add_gpu_usage(nodes)
        _cluster_sample = dict(owner=owner,scheduling=scheduling(),forecasts=queue_forecasts(rows,owner),nodes=sorted(nodes.values(),key=lambda n:n['name']), jobs=rows,
                               stamp=int(time.time()), zone=time.strftime('%Z'))
        return _cluster_sample


def inventory(home):
    src=home/'nas-archive-20261112/inventory.json'
    if not src.is_file():
        return {}
    with src.open() as stream:
        return json.load(stream)['checkpoint_dirs']


def checkpoint_groups(home):
    groups=defaultdict(dict)
    for value, entry in inventory(home).items():
        path=Path(value)
        if CK_NAME.fullmatch(path.name):
            groups[path.parent][path.name]=entry[1]
    tasks=set(groups)
    for catalog in (home/'.local/share/h800-atlas/checkpoint-scans').glob('*.json'):
        for folder in json.loads(catalog.read_text()):
            path=Path(folder)
            if CK_NAME.fullmatch(path.name) and path.is_dir():
                groups[path.parent].setdefault(path.name,None)
            if CK_NAME.fullmatch(path.name):
                tasks.add(path.parent)
    # Discover sibling runs in personal output collections. Inspect names only,
    # never recurse into weights or walk a user's entire workspace.
    personal=(home, *(Path(base)/home.name for base in ('/workspace','/data','/scratch')))
    collections={root/'Enactive'/'output' for root in personal}
    for task in tasks:
        collection=task.parent
        if collection.name in ('output','outputs','train_runs','enactive-runs','checkpoints') and any(root in collection.parents for root in personal):
            collections.add(collection)
    for collection in collections:
        try:
            if collection.resolve(strict=True)!=collection:continue
            with os.scandir(collection) as entries:
                for entry in entries:
                    if not entry.name.startswith('.') and entry.is_dir(follow_symlinks=False):
                        if CK_NAME.fullmatch(entry.name):
                            tasks.add(collection)
                        else:
                            tasks.add(Path(entry.path))
        except OSError:
            continue
    for task in tasks:
        try:
            with os.scandir(task) as entries:
                for entry in entries:
                    if CK_NAME.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                        groups[task].setdefault(entry.name,None)
        except OSError:
            continue
    return groups

def scan_checkpoints(home, value):
    if not isinstance(value,str) or not value.strip():raise ValueError('请输入 H800 目录的绝对路径')
    root=Path(value.strip()).expanduser()
    if not root.is_absolute():raise ValueError('请输入绝对路径或以 ~/ 开头的路径')
    root=root.resolve(strict=True)
    if not root.is_dir():raise ValueError('请选择目录')
    if root==Path('/') or root.parts[1] in ('proc','sys','dev','run'):
        raise ValueError('请选择训练任务或输出目录')
    found=[]
    if CK_NAME.fullmatch(root.name):
        next(root.iterdir(),None)
        found.append(str(root))
    else:
        started=time.monotonic()
        skipped=0
        def inaccessible(error):
            nonlocal skipped
            if Path(error.filename)==root:raise error
            skipped+=1
        for i,(directory,dirs,_) in enumerate(os.walk(root,onerror=inaccessible)):
            if i>=30000 or time.monotonic()-started>20:
                raise ValueError('目录较大，请输入更具体的训练输出目录')
            descend=[]
            for name in dirs:
                p=Path(directory)/name
                if name.startswith('.') or p.is_symlink():continue
                if CK_NAME.fullmatch(name):
                    if os.access(p,os.R_OK|os.X_OK):found.append(str(p))
                else:descend.append(name)
            dirs[:]=descend
    index=home/'.local/share/h800-atlas/checkpoint-scans'
    index.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=index,mode='w',delete=False) as tmp:
        try:
            json.dump(found,tmp)
            tmp.flush()
            os.replace(tmp.name,index/(hashlib.sha256(os.fsencode(root)).hexdigest()+'.json'))
        finally:Path(tmp.name).unlink(missing_ok=True)
    return dict(path=str(root),count=len(found),skipped=0 if CK_NAME.fullmatch(root.name) else skipped)


def checkpoint_index(home):
    groups=checkpoint_groups(home)
    items=[dict(updated=max((saved_time(p/n) for n in names),default=0),path=str(p),name=p.name,count=len(names),bytes=sum(v or 0 for v in names.values()),
                 unmeasured=sum(v is None for v in names.values()))
            for p,names in groups.items()]
    return sorted(items,key=lambda row:(-row["updated"],row["path"]))


def number(v):
    if isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v):
        return v
    return None


def read_state(path, safe):
    try:
        checked=safe(path)
        if checked.stat().st_size<=64*1024*1024:
            with checked.open() as stream:
                value=json.load(stream)
                if isinstance(value,dict):
                    return value
    except (ValueError,OSError):
        pass
    return {}


def checkpoint_detail(home, group, safe):
    parent=Path(group)
    groups=checkpoint_groups(home)
    if parent not in groups:
        raise ValueError('Unknown training output')
    rows=[]
    parent_state=None
    for name,size in groups[parent].items():
        path=parent/name
        step=int(CK_NAME.fullmatch(name)[1])
        source=path/'trainer_state.json'
        # Only the two fixed trainer-state locations of a registered group are read here.
        def metadata_path(p):
            resolved=p.resolve(strict=True)
            if resolved==p and p.name=='trainer_state.json' and (p.parent==parent or p.parent==path):return resolved
            return safe(p)
        state=read_state(source,metadata_path)
        if not state:
            source=parent/'trainer_state.json'
            if parent_state is None:
                parent_state=read_state(source,metadata_path)
            state=parent_state
        own_state=source.parent==path
        if own_state and number(state.get('global_step')) is not None:
            step=state['global_step']
        history=[r for r in state.get('log_history',[]) if isinstance(r,dict)
                 and number(r.get('step')) is not None and r['step']<=step]
        epoch=number(state.get('epoch')) if state.get('global_step')==step or own_state else None
        exact=next((r for r in reversed(history) if r['step']==step and number(r.get('epoch')) is not None),{})
        if epoch is None:
            epoch=exact.get('epoch')
        sampled=max((r for r in history if number(r.get('loss')) is not None),key=lambda r:r['step'],default={})
        lr=max((r for r in history if number(r.get('learning_rate')) is not None),key=lambda r:r['step'],default={})
        rows.append(dict(updated=saved_time(path),name=name,path=str(path),bytes=size,present=path.is_dir(),step=step,epoch=epoch,
                         epochs=number(state.get('num_train_epochs')),loss=sampled.get('loss'),
                         lossStep=sampled.get('step'),lr=lr.get('learning_rate'),lrStep=lr.get('step'),
                         source=str(source) if state else None))
    return sorted(rows,key=lambda row:(row['updated'],row['step']),reverse=True)

def lookup_command(job_id):
    for job in cluster()['jobs']:
        if job['id']==job_id:return command_details(job)
    raise ValueError('作业已离开当前队列')
