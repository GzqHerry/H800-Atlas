"""Relay an authenticated Atlas upload using read-only files opened by the submitting UID."""
import array
import contextlib
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import stat
import struct
import tempfile

import grus

ADDRESS = '\0h800-atlas.tianji'
MAX_PACKET = 8192


def peer_uid(sock):
    return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def grant_dir():
    return Path('/run/user')/str(os.getuid())/'h800-atlas-tianji-grants'


def authorize(user, path):
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError('无效的 checkpoint 路径')
    grants = grant_dir()
    grants.mkdir(mode=0o700, exist_ok=True)
    entry = grants/secrets.token_hex(16)
    with entry.open('x') as stream:
        json.dump([pwd.getpwnam(user).pw_uid, str(Path(path))], stream)
    return entry


def upload(path, job, update):
    files = grus.inference_files(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
        sock.connect(ADDRESS)
        if peer_uid(sock) != Path(__file__).stat().st_uid:
            raise PermissionError('Tianji 服务身份无效')
        sock.sendall(json.dumps([job['id'], len(files)]).encode())
        for name in files:
            fd = os.open(path/name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != files[name]:
                    raise ValueError('推理文件已更改，请重新提交')
                sock.sendmsg([os.fsencode(name)], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))])
            finally:
                os.close(fd)
        sock.shutdown(socket.SHUT_WR)
        while True:
            packet = sock.recv(MAX_PACKET)
            if not packet:
                raise ConnectionError('Tianji 上传服务连接中断')
            state, fields = json.loads(packet)
            if state == '失败':
                raise RuntimeError(fields['error'])
            if state == '完成' and grus.inference_files(path) != files:
                raise ValueError('上传期间源 checkpoint 已更改，请重新提交')
            update(state, **fields)
            if state == '完成':
                return


def receive(sock, update):
    import nas
    sock.settimeout(30)
    jid, count = json.loads(sock.recv(MAX_PACKET))
    if not re.fullmatch('[0-9a-f]{32}', jid) or type(count) != int or not 1 <= count <= 1024:
        raise ValueError('上传请求无效')
    grant = grant_dir()/jid
    uid, source = json.loads(grant.read_bytes())
    if peer_uid(sock) != uid:
        raise PermissionError('上传任务与当前 H800 账号不匹配')
    grant.unlink()
    source = Path(source)
    if not re.fullmatch(r'(?:checkpoint-|global_step_)?\d+', source.name):
        raise ValueError('无效的 checkpoint 名称')
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as cleanup:
        view = Path(tmp)/source.name
        view.mkdir()
        fds = []
        for _ in range(count):
            raw, descriptors, flags, _ = socket.recv_fds(sock, 512, 1)
            for fd in descriptors:
                cleanup.callback(os.close, fd)
            if len(descriptors) != 1 or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                raise ValueError('缺少文件描述符')
            fd = descriptors[0]
            mode = fcntl.fcntl(fd, fcntl.F_GETFL)
            name = os.fsdecode(raw)
            if (not name or name in ('.', '..') or Path(name).name != name or
                    mode & os.O_PATH or mode & os.O_ACCMODE != os.O_RDONLY or not stat.S_ISREG(os.fstat(fd).st_mode)):
                raise ValueError('只接受本次上传的只读普通文件')
            # All links are created here, in a private directory, from verified open descriptors.
            (view/name).symlink_to('/proc/self/fd/'+str(fd))
            fds.append(fd)
        allowed = grus.inference_files(view, descriptor_view=True)
        if len(allowed) != count:
            raise ValueError('文件清单包含非推理文件')
        sock.settimeout(None)
        job = dict(source=str(source), meta=dict(group=str(source.parent)))
        grus.upload(view, job, update, nas.transfer_files, 'Tianji', source_fds=tuple(fds), source_uid=uid)


if __name__ == '__main__':
    os.umask(0o077)
    with socket.socket(fileno=0) as connection:
        def report(state, **fields):
            connection.sendall(json.dumps([state, fields], ensure_ascii=False).encode())
        try:
            receive(connection, report)
        except Exception as error:
            report('失败', error=str(error)[:300] or 'Tianji 上传失败')
