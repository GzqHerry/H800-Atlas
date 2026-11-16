import os
import json
import math
import re
import socket
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

SAMPLE = """import time
def counters():
    with open('/proc/stat') as f:v=list(map(int,f.readline().split()[1:9]))
    return sum(v),v[3]+v[4]
a,b=counters();time.sleep(0.3);c,d=counters()
print(round(100*(1-(d-b)/(c-a)),1) if c>a else -1)
"""
retry_after={}
def sample(host):
    try:
        if host.split('.')[0].lower()==socket.gethostname().split('.')[0].lower():
            cmd=['python3','-']
        else:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*',host) or time.monotonic()<retry_after.get(host,0):return None
            known=Path('/scratch')/Path.home().name/'h800-atlas-node-keys'
            cmd=['ssh','-oBatchMode=yes','-oConnectTimeout=2','-oStrictHostKeyChecking=yes',
                 '-oUserKnownHostsFile='+str(known),host,'python3 -']
        p=subprocess.run(cmd,input=SAMPLE,capture_output=True,text=True,check=True,timeout=4)
        value=float(p.stdout.strip())
        return value if 0<=value<=100 else None
    except (ValueError,OSError,subprocess.SubprocessError):
        retry_after[host]=time.monotonic()+300
        return None

def add_cpu_usage(nodes):
    with ThreadPoolExecutor(max_workers=6) as pool:
        for node,value in zip(nodes.values(),pool.map(sample,nodes)):
            node.setdefault('cpu',{})['busy']=value


_lock = threading.Lock()
_urls = []
_discover_at = 0
_refresh_at = 0
_previous = {}
_result = {}
_counter = re.compile(r'^node_cpu_seconds_total\{cpu="(\d+)",mode="(\w+)"\} (\S+)$')


def cpu_targets(now):
    global _urls, _discover_at
    if now >= _discover_at:
        try:
            with build_opener(ProxyHandler({})).open('http://127.0.0.1:9091/api/v1/targets?state=active', timeout=1) as stream:
                targets = json.load(stream)['data']['activeTargets']
            _urls = [t['scrapeUrl'] for t in targets if t['labels'].get('job') == 'phase8-node-exporter']
            _discover_at = now + 60
        except (OSError, ValueError, KeyError):
            _discover_at = now + 5
    return _urls


def read_cpu_counters(url):
    query = urlencode([('collect[]', 'cpu'), ('collect[]', 'uname')])
    try:
        with build_opener(ProxyHandler({})).open(url+'?'+query, timeout=1.5) as stream:
            raw = stream.read(2**21).decode()
        name = re.search(r'^node_uname_info\{[^\n]*?\bnodename="([^"]+)"', raw, re.M)
        if not name:
            return None
        cores = {}
        for line in raw.splitlines():
            match = _counter.match(line)
            if match:
                core, mode, value = match.groups()
                value = float(value)
                if math.isfinite(value) and value >= 0:
                    cores.setdefault(int(core), {})[mode] = value
        return name[1], cores
    except (OSError, ValueError):
        return None


def cpu_intervals(current, previous):
    rows = []
    for core, values in sorted(current.items()):
        old = previous.get(core, {})
        rows.append([core, None, None])
        if old.keys() != values.keys() or not {'user', 'system', 'idle'} <= values.keys():
            continue
        delta = {m: v-old[m] for m, v in values.items()}
        total = sum(delta.values())
        if total <= 0 or min(delta.values()) < 0:
            continue
        user = delta['user'] + delta.get('nice', 0)
        kernel = delta['system'] + delta.get('irq', 0) + delta.get('softirq', 0)
        rows[-1] = [core, round(user*100/total, 1), round(kernel*100/total, 1)]
    return rows


def live_cpu_sample():
    global _refresh_at, _previous, _result
    with _lock:
        now = time.monotonic()
        if now >= _refresh_at:
            with ThreadPoolExecutor(max_workers=6) as pool:
                current = dict(r for r in pool.map(read_cpu_counters, cpu_targets(now)) if r)
            # After a pause or counter reset wait for two recent readings.
            previous = _previous if now-_refresh_at < 6 else {}
            nodes = {host: cpu_intervals(cores, previous.get(host, {})) for host, cores in current.items()}
            _result = dict(nodes=nodes, stamp=time.time())
            _previous = current
            _refresh_at = time.monotonic() + 1.5
        return _result
