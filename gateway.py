"""H800 Atlas HTTP/HTTPS gateway. One authenticated SSH master per session."""
from typing import NamedTuple
import collections
import base64
import hashlib
import io
import http.cookies
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import ssl
import socketserver
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from gpu_usage import live_gpu_sample
from cpu_usage import live_cpu_sample
import tianji

ROOT = Path(__file__).resolve().parent.parent.parent
CURRENT = ROOT / 'current'
DEFAULT_NODE = 'h800-node11'
LOGIN_NODES = tuple('h800-node'+str(i) for i in range(11, 17))
RUN = Path(os.environ['XDG_RUNTIME_DIR'])
OLD_SOCKET = Path('/scratch') / Path.home().name / 'h800-observatory.sock'
MAX_AGE = 24 * 3600
COOKIE = 'h800atlas_session'
TLS_COOKIE = '__Host-h800atlas_session'
sessions = {}
attempts = collections.defaultdict(list)
guard = threading.Lock()
auth_slots = threading.BoundedSemaphore(6)
AVATARS = Path.home()/'.local/share/h800-atlas/avatars'
PROFILES = AVATARS.parent/'profiles'
outcome_lock=threading.Lock()

def shared_catalog():
    return json.loads((ROOT/'samples/datasets.json').read_bytes())

def member_inventory(user, counts=None):
    counts=dict(counts if counts is not None else (profile_value(user,'inventory',{}) or {}))
    catalog=shared_catalog()
    counts.update(datasets=len(catalog['datasets']),datasetStamp=catalog['stamp'])
    return counts

def remember_outcomes(user, phases):
    if not phases:return
    with outcome_lock:
        saved=profile_value(user,'outcomes',{})
        combined={**saved,**phases}
        if combined!=saved:save_json(PROFILES/'outcomes'/(user+'.json'),combined)


def avatar_catalog():
    result = {}
    for p in AVATARS.glob('*.json'):
        value = json.loads(p.read_bytes())
        result[p.stem] = value['thumb'] if isinstance(value, dict) else value
    return result


def jpeg_data(image, quality):
    output = io.BytesIO()
    image.convert('RGB').save(output, 'JPEG', quality=quality)
    return 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode('ascii')


def save_cover(user, body):
    from PIL import Image
    if body == {'default': True}:
        cover = None
    elif set(body) == {'photo'} and isinstance(body['photo'], str):
        with Image.open(io.BytesIO(base64.b64decode(body['photo'], validate=True)), formats=['JPEG']) as image:
            if image.size not in ((1250, 330), (1200, 264), (1200, 240)):
                raise ValueError('invalid dimensions')
            cover = jpeg_data(image, 89)
    else:
        raise ValueError('invalid cover')
    save_json(PROFILES/'covers'/(user+'.json'), cover)
    return cover


def save_avatar(user, body):
    # user comes from the authenticated SSH session; never accept a target username.
    if set(body) == {'preset'} and type(body['preset']) is int and 0 <= body['preset'] < 48:
        avatar = body['preset']
    elif set(body) == {'photo'} and isinstance(body['photo'], str):
        from PIL import Image
        raw = base64.b64decode(body['photo'], validate=True)
        with Image.open(io.BytesIO(raw), formats=['JPEG']) as image:
            if image.size not in ((128, 128),(512, 512)):
                raise ValueError('invalid dimensions')
            avatar = dict(full=jpeg_data(image, 91), thumb=jpeg_data(image.resize((128,128),getattr(Image, "Resampling", Image).LANCZOS),85))
    else:
        raise ValueError('invalid avatar')
    save_json(AVATARS/(user+'.json'), avatar)
    return avatar['thumb'] if isinstance(avatar,dict) else avatar


def save_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, mode='w', delete=False) as temp:
        try:
            json.dump(value, temp)
            temp.flush()
            os.replace(temp.name, path)
        finally:
            Path(temp.name).unlink(missing_ok=True)

def profile_value(user, field, default):
    try:return json.loads((PROFILES/field/(user+'.json')).read_bytes())
    except FileNotFoundError:return default


class Release(NamedTuple):
    version: str
    directory: Path
    login: bytes
    app: bytes


def read_release(previous=None):
    directory = CURRENT.resolve(strict=True)
    if previous and directory == previous.directory:
        return previous
    return Release(directory.name, directory, (directory/'dist/login.html').read_bytes(),
                   (directory/'dist/index.html').read_bytes())


def refresh_releases():
    # One reader can wait on shared storage without tying up HTTP requests.
    global published
    while True:
        time.sleep(3)
        try:
            published = read_release(published)
        except OSError:
            continue


class Worker:
    def __init__(self, user, password, node):
        self.attach(user, Path(tempfile.mkdtemp(dir=RUN, prefix='h800-atlas-session-')), time.monotonic(), node)
        try:
            # sshpass consumes the password from an inherited pipe, never argv or env.
            # The authenticated master outlives a web gateway release. systemd also
            # terminates it at the session deadline if the gateway is offline.
            result = subprocess.run(['systemd-run','--user','--scope','--quiet','--unit='+self.directory.name,
                '--property=RuntimeMaxSec='+str(MAX_AGE)+'s','--','/usr/bin/sshpass', '-d', '0', '/usr/bin/ssh',
                '-M', '-N', '-f', '-oPreferredAuthentications=password',
                '-oNumberOfPasswordPrompts=1', '-oServerAliveInterval=30',
                '-oServerAliveCountMax=2', *self.base[1:]], input=password.encode()+b'\n',
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12)
            if result.returncode:
                if result.returncode==5:raise PermissionError()
                raise ConnectionError()
            self.start()
        except BaseException:
            self.close()
            raise

    def attach(self, user, directory, born, node):
        self.node = node
        local = node == DEFAULT_NODE
        keys = ROOT/'known_hosts' if local else CURRENT/'login_nodes.known_hosts'
        self.user, self.directory, self.born = user, directory, born
        self.base = ['/usr/bin/ssh', '-F', '/dev/null', '-oControlPath='+str(directory/'ssh'),
                     '-oStrictHostKeyChecking=yes', '-oUserKnownHostsFile='+str(keys),
                     '-oGlobalKnownHostsFile=/dev/null', '-oPubkeyAuthentication=no',
                     '-oConnectTimeout=6', '-l', user, '127.0.0.1' if local else node]
        self.lock = threading.Lock()
        self.child = self.version = None

    def read(self, seconds):
        if not select.select([self.child.stdout], [], [], seconds)[0]:
            raise TimeoutError()
        line = self.child.stdout.readline(32*1024*1024)
        if not line or not line.endswith(b'\n'):raise ConnectionError()
        return json.loads(line)

    def start(self):
        self.stop_child()
        current = published
        self.version, directory = current.version, current.directory
        self.child = subprocess.Popen([self.base[0], '-oBatchMode=yes', *self.base[1:], '/usr/bin/python3 -E -s -B -u '+str(directory/'server.py')],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
        hello = self.read(12)
        if hello.get('user')!=self.user or not hello.get('ready'):raise ConnectionError()

    def call(self, method, target, body):
        with self.lock:
            if not self.directory.is_dir():raise PermissionError()
            try:
                if self.version!=published.version or self.child is None:self.start()
                self.child.stdin.write(json.dumps(dict(method=method,target=target,body=body)).encode()+b'\n')
                self.child.stdin.flush()
                return self.read(70)
            except (OSError, ValueError):
                self.stop_child()
                raise

    def stop_child(self):
        if self.child:
            try:self.child.stdin.close()
            except BrokenPipeError:pass
            try:self.child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()
            self.child.stdout.close()
            self.child=None

    def close(self):
        with self.lock:
            self.stop_child()
            subprocess.run([*self.base[:-1], '-O', 'exit', self.base[-1]],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            shutil.rmtree(self.directory, ignore_errors=True)


def expired(worker, now):
    return now-worker.born>=MAX_AGE


def restore_sessions():
    for directory in RUN.glob('h800-atlas-session-*'):
        if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid != os.getuid():
            continue
        try:
            data=json.loads((directory/'session.json').read_bytes())
            key,user,born=data['hash'],data['user'],data['born']
            node=data.get('node', DEFAULT_NODE)
            if node not in LOGIN_NODES:raise ValueError('invalid login node')
            if not re.fullmatch('[0-9a-f]{64}',key) or not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}',user):
                raise ValueError('invalid session')
            if not (directory/'ssh').is_socket() or not 0 <= time.monotonic()-born < MAX_AGE:
                raise ValueError('expired session')
        except (OSError,ValueError,KeyError,TypeError):
            shutil.rmtree(directory,ignore_errors=True)
            continue
        worker=Worker.__new__(Worker)
        worker.attach(user,directory,born,node)
        sessions[key]=worker


def cleanup():
    while True:
        time.sleep(30)
        now=time.monotonic()
        with guard:
            old=[sessions.pop(k) for k,w in list(sessions.items()) if expired(w,now)]
            for key,events in list(attempts.items()):
                if not events or events[-1]<now-300:del attempts[key]
        for worker in old:worker.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    @property
    def secure(self):
        return isinstance(self.connection,ssl.SSLSocket)

    @property
    def team_http(self):
        return getattr(self.server,'team_http',False)

    def reply(self, status, value, mime=None, cookie=None):
        if mime is None:
            value=json.dumps(value,ensure_ascii=False).encode()
            mime='application/json;charset=utf-8'
        self.send_response(status)
        for k,v in [('Content-Type',mime),('Cache-Control','no-store'),('X-Frame-Options','DENY'),
                    ('X-Content-Type-Options','nosniff'),('Referrer-Policy','no-referrer'),
                    ('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
                    ('Content-Length',str(len(value)))]:self.send_header(k,v)
        if cookie is not None and not self.bearer:
            key=TLS_COOKIE if self.cookie_secure else COOKIE
            self.send_header('Set-Cookie',key+'='+cookie+'; Path=/; HttpOnly; SameSite=Strict'+('; Secure' if self.cookie_secure else '')+'; Max-Age='+str(MAX_AGE if cookie else 0))
        self.end_headers()
        self.wfile.write(value)

    def handle_request(self):
        host=self.headers.get('Host','')
        if self.team_http:
            origin=self.server.origins.get(host.lower())
            if origin is None:return self.reply(403,{'error':'访问地址无效'})
        elif self.secure:
            if host.lower() not in self.server.hosts:return self.reply(403,{'error':'访问地址无效'})
            origin='https://'+host
        elif not re.fullmatch(r'(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?',host):
            return self.reply(403,{'error':'访问地址无效'})
        else:
            origin='http://'+host
        self.cookie_secure=origin.startswith('https://')
        self.bearer=self.team_http and not self.cookie_secure
        path=urlsplit(self.path).path
        jar=http.cookies.SimpleCookie()
        jar.load(self.headers.get('Cookie',''))
        key=TLS_COOKIE if self.cookie_secure else COOKIE
        token=jar[key].value if key in jar else ''
        if self.bearer:
            # Other services share this IP. Cookies are shared across ports.
            scheme,_,credential=self.headers.get('Authorization','').partition(' ')
            token=credential if scheme=='Bearer' else ''
        token=hashlib.sha256(token.encode()).hexdigest()
        now=time.monotonic()
        with guard:
            worker=sessions.get(token)
            if worker and expired(worker,now):worker=None
        body={}
        if self.command=='POST':
            action={'/auth/login':'login','/auth/logout':'logout','/api/upload':'upload','/api/remove-local':'remove-local','/api/avatar':'avatar','/api/profile-cover':'cover','/api/profile':'bio','/api/checkpoint-scan':'checkpoint-scan','/api/wandb-link':'wandb-link'}.get(path)
            if not action or self.headers.get('Origin')!=origin or self.headers.get('X-Atlas-Action')!=action:
                return self.reply(403,{'error':'请求来源无效'})
            length=int(self.headers.get('Content-Length',0))
            limit=768*1024 if path in ('/api/avatar','/api/profile-cover') else 8192
            if not 0<=length<=limit or (not length and path!='/auth/logout'):
                return self.reply(400,{'error':'请求大小无效'})
            if length:
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict):return self.reply(400,{'error':'请求格式无效'})
            if path=='/auth/login':return self.login(body,token)
            if path=='/auth/logout':
                with guard: removed=sessions.pop(token,None)
                if removed:removed.close()
                return self.reply(200,{'ok':True},cookie='')
        if self.command=='GET' and (path in ('/','/index.html') or (self.team_http and path=='/login')):
            current=published
            show_app=(path!='/login') if self.bearer else bool(worker)
            data=current.app if show_app else current.login
            if not show_app:
                choices = ''.join('<label><input type="radio" name="node" value="'+n+'"'
                                  +(' checked' if n==DEFAULT_NODE else '')+'><span>'
                                  +n.removeprefix('h800-')+'</span></label>' for n in LOGIN_NODES)
                data = data.replace(b'__ATLAS_LOGIN_NODES__', choices.encode())
            data=data.replace(b'__ATLAS_TEAM_HTTP__',b'yes' if self.bearer else b'no')
            return self.reply(200,data.replace(b'__ATLAS_RELEASE__',current.version.encode()),'text/html;charset=utf-8')
        if not worker:return self.reply(401,{'error':'请先登录 H800'})
        if self.command=='GET' and path=='/api/gpu-live':
            return self.reply(200,live_gpu_sample())
        if self.command=='GET' and path=='/api/cpu-live':
            return self.reply(200,live_cpu_sample())
        if self.command=='GET' and re.fullmatch(r'/api/guide-images/[a-z0-9-]{1,40}\.webp',path):
            image=published.directory/'dist'/'guide-images'/path.rsplit('/',1)[1]
            if image.is_file():return self.reply(200,image.read_bytes(),'image/webp')
            return self.reply(404,{'error':'截图不存在'})
        if path=='/api/profile':
            if self.command=='POST':
                bio=body.get('bio')
                if set(body)!={'bio'} or not isinstance(bio,str) or len(bio)>240:
                    return self.reply(400,{'error':'简介最多 240 字'})
                save_json(PROFILES/'bios'/(worker.user+'.json'),bio.strip())
                return self.reply(200,{'bio':bio.strip()})
            user=parse_qs(urlsplit(self.path).query).get('user',[''])[0]
            if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}',user):
                return self.reply(400,{'error':'账号无效'})
            return self.reply(200,dict(bio=profile_value(user,'bios',''),inventory=member_inventory(user),cover=profile_value(user,'covers',None)))
        if self.command=='POST' and path=='/api/profile-cover':
            try:cover=save_cover(worker.user,body)
            except (ValueError,OSError):return self.reply(400,{'error':'封面保存失败，请重新选择图片'})
            return self.reply(200,{'cover':cover})
        if self.command=='GET' and path=='/api/avatar-preview':
            user=parse_qs(urlsplit(self.path).query).get('user',[''])[0]
            if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}',user):return self.reply(400,{'error':'账号无效'})
            try:value=json.loads((AVATARS/(user+'.json')).read_bytes())
            except FileNotFoundError:value=None
            return self.reply(200,{'avatar':value['full'] if isinstance(value,dict) else value})
        if self.command=='GET' and path=='/api/profile-inventory':
            last=profile_value(worker.user,'inventory',None)
            if last and time.time()-last['stamp']<1800:return self.reply(200,member_inventory(worker.user,last))
        if self.command=='GET' and path=='/api/avatars':return self.reply(200,avatar_catalog())
        if self.command=='POST' and path=='/api/avatar':
            try:avatar=save_avatar(worker.user,body)
            except (ValueError,OSError):return self.reply(400,{'error':'头像未保存，请重新选择或尝试其他图片'})
            return self.reply(200,{'avatar':avatar})
        if self.command=='GET' and path=='/api/version':return self.reply(200,{'release':published.version})
        if not path.startswith('/api/'):return self.reply(404,{'error':'页面不存在'})
        if self.command=='GET' and path=='/api/training-usage':
            body={'outcomes':{p.stem:json.loads(p.read_bytes()) for p in (PROFILES/'outcomes').glob('*.json')}}
        grant=None
        if self.command=='POST' and path=='/api/upload' and body.get('destination')=='Tianji':
            if worker.node!=DEFAULT_NODE:
                return self.reply(409,{'error':'Tianji 上传服务位于 node11，请选择 node11 登录后提交'})
            grant=tianji.authorize(worker.user,body.get('path'))
            body['tianjiGrant']=grant.name
        try:
            code,data=worker.call(self.command,self.path,body)
            if grant and (code!=202 or data.get('id')!=grant.name):
                grant.unlink(missing_ok=True)
        except (OSError,subprocess.SubprocessError):
            if subprocess.run([*worker.base[:-1],'-O','check',worker.base[-1]],capture_output=True,timeout=3).returncode==0:
                return self.reply(503,{'error':'H800 暂时未响应，登录仍然有效，请重试'})
            with guard:removed=sessions.pop(token,None)
            if removed:removed.close()
            return self.reply(401,{'error':'SSH 会话已断开，请重新登录'},cookie='')
        if type(code) is not int or not 200<=code<=599:raise ValueError()
        if code==200 and path=='/api/assets':data.update(shared_catalog())
        if code==200 and path=='/api/jobs':
            remember_outcomes(worker.user,{j['id']:j.get('trainingPhase') for j in data if 'trainingPhase' in j})
        if code==200 and path=='/api/detail' and data.get('attempt',{}).get('number'):
            job=parse_qs(urlsplit(self.path).query)['id'][0]
            remember_outcomes(worker.user,{job:data['attempt']['phase']})
        if code==200 and path=='/api/checkpoint-scan':
            save_json(PROFILES/'inventory'/(worker.user+'.json'),data['inventory'])
        if path=='/api/profile-inventory' and code==200:
            fields=('checkpoints','tasks','stamp')
            if not isinstance(data,dict) or not all(type(data.get(k)) is int and data[k]>=0 for k in fields):
                return self.reply(502,{'error':'资源统计不可用'})
            data={k:data[k] for k in fields}
            save_json(PROFILES/'inventory'/(worker.user+'.json'),data)
            data=member_inventory(worker.user,data)
        self.reply(code,data)

    def login(self, body, token):
        user=body.get('username')
        node=body.get('node', DEFAULT_NODE)
        if node not in LOGIN_NODES:return self.reply(400,{'error':'请选择列表中的登录节点'})
        password=body.pop('password',None)
        if not isinstance(user,str) or not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}',user) or not isinstance(password,str) or not password or len(password)>512 or any(c in password for c in '\x00\r\n'):
            return self.reply(400,{'error':'请输入有效的 H800 用户名和密码'})
        with guard:
            now=time.monotonic()
            attempts[user]=[v for v in attempts[user] if now-v<300]
            if len(attempts[user])>=5:return self.reply(429,{'error':'尝试次数过多，请五分钟后重试'})
            if not auth_slots.acquire(False):return self.reply(429,{'error':'登录繁忙，请稍后重试'})
            attempts[user].append(now)
        try:
            worker=Worker(user,password,node)
        except PermissionError:return self.reply(401,{'error':'H800 用户名或密码错误'})
        except (ConnectionError, TimeoutError, subprocess.TimeoutExpired):
            return self.reply(503,{'error':node+' SSH 连接失败或工作进程未就绪，请选择其他节点重试'})
        finally:auth_slots.release()
        key=secrets.token_urlsafe(32)
        digest=hashlib.sha256(key.encode()).hexdigest()
        try:save_json(worker.directory/'session.json',dict(hash=digest,user=worker.user,born=worker.born,node=node))
        except OSError:
            worker.close()
            raise
        with guard:
            attempts.pop(user,None)
            old=sessions.pop(token,None)
            sessions[digest]=worker
        if old:old.close()
        result={'user':user}
        if self.bearer:result.update(session=key,maxAge=MAX_AGE)
        self.reply(200,result,cookie=key)

    def do_GET(self):
        try:self.handle_request()
        except (BrokenPipeError,ConnectionResetError):pass
        except (ValueError,TypeError,http.cookies.CookieError):self.reply(400,{'error':'请求格式无效'})
        except (OSError,subprocess.SubprocessError):self.reply(503,{'error':'H800 会话无法连接，请重新登录'})

    do_POST=do_GET


class TeamHTTPS(ThreadingHTTPServer):
    def __init__(self,address,port,hostname,cert,key):
        self.hosts={f'{hostname}:{port}', f'{address}:{port}'}
        self.context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version=ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(cert,key)
        super().__init__((address,port),Handler)

    def process_request_thread(self, sock, address):
        # Perform the handshake in this client's thread so an idle client cannot block accept().
        try:
            sock.settimeout(15)
            with self.context.wrap_socket(sock, server_side=True) as secured:
                secured.settimeout(90)
                self.finish_request(secured,address)
        except OSError:pass
        finally:self.close_request(sock)


if __name__=='__main__':
    os.umask(0o077)
    published=read_release()
    restore_sessions()
    threading.Thread(target=refresh_releases,daemon=True).start()
    listener=ThreadingHTTPServer(('127.0.0.1',18768),Handler)
    config=Path.home()/'.config/h800-atlas/https.json'
    if config.is_file():
        team=TeamHTTPS(**json.loads(config.read_text()))
        threading.Thread(target=team.serve_forever,daemon=True).start()
    http_config=config.with_name('http.json')
    if http_config.is_file():
        settings=json.loads(http_config.read_text())
        address,port=settings['address'],settings['port']
        intranet=ThreadingHTTPServer((address,port),Handler)
        intranet.team_http=True
        # TLS terminates at the domain reverse proxy. Each Host has one browser Origin.
        intranet.origins={f'{address}:{port}':f'http://{address}:{port}',
                          'atlas.int.mintibot.com':'https://atlas.int.mintibot.com'}
        threading.Thread(target=intranet.serve_forever,daemon=True).start()
    # Keep the existing owner's tunnel pointing to the same authenticated UI.
    class Unix(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
        daemon_threads=True
    try:
        OLD_SOCKET.unlink(missing_ok=True)
        unix=Unix(str(OLD_SOCKET),Handler)
    except OSError as exc:
        print(f'Legacy socket unavailable: {exc}',flush=True)
    else:
        threading.Thread(target=unix.serve_forever,daemon=True).start()
    threading.Thread(target=cleanup,daemon=True).start()
    listener.serve_forever()
