"""Per-job training ETA from recent iteration measurements in Slurm output."""
import json
import math
import os
import re
import stat
import subprocess
import time
from collections import deque
from datetime import datetime

ANSI = re.compile(rb'\x1b\[[0-9;]*[A-Za-z]')
BAR = re.compile(rb'(\d+)%\|[^\r\n|]*\|\s*(\d+)\s*/\s*(\d+)\s*\[([\d:]+)(?:<[^,\]]*)?,\s*([\d.]+)\s*(s/it|it/s)')
GUARD = re.compile(rb'ALLOCATION_GUARD (?:starting attempt (\d+)|attempt (\d+) exited (-?\d+))')
TAIL = 256 * 1024
known_progress = {}


def job_records(job=None):
    raw = subprocess.check_output(['scontrol','show','job','-o',*([job] if job else [])],timeout=5,text=True)
    records = {}
    for line in raw.splitlines():
        f = dict(re.findall(r'\b(JobId|ArrayJobId|ArrayTaskId|UserId|JobName|JobState|StartTime|StdOut|StdErr|Command|ReqTRES|AllocTRES)=(\S+)',line))
        if 'JobId' not in f:continue
        key = f['JobId']
        if f.get('ArrayTaskId','').isdigit():
            key = f['ArrayJobId']+'_'+f['ArrayTaskId']
        records[key] = f
    return records


def read_tail(path, uid):
    # Only regular job-owner files; never block on pipes or read another UID's file.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as f:
        st = os.fstat(fd)
        if st.st_uid != uid or not stat.S_ISREG(st.st_mode):
            raise ValueError('not an owned job log')
        f.seek(max(0,st.st_size-TAIL))
        return ANSI.sub(b'',f.read(TAIL)), int(st.st_mtime)


def elapsed_seconds(value):
    result=0
    for part in value.split(b':'):
        result=result*60+int(part)
    return result


def points(text, train):
    window=deque(maxlen=31)
    for line in text.splitlines():
        match=BAR.search(line)
        if match:
            prefix=line[:match.start()].strip().lower()
            if not train and b'train' not in prefix and b'global step' not in prefix:
                continue
            if prefix and not any(k in prefix for k in (b'train',b'global step')):
                continue
            _,step,total,clock,speed,unit=match.groups()
            if len(speed)>20 or len(step)>16 or len(total)>16 or not re.fullmatch(rb'\d{1,6}(?::\d{1,2}){1,2}',clock):
                continue
            try: rate=float(speed)
            except ValueError:continue
            if rate<=0 or not math.isfinite(rate):continue
            step,total,elapsed=int(step),int(total),elapsed_seconds(clock)
            seconds=rate if unit==b's/it' else 1/rate
        elif train and b'{' in line and any(k in line for k in (b'"total_steps"',b'"max_steps"')):
            try:
                row=json.loads(line[line.index(b'{'):])
                total=row.get('total_steps',row.get('max_steps'))
                step=row.get('global_step',row.get('current_steps',row.get('step')))
                seconds=row.get('seconds_per_step',row.get('time/step'))
                elapsed=row.get('elapsed_seconds')
                if type(step)!=int or type(total)!=int:continue
                if type(seconds) not in (int,float) or not 0<seconds<86400:seconds=None
                if type(elapsed) not in (int,float) or not math.isfinite(elapsed):elapsed=None
            except (ValueError,AttributeError):continue
        else:continue
        if not 0<=step<=total or total<=0:continue
        if window:
            old=window[-1]
            if step<old[0] or total!=old[1] or (elapsed is not None and old[2] is not None and elapsed<old[2]):
                window.clear()
            elif step==old[0]:
                window.pop()
        window.append((step,total,elapsed,seconds))
    return window


def estimate(job, f, user, uid, now, memory):
    logs=[]
    for path in dict.fromkeys([f.get('StdOut'),f.get('StdErr')]):
        if not path or not os.path.isabs(path):continue
        try:logs.append(read_tail(path,uid))
        except (ValueError,OSError):continue
    if not logs:return {'state':'unavailable'}
    identity=(user,job,f.get('StartTime'))
    start=datetime.fromisoformat(f['StartTime']).timestamp()
    logs=[(t,mtime) for t,mtime in logs if mtime>=start-1]
    train=bool(re.search(r'train|sft|finetun|pretrain|enactive',f.get('JobName','')+' '+f.get('Command',''),re.I))
    train=train or any(re.search(rb"""train/loss|['"](?:grad_norm|learning_rate)['"]""",t) for t,_ in logs)
    attempts=[m for t,_ in logs for m in GUARD.finditer(t)]
    attempt=max((int(m[1] or m[2]) for m in attempts),default=0)
    exits=[m for m in attempts if m[2] and int(m[2])==attempt]
    if exits:return {'state':'stopped' if int(exits[-1][3]) else 'finished'}
    candidates=[]
    for text,mtime in logs:
        starts=[m.end() for m in GUARD.finditer(text) if m[1]]
        if starts:text=text[starts[-1]:]
        elif attempt>1:
            # stderr may contain both attempts. The newest run announcement
            # makes that boundary explicit; otherwise require observed progress.
            boundary=text.rfind(b'wandb: Run data is saved locally in')
            if boundary>=0:text=text[boundary:]
            probe=points(text,train)
            if not probe:continue
            key=identity+('boundary',)
            if memory.get(key,(0,))[0]!=attempt:
                memory[key]=(attempt,probe[-1][0])
                continue
            if probe[-1][0]<memory[key][1]:
                memory[key]=(attempt,probe[-1][0])
            if probe[-1][0]<=memory[key][1]:continue
        window=points(text,train)
        if window:candidates.append((mtime,window))
    if not candidates:return {'state':'waiting'}
    stamp,window=max(candidates,key=lambda item:item[0])
    step,total,last_elapsed,_=window[-1]
    old=memory.get(identity,{})
    observed=old.get('observed',[])
    if total!=old.get('total') or attempt!=old.get('attempt') or step<old.get('step',0):
        observed=[]
    if old.get('step')==step and old.get('total')==total and old.get('attempt')==attempt:
        stamp=old['stamp']
    elif not observed or stamp>observed[-1][1]:
        observed=(observed+[(step,stamp)])[-8:]
    memory[identity]=dict(step=step,total=total,stamp=stamp,attempt=attempt,observed=observed)
    base=dict(step=step,total=total,updated=stamp)
    if step==total:return dict(base,state='finished')
    first=window[0]
    if len(window)>=3 and last_elapsed is not None and first[2] is not None and last_elapsed>first[2]:
        rate=(last_elapsed-first[2])/(step-first[0])
    else:
        rates=[p[3] for p in window if p[3]]
        if len(rates)>=3:rate=sum(rates)/len(rates)
        elif len(observed)>=3:
            rate=(observed[-1][1]-observed[0][1])/(observed[-1][0]-observed[0][0])
        else:return dict(base,state='warming')
    if not 0<rate<86400:return dict(base,state='warming')
    finish=round(stamp+(total-step)*rate)
    if now-stamp>max(120,min(1800,5*rate)) or finish<=now:
        return dict(base,state='stale')
    return dict(base,state='ready',finish=finish,secondsPerStep=round(rate,4))


def account(f):
    m=re.fullmatch(r'([\w.-]+)\((\d+)\)',f.get('UserId',''))
    if not m:raise ValueError('unknown uid')
    return m[1],int(m[2])


def attach(jobs):
    now=int(time.time())
    try:records=job_records()
    except (OSError,subprocess.SubprocessError):return
    active=set()
    for job in jobs:
        f=records.get(job['id'])
        if not f:continue
        try:
            user,uid=account(f)
            if user!=job['user']:continue
            resource_key='ReqTRES' if job['state']=='PENDING' else 'AllocTRES'
            resources=dict(re.findall(r'([^,=]+)=([^,=]+)',f.get(resource_key,'')))
            if resources:
                def count_gres(key):
                    if key in resources:return int(resources[key])
                    return sum(int(v) for k,v in resources.items() if k.startswith(key+':'))
                job['resources']=dict(gpu=count_gres('gres/gpu'),shard=count_gres('gres/shard'),
                                      cpu=resources.get('cpu'),mem=resources.get('mem'),nodes=resources.get('node'))
            if uid!=os.getuid() or job['state']!='RUNNING' or f['JobState']!='RUNNING':continue
            active.add((user,job['id'],f['StartTime']))
            job['trainingEta']=estimate(job['id'],f,user,uid,now,known_progress)
        except (KeyError,OSError,ValueError,OverflowError,TypeError):continue
    for k in list(known_progress):
        if k[:3] not in active:known_progress.pop(k)
