"""Read scalar history from a SwanLab v2 local store (SDK 0.9.3)."""
import math
import os
import re
import struct
import zlib

# Rich wraps long absolute paths in SSH/Slurm output. Match the complete run
# directory, including a line break inside the run name or ID.
announcement = re.compile(rb'swanlab:[^\r\n]{0,120}Run data saved at[ \t]*', re.I)
run_path = re.compile(r'^/.+/run-[\w-]+-([A-Za-z0-9]+)$')
ansi = re.compile(rb'\x1b\[[0-9;]*[A-Za-z]')
cache = {}


def announcements(data, base):
    for match in announcement.finditer(data):
        # Only accept complete lines. On a live log, retry with the next chunk.
        tail = data[match.end():match.end()+3072]
        lines = ansi.sub(b'', tail).split(b'\n')[:-1]
        path = ''
        for line in lines[:16]:
            part = line.decode('utf8', 'replace').strip()
            if not part:
                continue
            if part.startswith('swanlab:') or (not path and not part.startswith('/')):
                break
            path += part
            if run_path.fullmatch(path):
                yield base+match.start(), path


def varint(buf, pos):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(buf):
            break
        b = buf[pos]
        pos += 1
        value |= (b & 127) << shift
        if b < 128:
            return value, pos
    raise ValueError('invalid protobuf varint')


def fields(buf):
    pos = 0
    while pos < len(buf):
        tag, pos = varint(buf, pos)
        wire = tag & 7
        if wire == 0:
            value, pos = varint(buf, pos)
        else:
            if wire == 2:
                length, pos = varint(buf, pos)
            elif wire in (1, 5):
                length = 8 if wire == 1 else 4
            else:
                raise ValueError('unsupported wire type')
            if pos + length > len(buf):
                raise ValueError('truncated field')
            value = buf[pos:pos+length]
            pos += length
        yield tag, value


def scalar(record):
    # Record.scalar=13, ScalarRecord.{key,step,value}=1,2,5.
    for tag, payload in fields(record):
        if tag != 106:
            continue
        row = dict(fields(payload))
        if 10 not in row or 42 not in row:
            continue
        key = row[10].decode('utf8')
        step = row.get(16, 0)
        number = dict(fields(row[42])).get(9, bytes(8))
        value, = struct.unpack('<d', number)
        if key and not key.startswith('__swanlab__.') and step < 2**63 and math.isfinite(value):
            return key, step, value


def records(stream, end):
    fragments = bytearray()
    fragmented = False
    while stream.tell() < end:
        space = 32768 - stream.tell() % 32768
        if space < 7:
            stream.seek(space, 1)
            continue
        if end-stream.tell() < 7:
            return
        crc, size, kind = struct.unpack('<IHB', stream.read(7))
        if size+7 > space or kind not in (1, 2, 3, 4):
            raise ValueError('invalid SwanLab chunk')
        if stream.tell()+size > end:
            return
        data = stream.read(size)
        if zlib.crc32(data, zlib.crc32(bytes([kind]))) != crc:
            raise ValueError('invalid SwanLab checksum')
        if fragmented != (kind in (3, 4)):
            raise ValueError('invalid SwanLab fragment order')
        if kind == 1:
            yield data
        else:
            fragments.extend(data)
            if len(fragments) > 64*1024*1024:
                raise ValueError('oversized SwanLab record')
            fragmented = kind != 4
            if not fragmented:
                yield bytes(fragments)
                fragments.clear()


def history(path):
    with path.open('rb') as stream:
        stat = os.fstat(stream.fileno())
        if stream.read(7) != b':SWL\xd6\xe1\x02':
            raise ValueError('unsupported SwanLab store')
        state = cache.get(path)
        if (not state or state['inode'] != stat.st_ino or stat.st_size < state['size']
                or (stat.st_size == state['size'] and stat.st_mtime_ns != state['mtime'])):
            if len(cache) >= 4:
                cache.pop(next(iter(cache)))
            state = cache[path] = dict(inode=stat.st_ino, size=-1, offset=7, metrics={})
        if state['size'] != stat.st_size:
            stream.seek(state['offset'])
            for record in records(stream, stat.st_size):
                point = scalar(record)
                if point:
                    key, step, value = point
                    state['metrics'].setdefault(key, {})[step] = value
                state['offset'] = stream.tell()
            state.update(size=stat.st_size, mtime=stat.st_mtime_ns)
        return state['metrics']
