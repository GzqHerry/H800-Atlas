"""Five-minute Slurm samples and personal trainer-state series for the dashboard."""
import json
import os
import re
import shlex
import stat
import time
from bisect import bisect_right
from functools import lru_cache
from itertools import groupby
from pathlib import Path
from monitor import CK_NAME, checkpoint_groups, expand_nodes, number, read_state, slurm
from insights import gpu_counts, discover

HISTORY = Path(__file__).resolve().parents[2] / 'samples' / 'nodes.json'

TRAINING_NAME = re.compile(r'train|fine[-_]?tun|(?:^|[^a-z])(?:sft|rlt|rlhf|grpo|dpo)(?![a-z])|训练', re.I)
OTHER_WORK = re.compile(r'eval|benchmark|preflight|smoke|probe|debug|(?:^|[^a-z])(?:test|infer|inference|convert|export|audit)(?![a-z])|评测|测试', re.I)
SCRIPT_NAME = re.compile(r'''(?:^|[\s/"'])([^\s/"']+\.(?:sbatch|slurm|sh|py))(?=$|[\s"'])''', re.I)
FINISHED = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL',
            'BOOT_FAIL', 'DEADLINE', 'PREEMPTED', 'REVOKED', 'SPECIAL_EXIT'}


def command_program(words, variables):
    """Identify one command's entry point without interpreting its arguments as tasks."""
    args = iter(words)
    launcher = ''
    for arg in args:
        if ref := re.fullmatch(r'\$\{?(\w+)\}?', arg):
            arg = variables.get(ref[1], '')
        if not arg:
            return ''
        if launcher:
            if (launcher != 'srun' and arg in ('-c', '--command')) or (launcher in ('sh', 'bash') and re.fullmatch(r'-[a-z]*c[a-z]*', arg)):
                return ''
            if arg in ('-m', '--module'):
                module = next(args, '')
                if module == 'torch.distributed.run':
                    launcher = 'torchrun'
                    continue
                return module
            if arg in ('-n', '-N', '-p', '-A', '-c', '--gres', '--partition', '--account',
                       '--ntasks', '--nodes', '--gpus', '--nproc-per-node', '--nproc_per_node',
                       '--nnodes', '--master_port', '--num_processes', '-W', '-X'):
                next(args, None)
                continue
            if arg.startswith('-') or (launcher == 'accelerate' and arg == 'launch'):
                continue
        if arg in ('exec', 'env', 'export', 'then', 'do', 'if', 'elif', 'while', 'until', 'command'):
            continue
        if assignment := re.fullmatch(r'(\w+)=(.*)', arg):
            variables[assignment[1]] = assignment[2]
            continue
        name = Path(arg).name
        if name in ('srun', 'torchrun', 'accelerate', 'bash', 'sh') or re.fullmatch(r'python[\d.]*', name):
            launcher = name
            continue
        # Shell conditions such as "test -f model" are not test workloads.
        return '' if name == 'test' else name
    return ''


def script_programs(source):
    programs = set()
    variables = {}
    heredoc = None
    for line in source.replace('\\\n', ' ').splitlines():
        if heredoc:
            if line.strip() == heredoc:
                heredoc = None
            continue
        if line.lstrip().startswith('#'):
            continue
        if match := re.search(r"""(?<!<)<<-?\s*['"]?(\w+)""", line):
            heredoc = match[1]
            continue
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=';&|')
            lexer.whitespace_split = True
            for separator, group in groupby(lexer, lambda w: w in (';', '&&', '||', '|', '&')):
                if not separator:
                    name = command_program(group, variables)
                    if re.fullmatch(r'[\w.+-]{1,128}', name) and not name.isdigit():
                        programs.add(name)
        except ValueError:
            continue
    return sorted(programs)


@lru_cache(maxsize=256)
def file_programs(path, mtime_ns, size):
    with path.open('rb') as stream:
        return script_programs(stream.read(128 * 1024).decode(errors='replace'))


def launch_labels(submit, workdir, submitted, safe):
    try:
        words = shlex.split(submit)
    except ValueError:
        return {}
    if not words:
        return {}
    if Path(words[0]).name != 'sbatch':
        return dict(programs=script_programs(submit))
    args = iter(words[1:])
    for arg in args:
        if arg == '--wrap' or arg.startswith('--wrap='):
            return dict(programs=script_programs(arg[7:] if '=' in arg else next(args, '')))
        if arg in ('-J', '-o', '-e', '-D', '-A', '-p', '-N', '-n', '-t', '-c', '-C', '-d', '-a',
                   '--job-name', '--output', '--error', '--chdir', '--account', '--partition',
                   '--nodes', '--ntasks', '--time', '--gres', '--gpus', '--export', '--mem',
                   '--constraint', '--dependency', '--array', '--qos', '--reservation',
                   '--cpus-per-task', '--mem-per-cpu', '--ntasks-per-node', '--gpus-per-node'):
            next(args, None)
            continue
        if arg.startswith('-'):
            continue
        path = Path(arg)
        if path.suffix.lower() not in ('', '.sbatch', '.slurm', '.sh', '.bash', '.py'):
            return {}
        labels = dict(script=path.name)
        try:
            if not path.is_absolute() and not Path(workdir).is_absolute():
                return labels
            path = safe(Path(workdir) / path)
            info = path.stat()
            if (submitted and stat.S_ISREG(info.st_mode) and info.st_size <= 128 * 1024
                    and int(info.st_mtime) <= submitted and path.suffix.lower() != '.py'):
                labels['programs'] = file_programs(path, info.st_mtime_ns, info.st_size)
        except (ValueError, OSError, RuntimeError):
            pass
        return labels
    return {}


def usage_rows(lines, outcomes=None, *, gpu_only=False):
    outcomes=outcomes or {}
    users = {}
    seen = set()
    for line in lines:
        fields = line.split('|', 6)
        if len(fields) != 7:
            continue
        job, user, name, state, elapsed, tres, submit = fields
        if not user or job in seen or not elapsed.isdigit() or int(elapsed) == 0:
            continue
        labels = ' '.join([name, *SCRIPT_NAME.findall(submit)])
        phase=outcomes.get(user,{}).get(job)
        if not gpu_only and phase is None and (OTHER_WORK.search(labels) or not TRAINING_NAME.search(labels)):
            continue
        state = state.partition(' ')[0].rstrip('+')
        seconds = int(elapsed)
        allocation = dict(item.split('=', 1) for item in tres.split(',') if '=' in item)
        cards = allocation.get('gres/gpu')
        if cards is None:
            cards = sum(int(v) for k, v in allocation.items() if k.startswith('gres/gpu:') and v.isdigit())
        if gpu_only and not int(cards):
            continue
        seen.add(job)
        row = users.setdefault(user, dict(user=user, jobs=0, seconds=0, gpuSeconds=0, completed=0, ended=0))
        row['gpuSeconds'] += seconds * int(cards)
        row['seconds'] += seconds
        row['jobs'] += 1
        success=phase=='holding_after_success'
        terminal=phase in ('holding_after_success','holding_after_failure')
        row['completed'] += success if terminal else state=='COMPLETED'
        row['ended'] += terminal or state in FINISHED
    return list(users.values())


@lru_cache(maxsize=4)
def usage_lines(days, bucket):
    if days not in (7, 30):
        raise ValueError('unsupported period')
    return slurm('sacct', '-a', '-X', '-T', '-nP', '-S', f'now-{days}days', '-E', 'now',
                  '-o', 'JobIDRaw,User,JobName,State,ElapsedRaw,AllocTRES,SubmitLine')


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
    robots, datasets=discover()
    target=HISTORY.with_name('datasets.json')
    tmp=target.with_suffix('.tmp')
    tmp.write_text(json.dumps(dict(robots=robots,datasets=datasets,stamp=int(time.time())),ensure_ascii=False))
    os.chmod(tmp,0o640)
    tmp.replace(target)
