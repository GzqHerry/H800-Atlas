"""H800 Atlas HTTP/HTTPS gateway. One authenticated SSH master per session."""
from typing import NamedTuple
import collections
import http.cookies
import json
import os
from pathlib import Path
import re
import secrets
import select
import ssl
import socketserver
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent.parent
CURRENT = ROOT / 'current'
RUN = Path(os.environ['XDG_RUNTIME_DIR'])
OLD_SOCKET = Path('/scratch') / Path.home().name / 'h800-observatory.sock'
MAX_IDLE = 1800
MAX_AGE = 8 * 3600
COOKIE = 'h800atlas_session'
TLS_COOKIE = '__Host-h800atlas_session'
sessions = {}
attempts = collections.defaultdict(list)
guard = threading.Lock()
auth_slots = threading.BoundedSemaphore(6)


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
    def __init__(self, user, password):
        self.user = user
        self.tmp = tempfile.TemporaryDirectory(dir=RUN, prefix='atlas-')
        control = self.tmp.name + '/ssh'
        self.base = ['/usr/bin/ssh', '-F', '/dev/null', '-oControlPath='+control,
                     '-oStrictHostKeyChecking=yes',
                     '-oUserKnownHostsFile='+str(ROOT/'known_hosts'),
                     '-oGlobalKnownHostsFile=/dev/null', '-oPubkeyAuthentication=no',
                     '-oConnectTimeout=6', '-l', user, '127.0.0.1']
        self.lock = threading.Lock()
        self.child = None
        self.version = None
        self.born = self.seen = time.monotonic()
        try:
            # sshpass consumes the password from an inherited pipe, never argv or env.
            result = subprocess.run(['/usr/bin/sshpass', '-d', '0', '/usr/bin/ssh',
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
            if not os.path.isdir(self.tmp.name):raise PermissionError()
            if self.version!=published.version or self.child is None:self.start()
            try:
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
            self.tmp.cleanup()


def expired(worker, now):
    return now-worker.seen>=MAX_IDLE or now-worker.born>=MAX_AGE


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
                    ('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
                    ('Content-Length',str(len(value)))]:self.send_header(k,v)
        if cookie is not None and not self.team_http:
            key=TLS_COOKIE if self.secure else COOKIE
            self.send_header('Set-Cookie',key+'='+cookie+'; Path=/; HttpOnly; SameSite=Strict'+('; Secure' if self.secure else '')+('; Max-Age=0' if not cookie else ''))
        self.end_headers()
        self.wfile.write(value)

    def handle_request(self):
        host=self.headers.get('Host','')
        if self.secure or self.team_http:
            if host.lower() not in self.server.hosts:return self.reply(403,{'error':'访问地址无效'})
        elif not re.fullmatch(r'(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?',host):
            return self.reply(403,{'error':'访问地址无效'})
        path=urlsplit(self.path).path
        jar=http.cookies.SimpleCookie()
        jar.load(self.headers.get('Cookie',''))
        key=TLS_COOKIE if self.secure else COOKIE
        token=jar[key].value if key in jar else ''
        if self.team_http:
            # Other services share this IP. Cookies are shared across ports.
            scheme,_,credential=self.headers.get('Authorization','').partition(' ')
            token=credential if scheme=='Bearer' else ''
        now=time.monotonic()
        with guard:
            worker=sessions.get(token)
            if worker and expired(worker,now):worker=None
            if worker and path!='/api/version':worker.seen=now
        body={}
        if self.command=='POST':
            action={'/auth/login':'login','/auth/logout':'logout','/api/upload':'upload','/api/remove-local':'remove-local'}.get(path)
            origin=('https://' if self.secure else 'http://')+host
            if not action or self.headers.get('Origin')!=origin or self.headers.get('X-Atlas-Action')!=action:
                return self.reply(403,{'error':'请求来源无效'})
            length=int(self.headers.get('Content-Length',0))
            if not 0<=length<=8192 or (not length and path!='/auth/logout'):
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
            show_app=(path!='/login') if self.team_http else bool(worker)
            data=current.app if show_app else current.login
            data=data.replace(b'__ATLAS_TEAM_HTTP__',b'yes' if self.team_http else b'no')
            return self.reply(200,data.replace(b'__ATLAS_RELEASE__',current.version.encode()),'text/html;charset=utf-8')
        if not worker:return self.reply(401,{'error':'请先登录 H800'})
        if self.command=='GET' and path=='/api/version':return self.reply(200,{'release':published.version})
        if not path.startswith('/api/'):return self.reply(404,{'error':'页面不存在'})
        try:
            code,data=worker.call(self.command,self.path,body)
        except (OSError,subprocess.SubprocessError):
            with guard:removed=sessions.pop(token,None)
            if removed:removed.close()
            return self.reply(401,{'error':'SSH 会话已断开，请重新登录'},cookie='')
        if type(code) is not int or not 200<=code<=599:raise ValueError()
        self.reply(code,data)

    def login(self, body, token):
        user=body.get('username')
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
            worker=Worker(user,password)
        except PermissionError:return self.reply(401,{'error':'H800 用户名或密码错误'})
        finally:auth_slots.release()
        key=secrets.token_urlsafe(32)
        with guard:
            attempts.pop(user,None)
            old=sessions.pop(token,None)
            sessions[key]=worker
        if old:old.close()
        result={'user':user}
        if self.team_http:result['session']=key
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
        intranet.hosts={f'{address}:{port}'}
        threading.Thread(target=intranet.serve_forever,daemon=True).start()
    # Keep the existing owner's tunnel pointing to the same authenticated UI.
    class Unix(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
        daemon_threads=True
    OLD_SOCKET.unlink(missing_ok=True)
    unix=Unix(str(OLD_SOCKET),Handler)
    threading.Thread(target=unix.serve_forever,daemon=True).start()
    threading.Thread(target=cleanup,daemon=True).start()
    listener.serve_forever()
