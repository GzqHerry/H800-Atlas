"""Read committed W&B history records using the training environment's installed SDK."""
import json
import math
import sys
from wandb.proto import wandb_internal_pb2
from wandb.sdk.internal.datastore import DataStore

def read(path, offset, keys):
    rows = []
    store = DataStore()
    store.open_for_scan(path)
    if offset:
        store.seek(offset)
    committed = store.get_offset()
    while True:
        try:
            data = store.scan_data()
        except AssertionError:
            if not store.in_last_block():
                raise
            break
        if data is None:
            break
        rec = wandb_internal_pb2.Record.FromString(data)
        committed = store.get_offset()
        if not rec.HasField('history'):
            continue
        row = {}
        for item in rec.history.item:
            name = item.key or '/'.join(item.nested_key)
            if not name.startswith(('train/', 'eval/')):
                continue
            namespace, key = name.split('/', 1)
            if namespace == 'eval':
                key = 'eval_' + key
            if key in keys or key == 'global_step':
                value = json.loads(item.value_json)
                if type(value) in (int, float) and math.isfinite(value):
                    row[key] = value
        if 'global_step' in row:
            rows.append(row)
    return [committed, rows]

if __name__ == '__main__':
    print(json.dumps(read(sys.argv[1], int(sys.argv[2]), set(sys.argv[3:]))))
