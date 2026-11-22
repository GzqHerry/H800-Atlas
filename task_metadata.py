"""Training-task labels from saved configuration, never from output directory names.

Only bounded text/JSON/YAML files are read; launch scripts are not executed and
training_args.bin (pickle) is deliberately not loaded.
"""
import json
import math
import re
import shlex
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


def mapping(value):
    return value if isinstance(value, dict) else {}


def at(value, key):
    for part in key.split('.'):
        value = mapping(value).get(part)
    return value


def first(*values):
    return next((v for v in values if v is not None and v != ''), None)


def positive(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def flag(value):
    return value if type(value) is bool else None


def text(value):
    return value.strip()[:240] if isinstance(value, str) and value.strip() else None


def read_text(path, safe, limit=2*1024*1024):
    try:
        p = safe(path)
        if p.is_file() and p.stat().st_size <= limit:
            return p.read_text(encoding='utf-8')
    except (OSError, ValueError, RuntimeError):
        pass
    return ''


def document(path, safe):
    raw = read_text(path, safe)
    if not raw:
        return {}
    try:
        value = json.loads(raw) if path.suffix == '.json' else yaml.safe_load(raw) if yaml else {}
        return mapping(value)
    except (ValueError, RecursionError):
        return {}
    except Exception as exc:
        if yaml and isinstance(exc, yaml.YAMLError):
            return {}
        raise


def assignments(script):
    """Resolve literal shell assignments only; ignore substitutions and commands."""
    result = {}
    for line in script.splitlines():
        line = line.strip()
        if line.startswith('export '):
            line = line[7:]
        if not re.match(r'^[A-Z][A-Z0-9_]*=', line):
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            continue
        for token in tokens:
            match = re.fullmatch(r'([A-Z][A-Z0-9_]*)=(.*)', token)
            if not match:
                break
            key, value = match.groups()
            value = re.sub(r'\$\{([A-Z][A-Z0-9_]*)\}|\$([A-Z][A-Z0-9_]*)',
                           lambda m: result.get(m[1] or m[2], m[0]), value)
            if not any(c in value for c in '$`\n;'):
                result[key] = value
    return result


def describe(group, checkpoints, safe, dataset_safe=None, source_group=None):
    group = Path(group)
    source_group = str(source_group or group)
    sources = []

    def read(path):
        value = document(Path(path), safe)
        if value:
            sources.append(str(path))
        return value

    env = assignments(read_text(group/'train_attempt.sh', safe))
    folder = group/'launch_configs'

    def config(variable, filename):
        path = Path(env.get(variable, str(folder/filename)))
        if source_group != str(group) and Path(source_group) in path.parents:
            path = group/path.relative_to(source_group)
        return read(path if path.is_absolute() else group/path)

    launch = read(folder/'launch_manifest.json') or read(group/'launch_manifest.json')
    # A copied or misplaced manifest must not label another experiment.
    if launch.get('output_dir') not in (None, source_group):
        launch = {}
    runtime = config('TRAIN_RUNTIME_CONFIG_FILE', 'runtime.yaml')
    if not runtime:
        runtime = read(group/'training-config.json')
    model_doc = config('MODEL_CONFIG_FILE', 'model.yaml')
    model = mapping(model_doc.get('model', model_doc))
    data = config('DATASET_CONFIG_FILE', 'data.yaml')
    topology = config('ACCELERATE_CONFIG', 'accelerate.yaml')
    training = mapping(runtime.get('training', runtime))
    effective = read(group/'training_args.json')
    # A task-local W&B config records resolved TrainingArguments after overrides.
    try:
        runs = sorted((group/'wandb').glob('*run-*/files/config.yaml'), reverse=True)
    except OSError:
        runs = []
    for path in runs[:8]:
        doc = read(path)
        values = {k: v.get('value') if isinstance(v, dict) and 'value' in v else v for k, v in doc.items()}
        if values.get('output_dir') == source_group:
            effective = values
            break
    state = saved = {}
    for checkpoint in sorted(checkpoints, key=lambda p: int(re.search(r'\d+$', str(p))[0]), reverse=True)[:4]:
        if not saved:
            saved = read(Path(checkpoint)/'config.json')
        if not state:
            state = read(Path(checkpoint)/'trainer_state.json')
        if saved and state:
            break
    if not saved:
        saved = read(group/'config.json')
    if not state:
        state = read(group/'trainer_state.json')
    saved = {**saved, **effective}
    if env:
        sources.append(str(group/'train_attempt.sh'))

    def numeric_env(key):
        value = env.get(key, '')
        return int(value) if value.isdigit() and int(value) > 0 else None

    gpu_env = (numeric_env('NUM_NODES') or 1) * numeric_env('NUM_GPUS_PER_NODE') if numeric_env('NUM_GPUS_PER_NODE') else None
    epochs = first(positive(effective.get('num_train_epochs')), positive(state.get('num_train_epochs')),
                   positive(training.get('num_train_epochs')), positive(launch.get('epochs')))
    batch = first(positive(effective.get('per_device_train_batch_size')), numeric_env('BATCH_SIZE'),
                  positive(training.get('per_device_train_batch_size')), positive(launch.get('per_gpu_batch')))
    accumulation = first(positive(effective.get('gradient_accumulation_steps')), numeric_env('GRADIENT_ACCUMULATION'),
                         positive(training.get('gradient_accumulation_steps')), positive(launch.get('gradient_accumulation_steps')))
    gpus = first(positive(effective.get('world_size')), gpu_env, positive(topology.get('num_processes')),
                 positive(launch.get('gpus')))
    if topology.get('use_cpu') is True:
        gpus = 0
    global_batch = batch*gpus*accumulation if batch and gpus and accumulation else None
    base_path = text(first(model.get('model_name_or_path'), launch.get('init_checkpoint')))
    recipe = text(launch.get('initialization_recipe'))
    # A recipe is usable only when its recorded input agrees with model.yaml.
    if base_path and launch.get('init_checkpoint') not in (None, base_path):
        recipe = None
    base = recipe or (Path(base_path).name if base_path else None)
    if not recipe and base and re.search(r'(?:^|_)pi05_base(?:_|$)', base):
        base = 'pi05_base'
    rlt = first(flag(at(saved, 'rlt.enabled')), flag(model.get('rlt_enabled')), flag(launch.get('rlt_enabled')))
    rtc = first(flag(at(saved, 'dit_config.generator_cfg.pi0_use_training_rtc')),
                flag(model.get('pi0_use_training_rtc')), flag(launch.get('training_rtc_enabled')))
    mem = first(flag(at(saved, 'visual_memory.enabled')), flag(model.get('use_visual_memory')), flag(launch.get('mem_enabled')))
    method = text(first(training.get('training_method'), runtime.get('training_method'), launch.get('training_method')))
    datasets = data.get('datasets', [])
    datasets = datasets if isinstance(datasets, list) else []
    # An action-supervision objective on recorded demonstrations establishes SFT.
    if not method and datasets and positive(at(runtime, 'runtime.objectives.action')):
        method = 'SFT'
    if method:
        method = method.upper()
    robots, ids = [], []
    for item in datasets or ([{'path': launch['dataset']}] if isinstance(launch.get('dataset'), str) else []):
        if not isinstance(item, dict):
            continue
        path = text(first(item.get('path'), item.get('repo_id')))
        info = {}
        if path and Path(path).is_absolute() and dataset_safe:
            info_path = Path(path)/'meta/info.json'
            info = document(info_path, dataset_safe)
            if info:
                sources.append(str(info_path))
        robot = text(first(info.get('robot_type'), item.get('robot_type'), at(item, 'robot.type')))
        if robot:
            robots.append(robot.replace('_', ' '))
        identifier = text(first(item.get('dataset_id'), item.get('id')))
        if not identifier and path:
            match = re.search(r'/work/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/dataset(?:/|$)', path.replace(chr(92), '/'), re.I)
            identifier = match[1] if match else None
        if identifier:
            ids.append(identifier)
    explicit_robot = text(first(data.get('robot_type'), model.get('robot_type'), launch.get('robot_type')))
    if not robots and explicit_robot:
        robots.append(explicit_robot.replace('_', ' '))
    return dict(base=base, basePath=base_path, method=method, rlt=rlt, rtc=rtc, mem=mem,
                epochs=epochs, gpus=gpus, batch=batch, accumulation=accumulation, globalBatch=global_batch,
                robots=list(dict.fromkeys(robots)), datasetIds=list(dict.fromkeys(ids)), sources=list(dict.fromkeys(sources)))


def merge(primary, fallback):
    """Keep explicit False values; fill only absent facts from a verified copy."""
    result = {key: first(value, fallback.get(key)) for key, value in primary.items()}
    for key in ('robots', 'datasetIds'):
        result[key] = primary.get(key) or fallback.get(key, [])
    result['sources'] = list(dict.fromkeys(primary.get('sources', []) + fallback.get('sources', [])))
    return result
