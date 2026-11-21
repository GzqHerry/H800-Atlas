"""Read and summarize trainer state on both H800 and the archival host."""
import json
import math


def number(v):
    if isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v):
        return v
    return None


def read_state(path, safe):
    try:
        checked=safe(path)
        if checked.is_file() and checked.stat().st_size<=64*1024*1024:
            with checked.open() as stream:
                value=json.load(stream)
                if isinstance(value,dict):
                    return value
    except (ValueError,OSError,RuntimeError):
        pass
    return {}


def checkpoint_metrics(state, step, own_state):
    if own_state and number(state.get('global_step')) is not None:
        step=state['global_step']
    epoch=number(state.get('epoch')) if own_state or state.get('global_step')==step else None
    exact_epoch=loss=lr=loss_step=lr_step=None
    history=state.get('log_history')
    for row in history if isinstance(history,list) else ():
        if not isinstance(row,dict):
            continue
        at=number(row.get('step'))
        if at is None or at>step:
            continue
        if at==step and number(row.get('epoch')) is not None:
            exact_epoch=row['epoch']
        if number(row.get('loss')) is not None and (loss_step is None or at>loss_step):
            loss,loss_step=row['loss'],at
        if number(row.get('learning_rate')) is not None and (lr_step is None or at>lr_step):
            lr,lr_step=row['learning_rate'],at
    return dict(step=step,epoch=epoch if epoch is not None else exact_epoch,epochs=number(state.get('num_train_epochs')),
                loss=loss,lossStep=loss_step,lr=lr,lrStep=lr_step)
