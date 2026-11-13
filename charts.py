"""Five-minute Slurm samples and personal trainer-state series for the dashboard."""
import json
import os
import re
import time
from bisect import bisect_right
from functools import lru_cache
from pathlib import Path
from monitor import CK_NAME, checkpoint_groups, expand_nodes, number, read_state, slurm
from insights import gpu_counts

HISTORY = Path(__file__).resolve().parents[2] / 'samples' / 'nodes.json'


def read_history():
    try:
        return json.loads(HISTORY.read_text())
    except FileNotFoundError:
        return []


def sample_nodes():
    nodes = {}
    for line in slurm('scontrol', '-d', '-o', 'show', 'node'):
        fields = dict(re.findall(r'(\w+)=([^\s]*)', line))
        gpu = gpu_counts(fields)
        nodes[fields['NodeName']] = dict(free=gpu['free'], total=gpu['total'], blocked=gpu['blocked'],
                                         state=fields.get('State'), jobs=[])
    for line in slurm('squeue', '-a', '-h', '-t', 'RUNNING,COMPLETING,SUSPENDED', '-o', '%i|%N'):
        job, hosts = line.split('|', 1)
        if hosts not in ('', '(null)'):
            for host in expand_nodes(hosts):
                if host in nodes:
                    nodes[host]['jobs'].append(job)
    now = int(time.time())
    history = [sample for sample in read_history() if now-86400 < sample['t'] < now]
    history.append(dict(t=now, nodes=nodes))
    tmp = HISTORY.with_suffix('.tmp')
    tmp.write_text(json.dumps(history,separators=(',', ':')))
    os.chmod(tmp, 0o640)
    tmp.replace(HISTORY)


@lru_cache(maxsize=120)
def state_points(path, stamp, size):
    if size>64*1024*1024:return []
    try:
        rows=json.loads(path.read_text()).get('log_history',[])
    except (ValueError,OSError):return []
    points={}
    for row in rows:
        if not isinstance(row,dict):continue
        step,epoch,loss=(number(row.get(k)) for k in ('step','epoch','loss'))
        if step is not None and loss is not None:points[step]=[step,epoch,loss]
    return sorted(points.values())


def checkpoint_evaluation(folder,group,step,safe):
    for filename in ('eval_results.json','evaluation.json'):
        result=read_state(folder/filename,safe)
        good=result.get('success_count',result.get('successes'))
        total=result.get('total_episodes',result.get('trials'))
        dataset=result.get('eval_dataset',result.get('dataset'))
        if type(total) is int and type(good) is int and 0<=good<=total and total>0 and isinstance(dataset,str) and dataset:
            return dict(dataset=dataset,success=good,total=total,step=step,path=str(folder),group=str(group))



def training_summary(home,safe):
    jobs=set()
    evaluations=[]
    for group,names in checkpoint_groups(home).items():
        manifest=read_state(group/'launch_configs/launch_manifest.json',safe)
        job=manifest.get('slurm_job_id')
        if job:jobs.add(str(job))
        for name in names:
            step=int(CK_NAME.fullmatch(name)[1])
            evaluation=checkpoint_evaluation(group/name,group,step,safe)
            if evaluation:evaluations.append(evaluation)
    return dict(jobs=sorted(jobs),evaluations=evaluations)


def training_charts(home,safe):
    curves=[]
    evaluations=[]
    for group,names in checkpoint_groups(home).items():
        saves=[]
        candidates=[]
        for folder in [group,*[group/n for n in names]]:
            try:
                path=safe(folder/'trainer_state.json')
                stat=path.stat()
                # Use the newest training state with a loss history for this output.
                candidates.append((stat.st_mtime_ns,path,stat.st_size))
            except (OSError,ValueError):pass
            if folder!=group and folder.is_dir():
                step=int(CK_NAME.fullmatch(folder.name)[1])
                saves.append(dict(step=step,path=str(folder)))
                evaluation=checkpoint_evaluation(folder,group,step,safe)
                if evaluation:evaluations.append(evaluation)
        for stamp,path,size in sorted(candidates,reverse=True):
            points=state_points(path,stamp,size)
            if not points:continue
            manifest=read_state(group/'launch_configs/launch_manifest.json',safe)
            data=manifest.get('dataset')
            steps=[p[0] for p in points]
            for ck in saves:
                index=bisect_right(steps,ck['step'])-1
                ck['point']=points[index] if index>=0 else None
                ck['epoch']=None
                if ck['point'] and ck['point'][0]==ck['step']:
                    ck['epoch']=ck['point'][1]
                else:
                    state=read_state(Path(ck['path'])/'trainer_state.json',safe)
                    if state.get('global_step')==ck['step']:ck['epoch']=number(state.get('epoch'))
            stride=max(1,(len(points)+599)//600)
            sample=points[::stride]
            if sample[-1]!=points[-1]:sample.append(points[-1])
            curves.append(dict(group=str(group),stamp=stamp//10**9,dataset=data if isinstance(data,str) else '',
                               job=str(manifest.get('slurm_job_id') or ''),points=sample,saves=saves))
            break
    return dict(curves=sorted(curves,key=lambda c:-c['stamp']),evaluations=evaluations)


if __name__=='__main__':
    sample_nodes()
