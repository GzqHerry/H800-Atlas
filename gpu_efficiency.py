"""Summarize sampled physical-GPU activity over the last 24 hours."""
import json
import math
import threading
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

from charts import read_history

STEP = 30
WINDOW = 86400
LOW = 10
SUSTAINED = 1800
QUERY = 'nvidia_gpu_duty_cycle{job="phase8-nvml-exporter",cluster="mingti",GPU_I_ID=""}[24h]'
_lock = threading.Lock()
_cache = None
_expires = 0


def summarize(series, inventory, end):
    start = end - WINDOW
    count = WINDOW // STEP
    samples = {(node, index): {} for node, total in inventory.items() for index in range(total)}
    for entry in series:
        labels = entry.get('metric', {})
        ordinal = labels.get('ordinal', '')
        if not ordinal.isdigit():
            continue
        key = (labels.get('node'), int(ordinal))
        if key not in samples:
            continue
        allocated = bool(labels.get('jobid') and labels['jobid'] not in ('0', '-1'))
        for stamp, raw in entry.get('values', []):
            try:
                value = float(raw)
                stamp = float(stamp)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value) or not 0 <= value <= 100 or not start <= stamp < end:
                continue
            bucket = int((stamp - start) // STEP)
            previous = samples[key].get(bucket)
            # Reassignment changes job labels. Count each physical GPU only once.
            if previous is None or stamp > previous[0]:
                samples[key][bucket] = (stamp, value, allocated)
    cards = []
    hourly = {node: [[] for _ in range(24)] for node in inventory}
    total_value = known = low = allocated_low = 0
    for (node, index), values in sorted(samples.items()):
        run = longest = card_low = card_allocated_low = 0
        for bucket in range(count):
            sample = values.get(bucket)
            if sample is None:
                run = 0
                continue
            _, value, allocated = sample
            hourly[node][bucket // 120].append(value)
            total_value += value
            known += 1
            if value < LOW:
                low += 1
                card_low += 1
                allocated_low += int(allocated)
                card_allocated_low += int(allocated)
                run += STEP
                longest = max(longest, run)
            else:
                run = 0
        cards.append(dict(node=node, index=index, coverage=len(values) / count,
                          mean=sum(v[1] for v in values.values()) / len(values) if values else None,
                          lowHours=card_low * STEP / 3600,
                          allocatedLowHours=card_allocated_low * STEP / 3600,
                          longestLowSeconds=longest, currentLowSeconds=run))
    nodes = []
    for node, total in sorted(inventory.items()):
        hours = [dict(mean=sum(v) / len(v) if v else None,
                      coverage=len(v) / (total * 120)) for v in hourly[node]]
        nodes.append(dict(name=node, hours=hours))
    expected = len(samples) * count
    return dict(start=start, end=end, stamp=time.time(), step=STEP, threshold=LOW,
                sustainedSeconds=SUSTAINED, cardCount=len(samples),
                coverage=known / expected if expected else 0,
                mean=total_value / known if known else None,
                observedHours=known * STEP / 3600,
                activeHours=total_value / 100 * STEP / 3600,
                lowHours=low * STEP / 3600,
                allocatedLowHours=allocated_low * STEP / 3600,
                sustainedCards=sum(c['longestLowSeconds'] >= SUSTAINED for c in cards),
                cards=cards, nodes=nodes)


def snapshot():
    global _cache, _expires
    with _lock:
        if _cache is not None and time.monotonic() < _expires:
            return _cache
        try:
            history = read_history()
            if not history:
                raise ValueError('No node inventory')
            inventory = {name: int(n['total']) for name, n in history[-1]['nodes'].items() if n['total'] > 0}
            end = int(time.time()) // STEP * STEP
            url = 'http://127.0.0.1:9091/api/v1/query?' + urlencode(dict(query=QUERY, time=end))
            with build_opener(ProxyHandler({})).open(url, timeout=12) as stream:
                result = json.load(stream)
            if result.get('status') != 'success' or result['data']['resultType'] != 'matrix':
                raise ValueError('Invalid monitoring response')
            _cache = summarize(result['data']['result'], inventory, end)
            _expires = time.monotonic() + 60
            return _cache
        except (OSError, ValueError, KeyError, TypeError):
            if _cache is not None:
                return dict(_cache, stale=True)
            return dict(error='GPU 历史监测暂时不可用')

