"""Read physical GPU samples from the cluster's local Prometheus service."""
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

FIELDS = {
    'nvidia_gpu_duty_cycle': 'utilization',
    'nvidia_gpu_memory_used_bytes': 'memoryUsed',
    'nvidia_gpu_memory_total_bytes': 'memoryTotal',
}
MAX_AGE = 75  # The NVML exporter is scraped every 30 seconds.
SELECTOR = ('{job="phase8-nvml-exporter",cluster="mingti",GPU_I_ID="",__name__=~"'
            + '|'.join(FIELDS) + '"}[' + str(MAX_AGE) + 's]')


def add_gpu_usage(nodes):
    for node in nodes.values():
        node['gpuCards'] = []
    try:
        url = 'http://127.0.0.1:9091/api/v1/query?' + urlencode({'query': SELECTOR})
        with build_opener(ProxyHandler({})).open(url, timeout=2) as stream:
            response = json.load(stream)
        if response.get('status') != 'success' or response['data']['resultType'] != 'matrix':
            return
        series = response['data']['result']
    except (OSError, ValueError, KeyError):
        return

    cards = {}
    now = time.time()
    for entry in series:
        metric = entry['metric']
        node = metric.get('node')
        ordinal = metric.get('ordinal', '')
        field = FIELDS.get(metric.get('__name__'))
        if node not in nodes or not ordinal.isdigit() or not field or not entry.get('values'):
            continue
        stamp, raw = entry['values'][-1]
        if not 0 <= now - stamp <= MAX_AGE:
            continue
        key = node, int(ordinal)
        card = cards.get(key)
        # A reassigned GPU may still have another time series under the old job label.
        if card is None or stamp > card['sampled']:
            card = cards[key] = dict(index=key[1], sampled=stamp)
        if card['sampled'] != stamp:
            continue
        try:
            value = float(raw)
        except (ValueError, TypeError):
            continue
        if not math.isfinite(value) or value < 0 or (field == 'utilization' and value > 100):
            continue
        card[field] = value if field == 'utilization' else int(value)

    for (node, _), card in sorted(cards.items()):
        nodes[node]['gpuCards'].append(card)


_lock = threading.Lock()
_targets = {}
_discover_after = 0
_sample = {}
_next_sample = 0
_metric = re.compile(r'^(' + '|'.join(FIELDS) + r')\{([^}]*)\}\s+(\S+)')


def live_targets(now):
    global _targets, _discover_after
    if now >= _discover_after:
        try:
            endpoint = 'http://127.0.0.1:9091/api/v1/targets?state=active'
            with build_opener(ProxyHandler({})).open(endpoint, timeout=1) as stream:
                targets = json.load(stream)['data']['activeTargets']
            _targets = {t['labels']['node']: t['scrapeUrl'] for t in targets
                        if t['labels'].get('job') == 'phase8-nvml-exporter'
                        and t['labels'].get('node')}
            _discover_after = now + 60
        except (OSError, ValueError, KeyError):
            _discover_after = now + 5
    return _targets


def read_cards(target):
    node, url = target
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=1.4) as stream:
            lines = stream.read(2**21).decode().splitlines()
    except (OSError, ValueError):
        return node, []
    cards = {}
    stamp = time.time()
    for line in lines:
        match = _metric.match(line)
        if not match:
            continue
        name, raw_labels, raw = match.groups()
        labels = dict(re.findall(r'(\w+)="([^"]*)"', raw_labels))
        if labels.get('GPU_I_ID') or not labels.get('ordinal', '').isdigit():
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        field = FIELDS[name]
        if not math.isfinite(value) or value < 0 or (field == 'utilization' and value > 100):
            continue
        index = int(labels['ordinal'])
        card = cards.setdefault(index, dict(index=index, sampled=stamp))
        card[field] = value if field == 'utilization' else int(value)
    return node, [cards[i] for i in sorted(cards)]


def live_gpu_sample():
    # One scrape per node for all dashboards served by this gateway.
    global _sample, _next_sample
    with _lock:
        now = time.monotonic()
        if now >= _next_sample:
            targets = live_targets(now)
            with ThreadPoolExecutor(max_workers=8) as pool:
                cards = dict(pool.map(read_cards, targets.items()))
            _sample = dict(nodes=cards, stamp=time.time())
            _next_sample = max(now + 0.9, time.monotonic() + 0.1)
        return _sample
