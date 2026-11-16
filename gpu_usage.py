"""Read physical GPU samples from the cluster's local Prometheus service."""
import json
import math
import time
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
