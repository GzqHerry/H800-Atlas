"""Dataset discovery, physical GPU accounting and locally fitted queue estimates."""
import json
import math
import os
import re
import subprocess
import threading
import time
from urllib.request import urlopen
from datetime import datetime
from pathlib import Path

def mtime(path):
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0

def newest(*paths):
    return max((mtime(p) for p in paths),default=0)

def saved_time(path):
    return newest(path,path/'trainer_state.json',path/'checkpoint_manifest.json')

def read_metadata(path):
    try:
        with path.open() as stream:
            value=json.load(stream)
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):
        return {}


def dataset_name(record):
    value=record.get('dataset_name')
    return value.strip() if isinstance(value,str) else ''


def service_names(items,root):
    endpoint=os.environ.get('BODYPIPELINE_JOBS_URL','http://100.64.0.71:8081/api/jobs')
    known={}
    try:
        with urlopen(endpoint,timeout=4) as response:
            jobs=json.load(response)
    except (OSError,ValueError):
        return known
    if not isinstance(jobs,list):
        return known
    for row in jobs:
        if not isinstance(row,dict) or not isinstance(row.get('dataset_path'),str):
            continue
        path=Path(row['dataset_path'])
        if (str(path) in items and path.is_relative_to(root) and path.name=='dataset'
                and path.parent.parent.name=='work' and path.parent.name==row.get('id') and dataset_name(row)):
            known[str(path)]=dataset_name(row)
    return known


def resolve_names(items,root):
    # A source in a merge manifest names that source only, never the output.
    catalog=service_names(items,root)
    references={}
    for key,item in items.items():
        path=Path(key)
        if path.name!='dataset' or path.parent.parent.name!='work' or not path.is_relative_to(root):
            continue
        job=path.parent
        item['jobId']=job.name
        if key in catalog:
            item.update(name=catalog[key],nameSource='转换服务')
            continue
        name=''
        for f in [job/'lineage.json',
                  *sorted((path/'meta').glob('bodypipeline*.json'))]:
            row=read_metadata(f)
            if row.get('job_id',row.get('id',job.name))!=job.name:
                continue
            if not name:
                name=dataset_name(row)
            for source in row.get('sources') or []:
                if not isinstance(source,dict):continue
                ref=source.get('dataset_path')
                if ref in items and source.get('job_id')==Path(ref).parent.name and dataset_name(source):
                    references.setdefault(ref,set()).add(dataset_name(source))
            ref=row.get('source_path')
            label=row.get('source_dataset_name')
            if ref in items and isinstance(label,str) and label.strip() and row.get('source_job_id')==Path(ref).parent.name:
                references.setdefault(ref,set()).add(label.strip())
        if name:
            item['name']=name
            item['nameSource']='作业元数据'
    for key,item in items.items():
        if not item.get('nameSource') and len(references.get(key,()))==1:
            item['name']=next(iter(references[key]))
            item['nameSource']='关联作业元数据'
        if not item.get('nameSource') and item['kind']=='原始采集':
            # A collection may contain multiple named episodes; keep names distinct.
            names=set()
            for manifest in (Path(key)/'episodes').glob('*/manifest.json'):
                label=dataset_name(read_metadata(manifest))
                if label:names.add(label)
                if len(names)>1:break
            if len(names)==1:
                item['name']=names.pop()
                item['nameSource']='采集清单'
        path=Path(key)
        if path.parent.parent.name=='work' and path.name!='dataset':
            canonical=catalog.get(str(path.parent/'dataset'))
            if canonical:
                item.update(name=canonical+' · '+path.name,jobId=path.parent.name,nameSource='所属作业 / 子目录')
        if not item.get('nameSource'):
            if re.fullmatch(r'[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',item['name']):
                item['name']='名称未获取 · '+item['name'][:12]
                item['nameSource']='目录标识'
            else:item['nameSource']='目录名'


SHARED_DATA = Path('/data/share/bodypipeline/runtime-gzqherry/data')
DATASET_ROOTS = [SHARED_DATA, Path('/data/gzqherry/lerobot_v3'),
                 *[Path('/workspace/gzqherry')/name for name in ('datasets','training_assets','data_episode')]]

def discover():
    root,*personal_roots=DATASET_ROOTS
    robots=sorted(p.name for p in root.iterdir() if p.is_dir())
    items={}
    def add(path,robot,kind,meta=None):
        data=read_metadata(meta) if meta else {}
        name=path.parent.name if path.name=='dataset' else path.name
        items[str(path)]=dict(name=dataset_name(data) or name,nameSource="数据集元数据" if dataset_name(data) else None,path=str(path),robot=robot,kind=kind,
                              episodes=data.get('total_episodes'),frames=data.get('total_frames'),
                              updated=newest(path,meta) if meta else mtime(path))
    for folder in personal_roots:
        for pattern in ('meta/info.json','*/meta/info.json','*/*/meta/info.json'):
            for meta in folder.glob(pattern):
                add(meta.parent.parent,'共享目录','LeRobot',meta)
    for robot in robots:
        for directory,dirs,files in os.walk(root/robot,followlinks=False):
            path=Path(directory)
            if 'meta' in dirs and (path/'meta/info.json').is_file():
                add(path,robot,'LeRobot',path/'meta/info.json')
                dirs.clear()
            elif path.parent.name=='collections' and 'episodes' in dirs:
                add(path,robot,'原始采集')
                dirs.clear()
            elif path.parent.name=='incoming' and 'manifest.json' in files:
                add(path,robot,'导入批次',path/'manifest.json')
                dirs[:]=[d for d in dirs if d=='data']
            else:
                dirs[:]=[d for d in dirs if d not in ('videos','images','episodes','annotations','analyses','meta','.git','__pycache__') and not d.startswith('chunk-')]
    resolve_names(items,root)
    return robots,sorted(items.values(),key=lambda a:(-a['updated'],a['path']))

def gpu_counts(fields):
    config=fields.get('CfgTRES','')
    match=re.search(r'(?:^|,)gres/gpu=(\d+)',config)
    total=int(match[1]) if match else None
    usage=fields.get('GresUsed','')
    whole=set()
    known=total is not None
    for count,indices in re.findall(r'(?:^|,)gpu:(?:[^,:()]+:)?(\d+)\(IDX:([^)]*)\)',usage):
        expanded=set()
        if int(count):
            for token in indices.split(','):
                if not re.fullmatch(r'\d+(?:-\d+)?',token):
                    known=False
                    continue
                ends=list(map(int,token.split('-')))
                expanded.update(range(ends[0],ends[-1]+1))
        known &= len(expanded)==int(count)
        whole.update(expanded)
    if 'gpu:' not in usage or '(IDX:' not in usage:
        known=False
    shared=set()
    for count,layout in re.findall(r'(?:^|,)shard:(?:[^,:()]+:)?(\d+)\(([^)]*)\)',usage):
        slots=re.findall(r'(\d+)/\d+',layout)
        if sum(map(int,slots))!=int(count) or (int(count) and len(slots)!=total):
            known=False
        shared.update(i for i,n in enumerate(slots) if int(n))
    if 'shard:' in fields.get('Gres','') and not re.search(r'shard:[^()]+\(',usage):
        known=False
    used=len(whole|shared) if known else None
    if used is not None and (used>total or any(i>=total for i in whole|shared)):
        used=None
    free=total-used if used is not None else None
    blocked=bool(re.search(r'DRAIN|DOWN|FAIL|MAINT|NO_RESPOND|POWERED_DOWN',fields.get('State','')))
    return dict(total=total,used=used,free=free,whole=len(whole) if known else None,
                shared=len(shared) if known else None,blocked=blocked)

def timestamp(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError,TypeError):
        return None

def amount(tres):
    for kind in ('gpu','shard'):
        m=re.search(r'(?:^|,)gres/'+kind+r'(?:=[0-9]+|(?::[^:,=]+)*[:=][0-9]+)(?=,|$)',tres)
        if m:
            return kind,int(re.search(r'(\d+)$',m[0])[1])
    return 'cpu',0

_history=[]
_history_at=0
_history_lock=threading.Lock()
def history(now):
    global _history,_history_at
    with _history_lock:
        if now-_history_at<900:
            return _history
        rows=[]
        data=subprocess.run(['sacct','-a','-X','-n','-P','-S','now-30days','-o','Partition,Eligible,Start,ReqTRES,TimelimitRaw'],capture_output=True,text=True,timeout=7,check=True)
        for line in data.stdout.splitlines():
            values=line.rstrip('|').split('|')
            if len(values)!=5: continue
            part,eligible,start,tres,limit=values
            a,b=timestamp(eligible),timestamp(start)
            if a is None or b is None or not a<=b<=now or not limit.isdigit(): continue
            kind,n=amount(tres)
            rows.append((part,a,kind,n,int(limit),b-a))
        _history,_history_at=rows,now
        return rows

def queue_forecasts(rows,owner):
    now=time.time()
    mine=[j for j in rows if j['user']==owner and j['state']=='PENDING']
    if not mine:return []
    try:
        samples=history(now)
    except (OSError,subprocess.SubprocessError):
        samples=[]
    forecasts=[]
    for j in mine:
        # EligibleTime may differ from submission when a job had a dependency or hold.
        try:
            raw=subprocess.run(['scontrol','-o','show','job',j['id']],capture_output=True,text=True,timeout=2,check=True).stdout
            fields=dict(re.findall(r'(\w+)=([^\s]+)',raw))
        except (OSError,subprocess.SubprocessError):
            fields={}
        eligible=timestamp(fields.get('EligibleTime'))
        estimate=timestamp(j['expected'])
        base=dict(id=j['id'],name=j['name'],reason=j['reason'],scheduler=estimate,center=None,samples=0)
        forecasts.append(base)
        if eligible is None or eligible>now or any(key in j['reason'] for key in ('Dependency','Held','BeginTime')):
            base['note']='尚未满足调度条件或缺少可调度时间'
            continue
        kind,n=amount(fields.get('ReqTRES',j['gres']))
        age=now-eligible
        limit=j['limit'];days,_,clock=limit.rpartition('-')
        parts=clock.split(':')
        if not all(x.isdigit() for x in parts) or (days and not days.isdigit()):
            base['note']='申请时限不可解析'
            continue
        seconds=int(days or 0)*86400+sum(int(v)*60**i for i,v in enumerate(reversed(parts)))
        candidates=[]
        for part,a,k,num,mins,wait in samples:
            if part!=j['partition'] or k!=kind or num!=n or wait<age: continue
            hourdiff=abs(a-eligible)%86400
            distance=abs(math.log1p(mins)-math.log1p(seconds/60))+min(hourdiff,86400-hourdiff)/43200+(now-a)/(86400*30)
            candidates.append((distance,wait-age))
        near=sorted(candidates)[:30]
        base['samples']=len(near)
        if len(near)<5:
            base['note']='同分区、同卡数且等待够久的已启动样本不足 5 项'
            continue
        vals=sorted(w for _,w in near)
        base.update(center=now+vals[len(vals)//2],low=now+vals[int((len(vals)-1)*.25)],
                    high=now+vals[int((len(vals)-1)*.75)])
    return forecasts
