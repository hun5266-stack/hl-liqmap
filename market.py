"""Hyperliquid public prices. Importing this module never makes requests."""
import argparse
import gzip
import json
import math
import os
from pathlib import Path
import time
import urllib.request
from datetime import datetime, timezone, timedelta

INFO = 'https://api.hyperliquid.xyz/info'
MARK_DIR = Path(os.environ.get('LIQMAP_MARK_DIR', str(Path.home()/'.local/share/hl-liqmap/marks')))


def request(body):
    req = urllib.request.Request(INFO, data=json.dumps(body).encode(),
                                 headers={'Content-Type':'application/json','User-Agent':'hl-liqmap/2'})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def mark_price():
    data = request({'type':'metaAndAssetCtxs'})
    i = next(i for i,u in enumerate(data[0]['universe']) if u['name']=='BTC')
    px = float(data[1][i]['markPx'])
    if not math.isfinite(px) or px <= 0:
        raise ValueError('invalid mark price')
    return {'t':int(time.time()*1000), 'px':px, 'source':'hyperliquid_mark'}


def read_marks(t0, t1, directory=None):
    """Read recorder observations, without connecting to the exchange."""
    directory = Path(directory) if directory is not None else MARK_DIR
    points = {}
    day = datetime.fromtimestamp(t0, timezone.utc).date()
    end = datetime.fromtimestamp(t1, timezone.utc).date()
    while day <= end:
        path = directory / (day.isoformat()+'.jsonl')
        if path.exists():
            with path.open(encoding='utf-8') as f:
                for line in f:
                    try:
                        p = json.loads(line)
                        if p.get('source')=='hyperliquid_mark' and t0*1000 <= p['t'] <= t1*1000 and math.isfinite(p['px']) and p['px']>0:
                            points[p['t']] = p
                    except (ValueError, KeyError, TypeError):
                        # An append in progress or a malformed line is a gap, not a price.
                        continue
        day += timedelta(days=1)
    return [points[t] for t in sorted(points)]


def write_json_gz(path, value):
    path = Path(path)
    tmp = path.with_name(path.name+'.tmp')
    with gzip.open(tmp,'wt',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False)
    os.replace(tmp,path)


def candle_history(t0,t1):
    """Completed Hyperliquid BTC 5m candles, including volume. API retains 5000."""
    start = max(int(t0*1000), int(t1*1000)-4999*300000)
    rows = request({'type':'candleSnapshot','req':{'coin':'BTC','interval':'5m',
                    'startTime':start,'endTime':int(t1*1000)}})
    if not isinstance(rows,list):
        raise ValueError('invalid candle response')
    valid = {}
    for row in rows:
        t, end = int(row['t']),int(row['T'])
        vals = {k:float(row[k]) for k in ('o','h','l','c','v')}
        if not all(math.isfinite(v) for v in vals.values()) or min(vals[k] for k in ('o','h','l','c'))<=0 or vals['h']<vals['l']:
            raise ValueError('invalid candle price')
        if t>=int(t0*1000) and end<int(t1*1000):
            valid[t] = dict(t=t,T=end,**vals)
    return [valid[t] for t in sorted(valid)]


def record_once(directory=None):
    directory = Path(directory) if directory is not None else MARK_DIR
    point = mark_price()
    directory.mkdir(parents=True,exist_ok=True)
    day = datetime.fromtimestamp(point['t']/1000,timezone.utc).strftime('%Y-%m-%d')
    with (directory/(day+'.jsonl')).open('a',encoding='utf-8') as f:
        f.write(json.dumps(point)+'\n')
    return point


def main():
    ap = argparse.ArgumentParser(description='BTC mark recorder; no wallet scans')
    ap.add_argument('--loop',action='store_true')
    ap.add_argument('--interval',type=float,default=5)
    args = ap.parse_args()
    if args.interval < 3:
        ap.error('interval must be >= 3 seconds')
    while True:
        start = time.monotonic()
        try:
            record_once()
        except Exception as exc:
            print(f'mark observation failed: {type(exc).__name__}: {exc}',flush=True)
            if not args.loop:
                raise
        if not args.loop:
            return
        time.sleep(max(0,args.interval-(time.monotonic()-start)))


if __name__=='__main__':
    main()
