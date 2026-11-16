"""NAS dataset catalog and checksum-verified checkpoint uploads."""
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import grus
from pathlib import Path

HOME = Path.home()
SCRATCH = Path('/scratch') / HOME.name
LEGACY_JOBS = SCRATCH / 'h800-atlas-uploads'
JOBS = HOME / '.local/share/h800-atlas/uploads'
# Each SSH user supplies their own NAS key and known_hosts.
NAS_ROOT = '/mnt/orion/data/public/' + ('gzq' if HOME.name=='gzqherry' else 'users/'+HOME.name)
def enabled():
    return (HOME/'.ssh/id_ed25519').is_file() and (SCRATCH/'nas-archive-20261112/known_hosts').is_file()

def catalog():
    if not enabled():return dict(datasets=[],enabled=False,note='此账号尚未配置 NAS 密钥和主机指纹')
    return dict(remote('list'),enabled=True)

HOST = 'public@172.30.80.1'
SSH = ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=6','-o','ServerAliveInterval=20',
       '-o','ServerAliveCountMax=3','-o','StrictHostKeyChecking=yes','-o',
       'UserKnownHostsFile='+str(SCRATCH/'nas-archive-20261112/known_hosts'),'-i',str(HOME/'.ssh/id_ed25519')]
# The same key and host fingerprint are used by the existing NAS archive.
REMOTE = """
import json, os, re, shutil, sys, time
from pathlib import Path
q=json.load(sys.stdin)
root=Path(q['root'])
if root.resolve()!=root:raise ValueError('Invalid NAS root')
if q['action']=='checkpoints':
    rows=[]
    def receipt(path):
        try:
            if path.resolve()!=path or path.stat().st_size>2000000:return {}
            return json.loads(path.read_text())
        except (OSError,ValueError):return {}
    def add(src,p,size,verified):
        if not re.fullmatch(r'(?:checkpoint-|global_step_)?[0-9]+',p.name) or p.name!=src.name:return
        if not src.is_absolute() or '..' in src.parts or not p.is_dir() or p.resolve()!=p:return
        # An in-progress archive can create the full directory tree before copying files.
        if not any(files for _,_,files in os.walk(p,followlinks=False)):return
        rows.append(dict(source=str(src),destination=str(p),bytes=size,verified=bool(verified),updated=int(p.stat().st_mtime)))
    for manifest in (root/'checkpoints').glob('*/*/*/.atlas-checkpoint.json'):
        if manifest.parent.name.startswith('.') or not re.fullmatch('[a-f0-9]{24}',manifest.parent.name):continue
        data=receipt(manifest)
        if data.get('kind')=='checkpoint':
            src=Path(data['path'])
            if src.name==manifest.parents[1].name:add(src,manifest.parent/src.name,data.get('bytes'),True)
    for archive in root.glob('h800-*'):
        if not archive.is_dir() or archive.resolve()!=archive:continue
        inv=receipt(archive/'inventory.json').get('checkpoint_dirs',{})
        for original,entry in inv.items():
            src=Path(original)
            if not src.is_absolute() or '..' in src.parts:continue
            dest=archive/src.relative_to('/')
            if not dest.is_dir() or not re.fullmatch(r'(?:checkpoint-|global_step_)?[0-9]+',src.name):continue
            verified=False
            for parent in [dest,*dest.parents]:
                if parent==archive:break
                proof=receipt(parent/'NAS_ARCHIVE_RECEIPT.json')
                if proof.get('source')==str(Path('/')/parent.relative_to(archive)) and proof.get('verified_utc') and 'checksum' in str(proof.get('verification','')):
                    verified=True
                    break
            add(src,dest,entry[1],verified)
    print(json.dumps(rows))
elif q['action']=='list':
    rows=[]
    def add(p,**v):
        rows.append(dict(v,path=str(p),location='NAS',updated=int(p.stat().st_mtime)))
    errors=[]
    def error(e):errors.append(e.filename)
    for directory,dirs,files in os.walk(root,followlinks=False,onerror=error):
        p=Path(directory)
        if p==root and 'checkpoints' in dirs:dirs.remove('checkpoints')
        if '.atlas-dataset.json' in files and (p/'data').is_dir():
            v=json.loads((p/'.atlas-dataset.json').read_text())
            add(p/'data',**{k:v.get(k) for k in ('name','robot','kind','episodes','frames','jobId','bytes','source')})
            dirs.clear()
        elif 'meta' in dirs and (p/'meta/info.json').is_file():
            v=json.loads((p/'meta/info.json').read_text())
            add(p,name=v.get('dataset_name') or p.name,robot=v.get('robot_type') or 'NAS',kind='LeRobot',
                episodes=v.get('total_episodes'),frames=v.get('total_frames'))
            dirs.clear()
        else:
            dirs[:]=[n for n in dirs if not n.startswith(('.','checkpoint-','global_step')) and n not in ('videos','images','optimizer_0')]
    usage=shutil.disk_usage(root)
    print(json.dumps(dict(datasets=sorted(rows,key=lambda x:-x['updated']),root=str(root),
          total=usage.total,free=usage.free,partial=bool(errors),stamp=int(time.time()))))
else:
    key=q['key']; version=q['version']
    assert re.fullmatch(r'[A-Za-z0-9_.-]+-[a-f0-9]{12}',key) and re.fullmatch(r'[a-f0-9]{24}',version)
    checkpoint=q['checkpoint']
    if not re.fullmatch(r'(?:checkpoint-|global_step_)?[0-9]+',checkpoint):raise ValueError('Invalid checkpoint')
    parent=root/'checkpoints'/key/checkpoint
    final=parent/version
    stage=parent/('.partial-'+version)
    assert root.resolve()==root and parent.resolve()==parent and stage.resolve()==stage and final.resolve()==final
    parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if q['action']=='prepare':
        if not final.exists(): (stage/checkpoint).mkdir(mode=0o700,parents=True,exist_ok=True)
    elif q['action']=='publish':
        if not final.exists():
            (stage/'.atlas-checkpoint.json').write_text(json.dumps(q['meta'],ensure_ascii=False))
            stage.rename(final)
    print(json.dumps(dict(done=(final/'.atlas-checkpoint.json').is_file(),stage=str(stage/checkpoint),final=str(final/checkpoint))))
"""

def remote(action, **params):
    r=subprocess.run(SSH+[HOST,'python3 -c '+shlex.quote(REMOTE)],input=json.dumps(dict(action=action,root=NAS_ROOT,**params)),
                     capture_output=True,text=True,timeout=45,check=True)
    return json.loads(r.stdout)

def write_job(job):
    path=JOBS/(job['id']+'.json')
    job['updated']=int(time.time())
    with tempfile.NamedTemporaryFile(dir=JOBS,mode='w',delete=False) as temp:
        try:
            json.dump(job,temp,ensure_ascii=False)
            temp.flush()
            os.replace(temp.name,path)
        finally:Path(temp.name).unlink(missing_ok=True)

def job_list():
    rows={}
    for folder in (LEGACY_JOBS,JOBS):
        for p in folder.glob('*.json'):
            try:
                row=json.loads(p.read_text())
                row.setdefault('updated',int(p.stat().st_mtime))
                rows[row['id']]=row
            except (OSError,KeyError,ValueError):
                continue
    for job in rows.values():
        if job['state'] in ('完成','失败') or not job.get('pid'):continue
        if not Path('/proc',str(job['pid'])).exists():
            job.update(state='失败',error='操作进程已退出，请重试以复核目标文件。')
    return sorted(rows.values(),key=lambda j:-j['started'])

def enqueue(checkpoint, action='upload'):
    JOBS.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (JOBS/'submit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        for j in job_list():
            if j['source']==checkpoint['path'] and j['state'] not in ('完成','失败'):
                try:os.kill(j.get('pid',0) or -99999999,0)
                except ProcessLookupError:
                    j.update(state='失败',error='上传进程已退出，可重新上传');write_job(j)
                else:
                    if j.get('action','upload')!=action:raise ValueError('该 checkpoint 已有任务在执行')
                    return j
        job=dict(id=uuid.uuid4().hex,source=checkpoint['path'],meta=checkpoint,action=action,started=int(time.time()),state='等待复核' if action=='remove' else '待传输',percent=0)
        # Worker takes this same lock before reading the submitted record.
        child=subprocess.Popen([sys.executable,__file__,job['id']],start_new_session=True,
                               stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        job['pid']=child.pid
        write_job(job)
        return job

def source_signature(root,cleanup=False):
    digest=hashlib.sha256()
    size=0
    def fail(e):raise e
    for directory,dirs,files in os.walk(root,followlinks=False,onerror=fail):
        dirs.sort();files.sort()
        for name in dirs+files:
            p=Path(directory)/name
            st=p.lstat()
            if cleanup and (st.st_uid!=os.getuid() or p.is_mount()):
                raise ValueError('checkpoint 含其他用户的文件或挂载点，已保留本地目录')
            rel=str(p.relative_to(root))
            if p.is_symlink():
                target=os.readlink(p)
                if os.path.isabs(target) or not p.resolve().is_relative_to(root):
                    raise ValueError('Checkpoint 含指向目录外或绝对路径的链接：'+rel)
            elif p.is_file():size+=st.st_size
            elif not p.is_dir():raise ValueError('不支持特殊文件：'+rel)
            digest.update(os.fsencode(rel)+b'\0'+str((st.st_mode,st.st_mtime_ns,st.st_size)).encode()+b'\0')
    return digest.hexdigest()[:24],size

def remove_verified_copy(path, job, update):
    candidates=[c for c in remote('checkpoints') if c['source']==job['source'] and c['verified']]
    if not candidates:raise ValueError('未找到该 checkpoint 的已校验 NAS 副本')
    if path.is_symlink() or path.is_mount() or path!=Path(job['meta']['resolved']) or path.stat().st_uid!=os.getuid():
        raise ValueError('本地目录归属或路径已改变')
    before=path.stat()
    signature,size=source_signature(path,cleanup=True)
    if not size:raise ValueError('本地 checkpoint 为空，请检查目录')
    for copy in sorted(candidates,key=lambda c:-c['updated']):
        destination=copy['destination']
        cmd=['rsync','-rl','--protect-args','--checksum','--dry-run','--delete',
             '--itemize-changes','-e',shlex.join(SSH),str(path)+'/',HOST+':'+destination+'/']
        result=subprocess.run(cmd,capture_output=True,text=True,check=True)
        if not result.stdout.strip():break
    else:raise ValueError('NAS 与本地内容不同，已保留本地副本')
    if source_signature(path,cleanup=True)[0]!=signature or not os.path.samestat(before,path.stat()):
        raise ValueError('校验期间本地目录发生变化，已取消删除')
    # Detach the verified directory so a recreated checkpoint at the old name is never removed.
    staged=path.with_name('.atlas-cleanup-'+job['id'])
    if staged.exists():raise ValueError('清理暂存目录已存在')
    update('清理中',destination=destination,staging=str(staged))
    path.rename(staged)
    try:
        if not os.path.samestat(before,staged.lstat()) or source_signature(staged,cleanup=True)[0]!=signature:
            raise ValueError('本地目录在清理前已改变')
    except Exception:
        if not os.path.lexists(path):staged.rename(path)
        raise
    shutil.rmtree(staged)
    update('完成',percent=100,bytes=size,staging=None)


def transfer_files(cmd, update):
    with subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace') as proc:
        last=0;tail=''
        for line in proc.stdout:
            tail=line.strip() or tail
            m=re.match(r'\s*([\d,]+)\s+(\d+)%',line)
            if m and time.monotonic()-last>=1:
                # A transient status-store failure must not close rsync's output pipe.
                try:update('传输中',percent=min(99,int(m[2])),processedBytes=int(m[1].replace(',','')))
                except OSError:pass
                last=time.monotonic()
        if proc.wait():raise RuntimeError('rsync 失败：'+tail[:240])


def worker(jid):
    # Home is NFS: a shared lock requires a readable file descriptor.
    with (JOBS/'submit.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_SH)
        job=json.loads((JOBS/(jid+'.json')).read_text())
    def update(state,**extra):
        job.update(state=state,**extra);write_job(job)
    try:
        with (JOBS/'transfer.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            update('清点文件')
            original=Path(job['source'])
            if original.is_symlink():raise ValueError('不能操作链接目录')
            path=original.resolve(strict=True)
            if str(path)!=job['meta']['resolved'] or not path.is_dir():raise ValueError('Checkpoint 源目录已更改')
            inference_target={'upload-grus':'Grus','upload-tianji':'Tianji'}.get(job.get('action'))
            if inference_target:
                grus.upload(path,job,update,transfer_files,inference_target)
                return
            if job.get('action')=='remove':
                update('重新校验')
                remove_verified_copy(path,job,update)
                return
            version,size=source_signature(path)
            group=Path(job['meta']['group'])
            title=re.sub(r'[^A-Za-z0-9_.-]+','-',group.name).strip('-.')[:72] or 'training'
            key=title+'-'+hashlib.sha256(os.fsencode(group)).hexdigest()[:12]
            args=dict(key=key,version=version,checkpoint=job['meta']['name'])
            target=remote('prepare',**args)
            job['destination']=target['final']
            base=['rsync','-rlt','--links','--protect-args','-e',shlex.join(SSH)]
            def endpoints(where):return [str(path)+'/',HOST+':'+where+'/']
            if not target['done']:
                update('传输中',bytes=size)
                cmd=base+['--partial','--info=progress2','--outbuf=L']+endpoints(target['stage'])
                transfer_files(cmd,update)
            update('校验中')
            check=subprocess.run(base+['--checksum','--dry-run','--itemize-changes','--delete']+
                 endpoints(target['final'] if target['done'] else target['stage']),capture_output=True,text=True,check=True)
            if check.stdout.strip() or source_signature(path)[0]!=version:raise RuntimeError('源与 NAS 校验不一致，请在源目录稳定后重试')
            if not target['done']:
                remote('publish',meta=dict(job['meta'],bytes=size,source=str(path)),**args)
            update('完成',percent=100,bytes=size)
    except Exception as e:
        msg=(e.stderr or '').strip()[:250] if isinstance(e,subprocess.CalledProcessError) else str(e)[:250]
        update('失败',error=msg or '传输命令失败，请检查 NAS 连接及权限')

if __name__=='__main__':
    os.umask(0o077)
    worker(sys.argv[1])
