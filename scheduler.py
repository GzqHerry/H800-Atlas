"""Read-only Slurm policy, launch-script evidence and observable process arguments."""
import os
import re
import shlex
import socket
import stat
import subprocess
import time
from pathlib import Path

def query(args):
    try:
        p=subprocess.run(args,capture_output=True,text=True,timeout=6,check=True)
        return p.stdout
    except (OSError,subprocess.SubprocessError):
        return ''

_policy={}
def policy():
    now=time.time()
    if now-_policy.get('stamp',0)<60:return _policy
    conf={}
    for line in query(['scontrol','show','config']).splitlines():
        k,sep,v=line.partition('=')
        if sep and (k.strip().startswith('Priority') or k.strip() in ('SchedulerType','SchedulerParameters','PreemptType')):
            conf[k.strip()]=v.strip()
    partitions=[]
    for line in query(['scontrol','-o','show','partition']).splitlines():
        row=dict(re.findall(r'(\w+)=([^\s]+)',line))
        if 'PartitionName' in row:partitions.append([row['PartitionName'],row.get('PriorityTier'),row.get('PriorityJobFactor')])
    qos=[s.split('|')[:2] for s in query(['sacctmgr','-n','-P','show','qos','format=Name,Priority']).splitlines()]
    _policy.update(stamp=now,config=conf,partitions=partitions,qos=qos)
    return _policy

def scheduling():
    keys=['site','age','assoc','fair','size','partition','qos','nice','tres']
    details={}
    for line in query(['sprio','-h','-o','%i|%r|%S|%A|%B|%F|%J|%P|%Q|%N|%T']).splitlines():
        fields=[f.strip() for f in line.split('|')]
        if len(fields)==11:details[fields[0]+'|'+fields[1]]=dict(zip(keys,fields[2:]))
    return dict(policy(),details=details)

def shell_text(path):
    try:
        if path.suffix not in ('.sh','.bash','.sbatch','.slurm') or not path.is_absolute():return ''
        with path.open('rb') as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):return ''
            return f.read(262144).decode('utf-8','replace')
    except OSError:return ''

def expand(token,env):
    return re.sub(r'\$\{(\w+)\}|\$(\w+)',lambda m:env.get(m[1] or m[2],m[0]),token)

def protect(s):
    s=re.sub(r'(?i)((?:--|\b)(?:api[-_]key|password|secret|access[-_]token|auth[-_]token|token)(?:=|\s+))\S+',r'\1[已隐藏]',s)
    return re.sub(r'(?i)(https?://)[^/\s:@]+:[^/\s@]+@',r'\1[已隐藏]@',s)

def script_commands(text,entry,cwd):
    """Inspect literal invocation lines; never execute shell expansion or scripts."""
    found=[]; visited=set()
    def visit(body,file,work,env,depth):
        if depth>6 or str(file) in visited or len(visited)>=12:return
        visited.add(str(file));env=dict(env);env['SCRIPT_DIR']=str(file.parent)
        if file.name=='train_enactive.sh' and file.parent.parts[-3:]==('scripts','entrypoints','train'):
            env['PROJECT_ROOT']=str(file.parents[3])
        skip=None
        for line in body.replace('\\\n',' ').splitlines():
            line=line.strip()
            if skip:
                if line==skip:skip=None
                continue
            if line.startswith('#') or not line:continue
            doc=re.search(r"<<-?\s*['\"]?(\w+)",line)
            if doc:skip=doc[1];continue
            try:tokens=shlex.split(line,comments=True)
            except ValueError:continue
            if not tokens:continue
            if tokens[0]=='export':tokens=tokens[1:]
            while tokens and re.fullmatch(r'\w+=.*',tokens[0]):
                k,v=tokens.pop(0).split('=',1)
                if '$(' not in v and '`' not in v:
                    m=re.fullmatch(r'\$\{'+re.escape(k)+r':-(.*)\}',v)
                    env[k]=env.get(k) or expand(m[1],env) if m else expand(v,env)
            if tokens[:1]==['exec']:tokens=tokens[1:]
            if not tokens:continue
            if tokens[0]=='cd' and len(tokens)==2:
                resolved=Path(expand(tokens[1],env))
                if resolved.is_absolute():work=resolved
                continue
            prog=Path(expand(tokens[0],env)).name
            references=[]
            if prog in ('bash','sh') and len(tokens)>1:references=[tokens[1]]
            elif tokens[0].endswith(('.sh','.sbatch')):references=[tokens[0]]
            for ref in references:
                p=Path(expand(ref,env));p=p if p.is_absolute() else work/p
                child=shell_text(p)
                if child:visit(child,p,work,env,depth+1)
            if re.fullmatch(r'python(?:\d+(?:\.\d+)?)?|torchrun|accelerate|deepspeed|srun|mpirun|uv',prog) or tokens[0] in ('"$ACCELERATE_BIN"','$ACCELERATE_BIN','$PYTHON_BIN'):
                if any('allocation_guard.py' in t or 'preflight.py' in t for t in tokens):continue
                keys=re.findall(r'\$\{?(\w+)',line)
                values={k:env[k] for k in keys if k in env and k in ('TRAIN_SCRIPT','MODEL_CONFIG_FILE','TRAIN_RUNTIME_CONFIG_FILE','DATASET_CONFIG_FILE','OUTPUT_DIR') and '$' not in env[k]}
                found.append(dict(command=protect(line),source=str(file),values=values))
    visit(text,entry,cwd,{},0)
    return found

def observed(job):
    if job['state']=='PENDING':return []
    if socket.gethostname().split('.')[0].lower() not in job['nodes'].lower():return []
    listing=query(['scontrol','listpids',job['id']])
    seen=set();rows=[]
    for line in listing.splitlines():
        fields=line.split()
        if not fields or not fields[0].isdigit():continue
        try:
            argv=(Path('/proc')/fields[0]/'cmdline').read_bytes()[:32768].decode('utf-8','replace').strip('\0').split('\0')
        except OSError:continue
        exe=Path(argv[0]).name
        if not re.fullmatch(r'python[\d.]*|torchrun|accelerate|deepspeed|srun|mpirun',exe):continue
        cmd=protect(shlex.join(argv))
        if cmd not in seen:seen.add(cmd);rows.append(dict(command=cmd,source='运行进程 PID '+fields[0]))
        if len(rows)>=6:break
    return rows

def command_details(job):
    submission=job.get('command') or ''
    # Only a scheduler-identified path is inspected. The browser supplies a job ID.
    try:args=shlex.split(submission)
    except ValueError:args=[]
    entry=Path(submission) if submission.startswith('/') and submission.endswith(('.sh','.sbatch','.slurm','.bash')) and Path(submission).is_file() else Path(args[0]) if args else Path('.')
    body=query(['scontrol','write','batch_script',job['id'],'-'])
    origin='Slurm 已提交批脚本' if body else '当前可读脚本'
    if not body:body=shell_text(entry)
    commands=script_commands(body,entry,Path(job.get('workdir') or '/'),) if body else []
    return dict(id=job['id'],name=job['name'],state=job['state'],submission=protect(submission),observed=observed(job),
                commands=commands,origin=origin)
