"""Read numeric history with the Atlas-managed W&B SDK, without a cloud account."""
import json
import math
import sys
from wandb.proto import wandb_internal_pb2
from wandb.sdk.internal.datastore import DataStore
from google.protobuf.message import DecodeError

def embedded_job(path):
    """Offline SDK runs can keep Slurm metadata solely in environment records."""
    store=DataStore()
    try:
        store.open_for_scan(path)
        # Environment records normally arrive at startup. Never scan full histories
        # while trying to identify all runs in a project.
        for _ in range(512):
            if store.get_offset()>=4*1024*1024:break
            data=store.scan_data()
            if not data:break
            record=wandb_internal_pb2.Record.FromString(data)
            if record.HasField('environment'):
                slurm=record.environment.slurm
                if slurm.get('array_job_id') and slurm.get('array_task_id'):
                    return slurm['array_job_id']+'_'+slurm['array_task_id']
                job=slurm.get('job_id') or slurm.get('jobid')
                if job:return job
    except (OSError,AssertionError,ValueError,DecodeError):
        pass
    finally:
        if getattr(store,'_fp',None):store._fp.close()
    return ''

def read(path, offset):
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
            if not name or len(name)>160:
                continue
            try:value=json.loads(item.value_json)
            except ValueError:continue
            if type(value) in (int,float) and math.isfinite(value):row[name]=value
        step=next((row[k] for k in ('train/global_step','global_step','step','_step') if k in row),None)
        if step is None and rec.history.HasField('step'):step=rec.history.step.num
        if step is None:continue
        scalars={}
        for key,value in row.items():
            if key.startswith('_') or key in ('train/global_step','global_step','step'):continue
            name=key.removeprefix('train/')
            if name.startswith('eval/'):name='eval_'+name[5:]
            scalars[name]=value
        if scalars:rows.append([step,scalars])
    return [committed, rows]

if __name__ == '__main__':
    if sys.argv[1]=='--jobs':
        result={}
        for path in json.load(sys.stdin):
            # DataStore also raises a plain Exception on unsupported file headers.
            try:result[path]=embedded_job(path)
            except Exception:result[path]=''
        print(json.dumps(result))
    else:
        print(json.dumps(read(sys.argv[1], int(sys.argv[2]))))
