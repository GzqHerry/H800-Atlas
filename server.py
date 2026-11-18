import os, json, re, math, time, subprocess, socketserver, threading, csv, ast, shutil
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from collections import Counter
import nas
import grus
import charts
import swan_history
import wandb_runs
import job_artifacts
import training_eta
from datetime import datetime
from insights import DATASET_ROOTS
from monitor import lookup_command, cluster, checkpoint_index, checkpoint_detail, checkpoint_groups, number, read_state, scan_checkpoints
H=Path.home(); W=Path('/workspace')/H.name; D=Path('/data')/H.name; S=Path('/scratch')/H.name
ROOTS=[H,W,D,S,Path('/data/share/bodypipeline/runtime-'+H.name)]; SITE=Path(__file__).parent
MOUNTS={'home':H,'workspace':W,'data':D,'scratch':S}
def safe(value):
    p=Path(value).resolve(strict=True)
    if not any(p==r or r in p.parents for r in ROOTS): raise PermissionError('超出个人目录范围')
    return p
def tail(p,limit=1048576):
    with p.open('rb') as f:
        f.seek(max(0,f.seek(0,2)-limit));return f.read(limit).decode('utf-8','replace')
def files(q):
    path=safe(q.get('path',str(H))); term=q.get('q','').casefold()
    start=max(0,int(q.get('offset',0))); items=[]
    with os.scandir(path) as entries:
        for e in entries:
            if term not in e.name.casefold():continue
            try:
                st=e.stat(follow_symlinks=False); link=e.is_symlink()
                items.append(dict(name=e.name,path=str(path/e.name),kind='链接' if link else '目录' if e.is_dir(follow_symlinks=False) else '文件',bytes=None if e.is_dir(follow_symlinks=False) else st.st_size,mtime=int(st.st_mtime),directory=e.is_dir(),target=os.readlink(e.path) if link else ''))
            except OSError: continue
    items.sort(key=lambda e:(not e['directory'],e['name'].casefold()))
    return dict(path=str(path),items=items[start:start+120],total=len(items),roots=[str(p) for p in ROOTS if p.is_dir()])
def command(args):
    return subprocess.run(args,capture_output=True,text=True,timeout=10,check=True).stdout

def training_results():
    results={}
    for submission in (H/'training_launches').glob('*/submission.json'):
        launch=read_state(submission,safe)
        job=str(launch.get('job_id',''))
        out=launch.get('output_dir')
        if not job or not isinstance(out,str) or not Path(out).is_absolute():continue
        status=read_state(Path(out)/'allocation_guard/status.json',safe)
        if str(status.get('job_id'))==job:
            results[job]=status.get('phase')
        else:
            completion=read_state(Path(out)/'completion.json',safe)
            if str(completion.get('job_id'))==job and type(completion.get('exit_code')) is int:
                results[job]='holding_after_failure' if completion['exit_code'] else 'holding_after_success'
    return results

def jobs():
    cols='JobID,JobName%200,State,ExitCode,Elapsed,NodeList,Start,End,Submit,AllocTRES,TimelimitRaw'
    args=['sacct','-u',H.name,'-S','2000-01-01','-X','-n','-P','-o',cols]
    rows=[dict(zip(['id','name','state','exit','elapsed','node','start','end','submit','allocation','limitMinutes'],l.split('|'))) for l in command(args).splitlines() if l.strip()]
    try:
        queue=command(['squeue','--me','-h','-o','%i|%T|%R|%S|%b|%j|%M|%N|%V'])
        active={}
        for line in queue.splitlines():
            jid,state,reason,start,gpu,name,elapsed,node,submit=line.split('|',8)
            active[jid]=dict(id=jid,name=name,state=state,elapsed=elapsed,node=node,submit=submit,
                             reason=reason,estimate=start,requested=gpu)
        accounted={row['id'] for row in rows}
        for row in rows:
            row.update(active.get(row['id'],{}))
        rows.extend(v for k,v in active.items() if k not in accounted)
    except (OSError,subprocess.SubprocessError,ValueError):pass
    outcomes=training_results()
    for row in rows:
        if row['id'] in outcomes:row['trainingPhase']=outcomes[row['id']]
        for key in ('submit','start','end','estimate'):
            try:row[key+'Ts']=int(datetime.fromisoformat(row[key]).timestamp())
            except (KeyError,ValueError,OverflowError):row[key+'Ts']=None
    rows.sort(key=lambda j:int(re.match(r'\d+',j['id'])[0]),reverse=True);return rows
# Keep units and the actual logged horizontal coordinate with each independent series.
RUN_METRICS={
    'loss':'训练损失', 'learning_rate':'学习率', 'grad_norm':'梯度范数',
    'seconds_per_step':'每步耗时（秒）', 'action_loss':'动作损失', 'loss_rlt':'RLT 损失',
    'eval_loss':'验证损失', 'eval_accuracy':'验证准确率', 'loss_vla':'VLA 损失',
    'total_loss':'总损失', 'loss_rel_valid':'有效动作损失', 'loss_rel_pad':'填充部分损失',
    'train_samples_per_second':'训练吞吐（样本/秒）'
}
PROGRESS=re.compile(r'(\d+)\s*/\s*(\d+)\s*\[([^\]]*)\]')
SPEED=re.compile(r'(\d+(?:\.\d+)?)\s*(s/it|it/s)')
LOG_ERRORS={'Watchdog caught collective operation timeout':'NCCL 集合通信超时',
            'Invalid access of peer GPU memory':'CUDA 对端显存访问异常',
            'CUDA out of memory':'CUDA 显存不足','Disk quota exceeded':'存储配额写入失败'}

# Link by the run-specific path printed by this Slurm job, never by latest-run.
history_cache={}
# Track byte boundaries in append-only Slurm output. Metrics still read a bounded tail.
log_indexes={}
attempt_event=re.compile(rb'(?:^|[\r\n])(?:ALLOCATION_GUARD starting attempt (\d+)|ALLOCATION_GUARD attempt (\d+) exited (-?\d+)[^\r\n]*|wandb: Run data is saved locally in (/[^\r\n]+/run-[\w-]+)|Output dir:\s*([^\r\n]+))(?=[\r\n])')

def log_events(path):
    stat=path.stat()
    state=log_indexes.get(path)
    if not state or state['inode']!=stat.st_ino or stat.st_size<state['end'] or (stat.st_size==state['end'] and stat.st_mtime_ns!=state['mtime']):
        state=dict(inode=stat.st_ino,end=0,carry=b'',events=[],wandb={},swan={},swan_tail=b'',mtime=0)
        if len(log_indexes)>=32:log_indexes.pop(next(iter(log_indexes)))
        log_indexes[path]=state
    if stat.st_size==state['end']:return state['events']
    with path.open('rb') as f:
        f.seek(state['end'])
        while f.tell()<stat.st_size:
            base=f.tell()-len(state['carry'])
            chunk=f.read(min(524288,stat.st_size-f.tell()))
            if not chunk:break
            data=state['carry']+chunk
            swan_data=state['swan_tail']+chunk
            for at,name in swan_history.announcements(swan_data,f.tell()-len(swan_data)):
                state['swan'][at]=name
            state['swan_tail']=swan_data[-4096:]
            for m in attempt_event.finditer(data):
                args=tuple(v.decode('utf8','replace') if v else None for v in m.groups())
                state['events'].append((base+m.start(),*args))
            last=max(data.rfind(b'\r'),data.rfind(b'\n'))
            if last>=0:
                for marker in re.finditer(rb'wandb:[^\r\n]*',data[:last]):
                    for hint in wandb_runs.markers(marker[0]):
                        state['wandb'][base+marker.start()]=hint
            state['carry']=data[max(last,len(data)-8192,0):]
        state['end']=f.tell()
    state['mtime']=stat.st_mtime_ns
    return state['events']

def attempt_logs(paths,job):
    events={p:log_events(p) for p in paths}
    starts={int(n) for rows in events.values() for _,n,_,_,_,_ in rows if n}
    exits={int(n):int(code) for rows in events.values() for _,_,n,code,_,_ in rows if n}
    current=max(starts,default=0)
    phase='training' if current else None
    folders=[Path(out.strip()) for rows in events.values() for _,_,_,_,_,out in rows if out]
    guard=None
    for folder in reversed(folders):
        try:
            location=safe(folder/'allocation_guard/status.json')
            status=json.loads(location.read_text())
            if str(status.get('job_id'))==job and int(status['attempt'])>=current:
                current=int(status['attempt']);phase=status['phase'];guard=location.parent
                break
        except (OSError,ValueError,TypeError,KeyError):continue
    if current in exits:phase='holding_after_failure' if exits[current] else 'holding_after_success'
    runs=list(dict.fromkeys(Path(run) for rows in events.values() for _,_,_,_,run,_ in rows if run))
    run=runs[-1] if runs else None
    if current>1 and run:
        # A restart can fail before W&B initializes. Do not inherit its predecessor.
        try:
            boundary=safe(guard/f'restart.consumed.{current-1}').stat().st_mtime if guard else None
            meta=json.loads(safe(run/'files/wandb-metadata.json').read_text())
            if boundary is not None:
                if datetime.fromisoformat(meta['startedAt'].replace('Z','+00:00')).timestamp()<boundary:run=None
            elif len(runs)!=current:run=None
        except (OSError,ValueError,KeyError,TypeError):
            if len(runs)!=current:run=None
    offsets={}
    for p,rows in events.items():
        marks=[at for at,n,_,_,_,_ in rows if n and int(n)==current]
        run_marks=[at for at,_,_,_,name,_ in rows if name and Path(name)==run]
        if marks:offsets[p]=marks[-1]
        elif run_marks:offsets[p]=run_marks[-1]
        elif current<=1 and len(runs)<=1:offsets[p]=0
    if run and (len(runs)>1 or current>1) and not any(p.suffix=='.out' for p in offsets):
        try:offsets[safe(run/'files/output.log')]=0
        except OSError:pass
    attempt=dict(number=current,phase=phase,exitCode=exits.get(current),run=run.name.rsplit('-',1)[-1] if run else None,
                 previous=[dict(number=n,code=c) for n,c in sorted(exits.items()) if n<current])
    return offsets,run,attempt

def job_history(folder,job):
    if not folder:return {},None
    try:
        folder=safe(folder)
        runid=folder.name.rsplit('-',1)[-1]
        path=safe(folder/('run-'+runid+'.wandb'))
        if wandb_runs.identity(folder,safe) not in ('',job):return {},None
        stat=path.stat()
        state=history_cache.get(path)
        if not state or state['inode']!=stat.st_ino or stat.st_size<state['size']:
            state=dict(inode=stat.st_ino,size=-1,offset=0,metrics={})
            if len(history_cache)>=4:history_cache.pop(next(iter(history_cache)))
            history_cache[path]=state
        if state['size']!=stat.st_size:
            python=wandb_runs.READER
            if not python.exists():return {},None
            result=subprocess.run([str(python),'-I','-B',str(SITE/'wandb_history.py'),str(path),
                                   str(state['offset'])],capture_output=True,timeout=25,check=True)
            offset,rows=json.loads(result.stdout)
            for step,row in rows:
                for key,value in row.items():state['metrics'].setdefault(key,{})[step]=value
            state.update(offset=offset,size=stat.st_size)
        return state['metrics'],str(path)
    except (ValueError,OSError,subprocess.SubprocessError):
        return {},None

def swan_run(offsets):
    for path, start in offsets.items():
        markers=log_indexes.get(path,{}).get('swan',{})
        current=[at for at in markers if at>=start]
        if current:
            folder=Path(markers[max(current)])
            run_id=folder.name.rsplit('-',1)[-1]
            folders=[folder]
            # Some containers mount the user's workspace directly at /workspace.
            for root in (W,D,S):
                if folder.is_relative_to(root.parent) and not folder.is_relative_to(root):
                    folders.append(root/folder.relative_to(root.parent))
            for candidate in folders:
                try:
                    store=safe(candidate/f'run-{run_id}.swanlab')
                    return run_id,swan_history.history(store),str(store)
                except (OSError,ValueError,RuntimeError):
                    continue
            return run_id,{},None
    return None,{},None

EPOCH_STEPS = re.compile(r'''(?i)["']?(?:steps_per_epoch|steps per epoch)["']?\s*[:=]\s*([1-9]\d*)\b''')

def epoch_scale(progress, steps_per_epoch, paired):
    if not progress:
        return
    estimated = False
    if not steps_per_epoch:
        # Fractional logged epochs may be rounded. Require two agreeing
        # step/epoch pairs before extrapolating a full training schedule.
        samples = sorted((s,e) for s,e in paired.items() if s>0 and e>=0.1 and e%1)
        if len(samples)<2:
            return
        first, last = samples[(len(samples)-1)//2], samples[-1]
        rate = last[0]/last[1]
        if first[0]>=last[0] or first[1]>=last[1] or abs(first[0]/first[1]-rate)>0.03*rate:
            return
        steps_per_epoch = max(1,round(rate))
        estimated = True
    progress.update(stepsPerEpoch=steps_per_epoch,epochEstimated=estimated)

def openpi_export(paths,job):
    # The bootstrap log and attempts/<Slurm ID> belong to the same launch.
    # A project can also contain older, unscoped status files.
    for log in paths:
        folder=log.parent/'attempts'/job
        try:
            path=safe(folder/'status/rank0-metrics.jsonl')
        except (OSError,ValueError):
            continue
        return path,folder
    return None,None


openpi_cache={}
OPENPI_LABELS={'param_norm':'参数范数','rlt_reconstruction_loss':'RLT 重建损失','velocity_loss':'速度损失'}

def openpi_history(path):
    # rsync may atomically replace the file. Never carry an offset across inodes.
    with path.open('rb') as stream:
        st=os.fstat(stream.fileno())
        state=openpi_cache.get(path)
        if (not state or state['inode']!=st.st_ino or st.st_size<state['size']
                or (st.st_size==state['size'] and st.st_mtime_ns!=state['mtime'])):
            if len(openpi_cache)>=4:openpi_cache.pop(next(iter(openpi_cache)))
            state=dict(inode=st.st_ino,offset=0,values={})
            openpi_cache[path]=state
        state.update(size=st.st_size,mtime=st.st_mtime_ns)
        stream.seek(state['offset'])
        for line in stream:
            if not line.endswith(b'\n'):break
            state['offset']=stream.tell()
            try:row=json.loads(line)
            except (ValueError,UnicodeError):continue
            if not isinstance(row,dict):continue
            step=row.get('step')
            if type(step)!=int or step<0 or not isinstance(row.get('metrics'),dict):continue
            for key,val in row['metrics'].items():
                if key not in RUN_METRICS and key not in OPENPI_LABELS and not re.fullmatch(r'joint_action_loss/[a-zA-Z0-9_/.-]{1,80}',key):continue
                if number(val) is not None:state['values'].setdefault(key,{})[step]=val
        return state['values']


def read_run_metrics(offsets, focus=None, history=None, swan=None, openpi=None, artifacts=None):
    history=history or {}
    series={}
    evidence=[]
    progress=None
    latest_epoch=None
    steps_per_epoch=None
    paired={}
    truncated=False
    for p,offset in offsets.items():
        size=max(0,p.stat().st_size-offset)
        truncated |= size>2000000
        with p.open('rb') as f:
            f.seek(offset+max(0,size-2000000));text=f.read(min(size,2000000)).decode('utf8','replace')
            if size>2000000:
                f.seek(offset)
                head=f.read(min(size,131072)).decode('utf8','replace')
            else:
                head=''
        for match in EPOCH_STEPS.finditer(head+'\n'+text):
            steps_per_epoch=int(match[1])
        if size>2000000:text=re.sub(r'^[^\r\n]*','',text,count=1)
        for line in text.splitlines():
            for marker,label in LOG_ERRORS.items():
                if marker in line and label not in evidence:evidence.append(label)
            match=PROGRESS.search(line)
            if match:
                step,total=map(int,match.group(1,2))
                if total and 0<=step<=total:
                    progress=dict(step=step,total=total)
                    speed=SPEED.search(match[3])
                    if speed and float(speed[1])>0:
                        value=float(speed[1]) if speed[2]=='s/it' else 1/float(speed[1])
                        series.setdefault(('seconds_per_step','step'),{})[step]=value
            start,end=line.find('{'),line.rfind('}')
            if start<0 or end<=start or end-start>16000:continue
            raw=line[start:end+1]
            try:
                try:row=json.loads(raw)
                except ValueError:row=ast.literal_eval(raw)
            except (ValueError,SyntaxError,RecursionError):continue
            if not isinstance(row,dict):continue
            step=number(row.get('global_step',row.get('step')))
            epoch=number(row.get('epoch'))
            if epoch is not None:latest_epoch=epoch
            if epoch is not None and step is not None:paired[step]=epoch
            axis='step' if step is not None else 'epoch' if epoch is not None else 'sample'
            x=step if step is not None else epoch
            for key in RUN_METRICS:
                value=number(row.get(key))
                if value is None:continue
                points=series.setdefault((key,axis),{} if axis=='step' else [])
                if axis=='step':points[x]=value
                else:points.append((x if x is not None else len(points)+1,value))
    sources={}
    labels=dict(RUN_METRICS)
    for key, values in (artifacts or {}).items():
        tracker, _, tag = key.partition('/')
        label = tag
        for suffix, title in (('Environment/reward', '环境奖励'), ('env/reward', '环境奖励'),
                              ('Evaluation/reward', '评估奖励'), ('eval/reward', '评估奖励')):
            if tag == suffix or tag.endswith('/' + suffix):
                label = title + ' · ' + tracker
                break
        labels[key] = label
        sources[key] = tracker
        series[(key, 'step')] = values
    for key,values in (openpi or {}).items():
        labels[key]=labels.get(key,OPENPI_LABELS.get(key,key))
        series[(key,'step')]=dict(values)
        sources[key]='OpenPI · rank 0'
    for key,values in (swan or {}).items():
        short=key.removeprefix('train/')
        label={'lr':'学习率','accuracy':'训练准确率','accuracy_neighbor':'邻近准确率','time/step':'每步耗时（秒）'}.get(short)
        labels[key]=label or RUN_METRICS.get(short,key)
        series[(key,'step')]=values
        sources[key]='SwanLab · step'
    for key,values in history.items():
        if values:
            if key!='epoch':labels.setdefault(key,key)
            for axis in ('epoch','sample'):
                series.pop((key,axis),None)
            series.setdefault((key,'step'),{}).update(values)
            sources[key]='W&B · step'
    metrics=[]
    expanded=None
    for key,label in labels.items():
        for axis in ('step','epoch','sample'):
            data=series.get((key,axis))
            if not data:continue
            points=sorted(data.items()) if isinstance(data,dict) else data
            # Rounded epochs can collapse distinct measurements onto one x pixel.
            if axis=='epoch' and any(a[0]>=b[0] for a,b in zip(points,points[1:])):
                axis='sample'
                points=[(i,v) for i,(_,v) in enumerate(points,1)]
            source=sources.get(key,'作业日志') if axis=='step' else '作业日志'
            if focus==(key,axis):
                expanded=dict(key=key,label=label,axis=axis,source=source,points=points)
            sampled=points[::max(1,math.ceil(len(points)/700))]
            if sampled[-1]!=points[-1]:sampled.append(points[-1])
            metrics.append(dict(key=key,label=label,axis=axis,source=source,points=sampled))
    paired.update(history.get('epoch',{}))
    if paired:
        latest_epoch=paired[max(paired)]
    epoch_scale(progress,steps_per_epoch,paired)
    return dict(metrics=metrics,expanded=expanded,progress=progress,epoch=latest_epoch,evidence=evidence,truncated=truncated)

def personal_job(job):
    if not isinstance(job,str) or not re.fullmatch(r'[0-9]+(?:_[0-9]+)?',job):raise ValueError('作业号无效')
    raw=command(['sacct','-u',H.name,'-j',job,'-X','-n','-P','-o','JobID,JobName%200,State,ExitCode,Elapsed,WorkDir,StdOut,StdErr'])
    rows=[r.split('|') for r in raw.splitlines() if r.split('|')[0]==job]
    if rows:
        _,name,state,code,elapsed,workdir,*paths=rows[0]
    else:
        live=command(['squeue','--me','-j',job,'-h','-o','%i|%j|%T|%M|%o'])
        row=next((s.split('|',4) for s in live.splitlines() if s.split('|',1)[0]==job),None)
        if not row:raise ValueError('未找到个人作业')
        _,name,state,elapsed,_=row
        fields=command(['scontrol','show','job','-o',job])
        workdir=next(iter(re.findall(r'\bWorkDir=(\S+)',fields)),'')
        paths=re.findall(r'\bStd(?:Out|Err)=(\S+)',fields)
        code=''
    return name,state,code,elapsed,workdir,paths

def detail(job,focus=None):
    name,state,code,elapsed,workdir,paths=personal_job(job)
    matches=[]
    candidates=[s.replace('%j',job).replace('%x',name).replace('%A',job.split('_')[0]).replace('%a',job.split('_')[-1]) for s in paths if s]
    for ext in ('out','err'):
        candidates.extend((H/'training_launches').glob('*/logs/*-'+job+'.'+ext))
    for s in candidates:
        try:
            p=safe(s)
            if p.is_file() and p not in matches:matches.append(p)
        except (OSError,ValueError):pass
    offsets,folder,attempt=attempt_logs(matches,job)
    discovery=''
    bases=[Path(workdir)] if Path(workdir).is_absolute() else []
    bases.extend(p.parent for p in matches)
    hints=[hint for p,offset in offsets.items()
           for pos,hint in log_indexes.get(p,{}).get('wandb',{}).items() if pos>=offset]
    # Guarded restarts must retain the existing attempt boundary check.
    if attempt['number']>1:
        if folder:hints=[str(folder)]
        else:
            hints=[]
            for p in matches:
                marks=[at for at,n,*_ in log_indexes[p]['events'] if n and int(n)==attempt['number']]
                if marks:hints.extend(v for at,v in log_indexes[p]['wandb'].items() if at>=marks[-1])
    elif folder and not hints:hints=[str(folder)]
    auxiliary=bool(re.search(r'(?:^|[-_])(?:wandb[-_]sync|sync[-_]network[-_]check)$',name,re.I))
    choices=[]
    try:saved=json.loads(wandb_runs.binding_file(H,job).read_text())
    except (OSError,ValueError):saved=None
    if saved:
        try:
            folder=wandb_runs.validate_path(saved,job,safe)
            discovery='使用为此作业保存的 W&B run。'
        except (OSError,ValueError,RuntimeError):
            folder=None
            discovery='已关联的 W&B run 暂不可读，可以更改关联。'
    elif not auxiliary:
        folder,discovery,choices=wandb_runs.resolve(job,hints,bases,[H,W,D,S],safe,allow_index=attempt['number']<=1)
    else:folder=None
    attempt['run']=folder.name.rsplit('-',1)[-1] if folder else None
    if folder:attempt['tracker']='W&B'
    history,history_path=job_history(folder,job)
    swan_id,swan,swan_path=swan_run(offsets)
    if swan_id and not saved:
        attempt.update(run=swan_id,tracker='SwanLab')
    export,export_dir=openpi_export(matches,job)
    openpi={}
    if export:
        try:openpi=openpi_history(export)
        except OSError:pass
    artifacts, artifact_paths, artifact_errors = job_artifacts.collect(job, bases, safe, attempt['number'])
    result=read_run_metrics(offsets,focus,history,swan,openpi,artifacts)
    if artifacts and not choices and not saved and not folder:
        discovery='已读取按作业号匹配的训练指标，也可另外关联 W&B。'
    if saved or (not auxiliary and not folder and not swan_id):
        result['wandbLink']=dict(saved=saved,candidates=choices,note=discovery)
    if openpi and not result['progress']:
        config=read_state(export_dir/'training-config.json',safe)
        status=read_state(export_dir/'status/rank0-status.json',safe)
        total=config.get('num_train_steps')
        step=max(max(values) for values in openpi.values())
        updates=status.get('completed_updates')
        if type(updates)==int and type(total)==int and updates<=total:step=max(step,updates)
        if type(total)==int and 0<=step<=total and total>0:
            result['progress']=dict(step=step,total=total)
            per_epoch=config.get('steps_per_epoch')
            if type(per_epoch)==int and per_epoch>0:epoch_scale(result['progress'],per_epoch,{})
    if state=='RUNNING':
        try:
            f=training_eta.job_records(job).get(job)
            if f and training_eta.account(f)[1]==os.getuid():
                result['trainingEta']=training_eta.estimate(job,f,H.name,os.getuid(),int(time.time()),training_eta.known_progress)
        except (OSError,ValueError,KeyError,subprocess.SubprocessError):pass
    evidence=result.pop('evidence')
    summary=f'作业 {job} · {state} · 运行 {elapsed}'
    if code and state not in ('RUNNING','PENDING','COMPLETING','CONFIGURING'):summary+=f' · 退出码 {code}'
    if evidence:summary+=' · '+'、'.join(evidence)
    loss=next((m['points'] for m in result['metrics'] if m['key']=='loss' and m['axis']=='epoch'),[])
    source=[str(p) for p in offsets]+([history_path] if history_path and history else [])
    source.extend(artifact_paths)
    note='各日志仅取末尾 2 MB。' if result['truncated'] else '读取当前作业的标准输出与错误日志。'
    if discovery:note+=' '+discovery
    if artifact_paths:note+=' TensorBoard 和 metrics.log 曲线各自沿用原始 step。'
    if artifact_errors:note+=' 暂未能读取指标文件：'+'、'.join(artifact_errors)+'。'
    if folder and not history:note+=' 已定位 run，暂未读到可绘制的本地标量。'
    if openpi:
        source.append(str(export))
        note+=' OpenPI 曲线取自该作业单独导出的 rank 0 原始标量。'
    if not result['metrics'] and re.search(r'(?:^|[-_])(?:wandb[-_]sync|sync[-_]network[-_]check)$',name,re.I):
        result['emptyReason']='这是 W&B 同步或连通性检查作业。请在左侧选择启动训练的作业查看曲线。'
    if swan_path:
        source.append(swan_path)
        note+=' SwanLab 指标取自该实验已写入的标量记录。'
    elif swan_id:
        note+=' 已识别 SwanLab 实验，尚未读到支持的本地记录。'
    return dict(result,summary=summary,attempt=attempt,points=loss,source=source,sampled=int(time.time()),note=note)

def profile_inventory():
    groups=checkpoint_groups(H)
    return dict(checkpoints=sum(map(len,groups.values())),tasks=len(groups),stamp=int(time.time()))

def assets():
    inv=H/'nas-archive-20261112/inventory.json'; ck=[]
    if inv.exists():
        d=json.loads(inv.read_text())['checkpoint_dirs']
        for p,v in d.items():
            if re.fullmatch(r'(checkpoint-\d+|global_step_\d+|\d+)',Path(p).name):
                ck.append(dict(name=Path(p).name,path=p,bytes=v[1],present=Path(p).is_dir()))
    archive=S/'nas-archive-20261112/events.jsonl'
    return dict(ck=ck,stamp=int(inv.stat().st_mtime) if inv.exists() else None,archive=[json.loads(i) for i in tail(archive,50000).splitlines()[-2:] if i.startswith('{')] if archive.exists() else [])
size_lock=threading.Lock()
size_cache={}
def measure(q):
    p=Path(q.get('path','')).resolve(strict=True)
    if not any(r in p.parents for r in DATASET_ROOTS):p=safe(p)
    if p in ROOTS or not p.is_dir(): raise ValueError('Choose an asset directory')
    now=time.time()
    if p in size_cache and now-size_cache[p]['stamp']<3600:return size_cache[p]
    if not size_lock.acquire(False):return dict(bytes=None,note='另一项统计正在进行，请稍后重试')
    try:
        out=subprocess.run(['nice','-n','15','du','-sx','-B1','--',str(p)],capture_output=True,text=True,timeout=12,check=True)
        result=dict(bytes=int(out.stdout.split()[0]),stamp=int(now),note='目录实际占用，统计结果缓存一小时')
        size_cache[p]=result
        return result
    except (subprocess.SubprocessError,OSError):return dict(bytes=None,note='扫描超时或部分目录不可读')
    finally:size_lock.release()
HOME_QUOTA_BYTES = 30 * 1024**3

def home_capacity():
    used = None
    try:
        # This H800 NFS export reports recursive logical bytes in directory st_size.
        # Ordinary Linux directories do not, so only use it on the verified export.
        with open('/proc/self/mountinfo') as mounts:
            for line in mounts:
                mount, fs = line.rstrip().split(' - ', 1)
                if mount.split()[4] == str(H.parent) and fs.split()[:2] == ['nfs4', '1.1.1.253:/home']:
                    used = H.stat().st_size
                    break
    except OSError:
        pass
    free = max(0, HOME_QUOTA_BYTES - used) if used is not None else None
    return dict(name='home', scope='personal', total=HOME_QUOTA_BYTES, used=used, free=free)

def overview():
    mounts=[]
    for k,p in MOUNTS.items():
        if k == 'home':
            mounts.append(home_capacity())
            continue
        if p.is_dir():
            u=shutil.disk_usage(p);mounts.append(dict(name=k,used=u.used,total=u.total,free=u.free))
    return dict(mounts=mounts,stamp=int(time.time()),user=H.name,home=str(H),checkpointUploadEnabled=nas.enabled(),roots=[str(p) for p in ROOTS if p.is_dir()],host=os.uname().nodename,zone=time.strftime('%Z'))
def checkpoint_sources():
    rows={}
    for parent,names in checkpoint_groups(H).items():
        for name,size in names.items():
            path=parent/name
            if path.is_dir():
                rows[str(path)]=dict(path=str(path),group=str(parent),name=name,present=True,bytes=size,localUpdated=int(path.stat().st_mtime),updated=int(path.stat().st_mtime),copies=[])
    note=''
    try:
        if nas.enabled():copies=nas.remote('checkpoints')
        else:copies=[];note='此账号尚未配置 NAS'
    except (OSError,ValueError,subprocess.SubprocessError):
        copies=[];note='NAS 暂时无法读取'
    for c in copies:
        path=Path(c['source'])
        if not any(r in path.parents for r in ROOTS):continue
        row=rows.setdefault(str(path),dict(path=str(path),group=str(path.parent),name=path.name,present=False,bytes=c['bytes'],updated=c['updated'],copies=[]))
        row['copies'].append(c)
        row['updated']=max(row['updated'],c['updated'])
    try:
        remote = grus.checkpoints()
    except (OSError, ValueError, subprocess.SubprocessError):
        remote = {'items': []}
        note = ' · '.join(filter(None, (note, '当前账号暂时无法读取 Grus')))
    if remote.get('partial'):
        note = ' · '.join(filter(None, (note, '部分 Grus 目录不可读取或缺少推理文件')))
    parents = {}
    for row in rows.values():
        parent = Path(row['group'])
        parents.setdefault(parent.name, set()).add(parent)
    for copy in remote['items']:
        destination = Path(copy['destination'])
        candidates = parents.get(destination.parent.name, set()) if destination.parent != Path('/mnt/data0/arx_ckpts') else set()
        if len(candidates) == 1:
            group = next(iter(candidates))
        else:
            group = destination.parent
        key = str(group / destination.name)
        row = rows.setdefault(key, dict(path=key, group=str(group), name=destination.name,
                                        present=False, bytes=copy['bytes'], updated=copy['updated'], copies=[]))
        row['grus'] = copy
        row['updated'] = max(row['updated'], copy['updated'])
    return dict(items=list(rows.values()),note=note)

def route(method, target, body):
    if method == 'GET' and urlsplit(target).path == '/api/cpu-live':
        from cpu_usage import live_cpu_sample
        return 200, live_cpu_sample()
    if method == 'GET' and urlsplit(target).path == '/api/gpu-live':
        from gpu_usage import live_gpu_sample
        return 200, live_gpu_sample()
    u=urlsplit(target)
    q={k:v[-1] for k,v in parse_qs(u.query).items()}
    if method=='POST' and u.path=='/api/wandb-link':
        job=body.get('job')
        try:
            personal_job(job)
            folder=wandb_runs.validate_path(body.get('path'),job,safe)
            wandb_runs.save_binding(wandb_runs.binding_file(H,job),folder)
        except FileNotFoundError:return 404,dict(error='路径不存在，或目录中缺少对应的 .wandb 文件')
        except PermissionError:return 403,dict(error='当前账号无法读取该路径或保存关联')
        except (ValueError,RuntimeError) as exc:return 400,dict(error=str(exc))
        return 200,dict(path=str(folder))
    if method=='POST' and u.path=='/api/checkpoint-scan':
        try:result=scan_checkpoints(H,body.get('path'))
        except FileNotFoundError:return 404,dict(error='目录不存在，请检查 H800 路径')
        except PermissionError:return 403,dict(error='当前 H800 账号无权读取该目录')
        except ValueError as exc:return 400,dict(error=str(exc))
        return 200,dict(result,inventory=profile_inventory())
    if method=='POST' and u.path in ('/api/upload','/api/remove-local'):
        destination=body.get('destination','NAS') if u.path=='/api/upload' else 'NAS'
        if destination not in ('NAS','Grus','Tianji'):return 400,{'error':'未知的上传目标'}
        if destination=='NAS' and not nas.enabled(): raise PermissionError('此账号尚未配置 NAS')
        path=Path(body.get('path',''))
        if path.name not in checkpoint_groups(H).get(path.parent,{}):
            return 400,{'error':'请选择已登记的 H800 checkpoint'}
        source=safe(path)
        if not source.is_dir() or path.is_symlink():
            return 400,{'error':'本地 checkpoint 目录不可用'}
        action='remove' if u.path=='/api/remove-local' else 'upload'
        if destination in ('Grus','Tianji'):action='upload-'+destination.lower()
        if action=='remove' and not any(c['verified'] and c['source']==str(path) for c in nas.remote('checkpoints')):
            return 409,{'error':'未找到已校验的 NAS 副本'}
        grant=body.get('tianjiGrant') if action=='upload-tianji' else None
        if action=='upload-tianji' and not re.fullmatch('[0-9a-f]{32}',grant or ''):
            return 403,dict(error='请通过 Atlas 登录后提交 Tianji 上传')
        return 202,nas.enqueue(dict(name=path.name,group=str(path.parent),path=str(path),resolved=str(source),kind='checkpoint'),action,grant)
    routes={'/api/job-command':lambda:lookup_command(q.get('id','')),'/api/nas':nas.catalog,'/api/uploads':nas.job_list,'/api/cluster':cluster,'/api/checkpoint-groups':lambda:checkpoint_index(H),'/api/checkpoints':lambda:checkpoint_detail(H,q.get('group',''),safe),'/api/size':lambda:measure(q),'/api/overview':overview,'/api/jobs':jobs,'/api/files':lambda:files(q),'/api/detail':lambda:detail(q.get('id',''),(q.get('metric'),q.get('axis')))}
    routes['/api/checkpoint-sources']=checkpoint_sources
    routes['/api/training-summary']=lambda:charts.training_summary(H,safe)
    routes['/api/profile-inventory']=profile_inventory
    if method=='GET' and u.path in ('/api/training-usage','/api/gpu-usage'):
        days=int(q.get('days','7'))
        gpu_only=u.path=='/api/gpu-usage'
        outcomes={}
        if not gpu_only:
            outcomes=body.get('outcomes',{})
            outcomes.setdefault(H.name,{}).update(training_results())
        rows=charts.usage_rows(charts.usage_lines(days,int(time.monotonic()//300)),outcomes,gpu_only=gpu_only)
        return 200,dict(users=rows,stamp=int(time.time()),days=days)
    routes['/api/training-charts']=lambda:charts.training_charts(H,safe)
    routes['/api/node-history']=charts.read_history
    routes['/api/assets']=assets
    if method!='GET' or u.path not in routes:return 404,{'error':'接口不存在'}
    return 200,routes[u.path]()

def serve_worker():
    # This process runs inside the authenticated user's sshd session.
    import sys
    print(json.dumps({'ready':True,'user':H.name,'uid':os.getuid()}),flush=True)
    for line in sys.stdin:
        try:
            req=json.loads(line)
            status,value=route(req['method'],req['target'],req['body'])
            reply=json.dumps([status,value],allow_nan=False)
        except PermissionError:reply=json.dumps([403,{'error':'无权访问该目录或资源'}])
        except (FileNotFoundError,OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
            reply=json.dumps([400,{'error':'资源不可用或请求无效'}])
        print(reply,flush=True)


if __name__=='__main__':
    os.umask(0o077)
    serve_worker()
