"""Read inference checkpoint metadata on the Grus host. No weight tensors are read."""
import hashlib
import json
import re
import socket
from pathlib import Path


CHECKPOINT = re.compile(r'(?:checkpoint-|global_step_)?[0-9]+')
ROOT = Path('/mnt/data0/arx_ckpts')


def read_checkpoint(folder):
    files = {p.name: p.stat().st_size for p in folder.iterdir()
             if not p.is_symlink() and p.is_file()}
    if not files.get('config.json'):
        return None
    for weight in ('model.safetensors', 'pytorch_model.bin'):
        if files.get(weight):
            break
        index = weight + '.index.json'
        if files.get(index):
            shards = json.loads((folder / index).read_text()).get('weight_map')
            if isinstance(shards, dict) and shards and all(files.get(name) for name in set(shards.values())):
                break
    else:
        return None
    marker = folder / '.atlas-grus-source.json'
    row = dict(destination=str(folder), bytes=sum(v for k,v in files.items() if k != marker.name),
               updated=int(folder.stat().st_mtime))
    if not marker.is_file() or marker.is_symlink():
        return row
    task_file = folder.parent / '.atlas-grus-task'
    try:
        record = json.loads(marker.read_text())
        manifest = record['files']
        if (record['schema'] != 1 or record['destination'] != str(folder)
                or not task_file.is_file() or task_file.is_symlink()
                or task_file.read_text() != record['taskKey']
                or set(manifest) != set(files) - {marker.name}
                or not all(re.fullmatch('[0-9a-f]{64}',record[k]) for k in ('sourceKey','payload','sourceStamp'))
                or hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest() != record['payload']):
            return row
        for name, item in manifest.items():
            st = (folder / name).stat()
            if (item['size'] != st.st_size or
                    record['stats'][name] != [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]):
                return row
        row.update({k:record[k] for k in ('taskKey','sourceKey','payload','sourceStamp')})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return row


def inventory():
    if socket.gethostname().split('.')[0].lower() != 'grus':
        raise ValueError('Unexpected host')
    rows = []
    partial = False
    for parent in ROOT.iterdir():
        if parent.name.startswith('.') or parent.is_symlink() or not parent.is_dir():
            continue
        try:
            folders = [parent] if CHECKPOINT.fullmatch(parent.name) else parent.iterdir()
            for folder in folders:
                if CHECKPOINT.fullmatch(folder.name) and not folder.is_symlink() and folder.is_dir():
                    try:
                        row = read_checkpoint(folder)
                        if row:
                            rows.append(row)
                        else:
                            partial = True
                    except (OSError, ValueError, TypeError, AttributeError):
                        partial = True
        except OSError:
            partial = True
    return dict(items=rows, partial=partial)


if __name__ == '__main__':
    print(json.dumps(inventory()))
