import os
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
