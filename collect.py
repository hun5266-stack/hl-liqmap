#!/usr/bin/env python3
"""하이퍼리퀴드 BTC 청산가 스냅샷 수집기.

두 가지 모드로 돈다.
  --full   리더보드 전체를 훑어 BTC 보유 계정 명단(holders.json)을 갱신한다. 약 18분.
  (기본)   holders.json 의 계정만 재조회한다. 약 70초.

매시간 --full 로 돈다. 공개 레포는 Actions 무료 분 제한이 없어
전수를 매번 돌려도 비용이 들지 않는다. 기본 모드는 수동 실행용으로 남겨둔다.

저장 형식은 gzip CSV다. 스냅샷 하나가 수십 KB라 몇 달 쌓아도 레포가 감당한다.
"""
import argparse
import csv
import gzip
import io
import json
import math
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import market

INFO = "https://api.hyperliquid.xyz/info"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
UA = {"User-Agent": "hl-liqmap/1.0", "Content-Type": "application/json"}
ROOT = os.path.dirname(os.path.abspath(__file__))
HOLDERS = os.path.join(ROOT, "holders.json")

# 러너(4코어) 실측: 36워커 초당 122건 실패 0, 64워커 200건 실패 19,
# 100워커 333건 실패 74(25%). 36이 무손실 한계선이다.
#
# 다만 짧은 벤치마크의 순간 처리율과 실전 지속 처리율이 다르다. 300건을
# 재면 초당 122건(전수 6분)인데 44,000건을 18분 돌리면 실제로는 18.6분이
# 걸린다. 지속 부하에서 API가 속도를 낮추는 것으로 보인다.
#
# 속도보다 무손실이 중요하다. 빠진 계정은 지도의 구멍이 되고
# 나중에 소급할 방법이 없다.
WORKERS = 36

# 중복 실행을 막는 기본 간격. 실행 주체마다 달라서 --min-gap 으로 덮어쓴다.
#
# VPS 는 정시 슬롯의 주인이라 30분을 쓴다. 전수 스캔이 18분 걸려 스냅샷이
# HH:18 에 찍히고 다음 정시에는 42분 전으로 보인다. 기본값 45분이면 매번
# 걸러져 두 시간에 한 번밖에 못 돈다.
#
# Actions 는 백업이라 70분을 쓴다. VPS 가 정상이면 항상 걸러지고,
# VPS 가 슬롯을 놓쳤을 때만 깨어난다.
MIN_GAP_MIN = 45


class RequestFailure(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__(str(errors[-1]))


def post(body, tries=3):
    errors = []
    for i in range(tries):
        try:
            req = urllib.request.Request(INFO, data=json.dumps(body).encode(), headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r), i+1, errors
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append({'kind':type(exc).__name__, 'code':getattr(exc,'code',None),
                           'message':str(exc)[:250]})
            if i < tries-1:
                time.sleep(2 ** i)
    raise RequestFailure(errors)


def btc_price():
    """하이퍼리퀴드 마크가를 쓴다.

    바이낸스는 GitHub Actions 러너 IP(미국)를 지역 차단해 451을 준다.
    그리고 청산가를 계산하는 주체가 하이퍼리퀴드이므로, 기준 가격도
    같은 곳에서 받아야 지도와 가격이 어긋나지 않는다.
    """
    d, _, _ = post({'type':'metaAndAssetCtxs'})
    i = next(i for i,u in enumerate(d[0]['universe']) if u['name']=='BTC')
    px = float(d[1][i]['markPx'])
    if not math.isfinite(px) or px<=0:
        raise ValueError('invalid mark price')
    return {'t':int(time.time()*1000),'px':px,'source':'hyperliquid_mark'}


def leaderboard():
    req = urllib.request.Request(LEADERBOARD, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.load(r)
    rows = d.get('leaderboardRows') if isinstance(d,dict) else d
    if not isinstance(rows,list) or not rows:
        raise ValueError('빈 리더보드 또는 잘못된 응답')
    addrs = [x['ethAddress'].lower() for x in rows]
    if any(len(a)!=42 or not a.startswith('0x') for a in addrs):
        raise ValueError('잘못된 리더보드 주소')
    for a in addrs:
        int(a[2:],16)
    return sorted(set(addrs))


def fetch(addr):
    """Explicit result per address; an unsuccessful read is never no-BTC."""
    result = {'addr':addr,'attempts':0,'errors':[],'row':None}
    try:
        st, result['attempts'], result['errors'] = post({'type':'clearinghouseState','user':addr})
        if not isinstance(st,dict) or not isinstance(st.get('assetPositions'),list) or not isinstance(st.get('marginSummary'),dict):
            raise ValueError('missing assetPositions/marginSummary')
        positions = [p['position'] for p in st['assetPositions']]
        btc = [z for z in positions if z['coin']=='BTC' and float(z['szi'])!=0]
        if len(btc)>1:
            raise ValueError('duplicate BTC position')
        result['status'] = 'ok_no_btc'
        if btc:
            z = btc[0]
            row = {'a':addr,'sz':float(z['szi']),'ep':float(z['entryPx']),
                   'liq':float(z['liquidationPx']) if z.get('liquidationPx') is not None else None,
                   'lev':float(z['leverage']['value']),'mt':z['leverage']['type'],
                   'pnl':float(z['unrealizedPnl']),'av':float(st['marginSummary']['accountValue'])}
            if row['ep']<=0 or row['lev']<=0 or row['mt'] not in ('cross','isolated') or any(not math.isfinite(row[k]) for k in ('sz','ep','pnl','av','lev')) or (row['liq'] is not None and (not math.isfinite(row['liq']) or row['liq']<=0)):
                raise ValueError('invalid BTC position values')
            result.update(status='ok_btc',row=row)
    except RequestFailure as exc:
        result.update(status='transport_error',attempts=len(exc.errors),errors=exc.errors)
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        result['status'] = 'invalid_response'
        result['errors'].append({'kind':type(exc).__name__,'message':str(exc)[:250]})
    result['observed_at'] = datetime.now(timezone.utc).isoformat()
    return result


def scan(addrs):
    results = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(fetch, addrs):
            results[r['addr']] = r
        failed = [a for a,r in results.items() if r['status'] not in ('ok_btc','ok_no_btc')]
        if failed:
            time.sleep(5)
            for r in ex.map(fetch, failed):
                old = results[r['addr']]
                r['attempts'] += old['attempts']
                r['errors'] = old['errors']+r['errors']
                r['recovered'] = r['status'] in ('ok_btc','ok_no_btc')
                results[r['addr']] = r
    return [r['row'] for r in results.values() if r['status']=='ok_btc'], results


def write_snapshot(rows, px, mode, elapsed, scanned, audit=None):
    ts = datetime.fromisoformat(audit['completed_at']) if audit else datetime.now(timezone.utc)
    day = ts.strftime("%Y-%m")
    outdir = os.path.join(ROOT, "data", day)
    os.makedirs(outdir, exist_ok=True)
    name = ts.strftime("%Y%m%dT%H%M%S%f") + ("_full" if mode == "full" else "") + ".csv.gz"
    path = os.path.join(outdir, name)

    # 필터를 두지 않는다. 3주를 모으는 동안 가격이 15~20% 움직이면
    # 오늘 먼 청산가가 나중엔 가까워진다. 그때 데이터가 없으면 소급이 안 되고,
    # 전량 저장해도 스냅샷이 120KB 남짓이라 아낄 이유가 없다.
    buf = io.StringIO()
    w = csv.writer(buf)
    # 첫 줄은 스냅샷 메타. 실제 실행 시각을 남겨야 나중에 지연을 보정할 수 있다.
    meta = ["#ts", ts.isoformat(), "px", f"{px:.2f}", "mode", mode,
                "scanned", scanned, "btc_positions", len(rows),
                "elapsed_s", f"{elapsed:.0f}"]
    if audit:
        meta += ['schema_version',2,'started_at',audit['started_at'],
                 'price_at',audit['price']['t'],'price_source',audit['price']['source'],
                 'complete',str(audit['complete']).lower(),'failed',audit['counts']['failed']]
        market.write_json_gz(path.replace('.csv.gz','.status.json.gz'),audit)
    w.writerow(meta)
    w.writerow(["addr", "size", "entry", "liq", "lev", "margin", "pnl", "acct_value"])
    for r in rows:
        w.writerow([r["a"], f"{r['sz']:.6f}", f"{r['ep']:.2f}",
                    f"{r['liq']:.2f}" if r["liq"] else "",
                    r["lev"], r["mt"], f"{r['pnl']:.2f}", f"{r['av']:.2f}"])

    with gzip.open(path+'.tmp', "wt", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())
    os.replace(path+'.tmp',path)

    longs = [r for r in rows if r["sz"] > 0]
    shorts = [r for r in rows if r["sz"] < 0]
    summary = {
        "ts": ts.isoformat(), "price": px, "mode": mode,
        "scanned": scanned, "btc_positions": len(rows),
        "long_btc": round(sum(r["sz"] for r in longs), 2),
        "short_btc": round(sum(-r["sz"] for r in shorts), 2),
        "long_n": len(longs), "short_n": len(shorts),
        "elapsed_s": round(elapsed),
    }
    if audit:
        summary.update(complete=audit['complete'],counts=audit['counts'],
                       started_at=audit['started_at'],price_at=audit['price']['t'])
    with open(os.path.join(ROOT, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return path, summary


def last_snapshot_age_min():
    """가장 최근 스냅샷이 몇 분 전인지. 없으면 None."""
    base = os.path.join(ROOT, "data")
    if not os.path.isdir(base):
        return None
    names = []
    for d in os.listdir(base):
        p = os.path.join(base, d)
        if os.path.isdir(p):
            names += [n for n in os.listdir(p) if n.endswith(".csv.gz")]
    if not names:
        return None
    stamp = max(n.split("_")[0].replace(".csv.gz", "") for n in names)
    try:
        t = datetime.strptime(stamp[:13], "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="리더보드 전체 스캔 후 명단 갱신")
    ap.add_argument("--force", action="store_true", help="간격 무시하고 강제 실행")
    ap.add_argument("--min-gap", type=int, default=MIN_GAP_MIN,
                    help="직전 스냅샷과의 최소 간격(분)")
    a = ap.parse_args()

    age = last_snapshot_age_min()
    if age is not None and age < a.min_gap and not a.force:
        print(f"직전 스냅샷이 {age:.0f}분 전이다 (최소 간격 {a.min_gap}분). 건너뛴다")
        return

    t0 = time.time()
    started_at = datetime.now(timezone.utc).isoformat()
    start_price = btc_price()
    previous = []
    if os.path.exists(HOLDERS):
        with open(HOLDERS,encoding='utf-8') as f:
            previous = [a.lower() for a in json.load(f)['addresses']]

    if a.full or not os.path.exists(HOLDERS):
        listed = leaderboard()
        addrs = sorted(set(listed) | set(previous))
        mode = "full"
    else:
        addrs = sorted(set(previous))
        listed = []
        mode = "known"

    rows, results = scan(addrs)
    price_error = None
    try:
        price = btc_price()
    except Exception as exc:
        price = start_price
        price_error = f'{type(exc).__name__}: {exc}'[:250]
    px = price['px']
    elapsed = time.time() - t0
    completed_at = datetime.now(timezone.utc).isoformat()
    failed = [a for a,r in results.items() if r['status'] not in ('ok_btc','ok_no_btc')]
    counts = {'requested':len(addrs),'ok_btc':len(rows),
              'ok_no_btc':sum(r['status']=='ok_no_btc' for r in results.values()),
              'failed':len(failed),'recovered':sum(r.get('recovered',False) for r in results.values()),
              'attempts':sum(r['attempts'] for r in results.values())}
    listed_set = set(listed)
    accounts = {a:{**{k:v for k,v in r.items() if k not in ('row','addr')},
                   'in_leaderboard':a in listed_set if mode=='full' else None}
                for a,r in results.items()}
    audit = {'schema_version':2,'started_at':started_at,'completed_at':completed_at,
             'complete':not failed,'counts':counts,'price':price,'start_price':start_price,
             'end_price_error':price_error,'accounts':accounts}
    # A failed observation never removes a known holder. A successful no-BTC does.
    retained = sorted({r['a'] for r in rows} | (set(previous)&set(failed)))
    with open(HOLDERS+'.tmp','w',encoding='utf-8') as f:
        json.dump({'updated':completed_at,'addresses':retained,'unconfirmed':failed},f)
    os.replace(HOLDERS+'.tmp',HOLDERS)
    path, s = write_snapshot(rows, px, mode, elapsed, len(addrs),audit)
    # Prices are archived before GitHub publication. Recorder lives outside this repo.
    since = t0-min((age or 60)*60,86400)
    try:
        points = market.read_marks(since,time.time())
    except OSError as exc:
        print(f'[경고] 마크가격 기록 읽기 실패: {exc}')
        points = []
    for p in (start_price,price):
        points.append(p)
    points = sorted({p['t']:p for p in points}.values(),key=lambda p:p['t'])
    market.write_json_gz(path.replace('.csv.gz','.marks.json.gz'),points)
    try:
        cs = market.candle_history(since,time.time())
        market.write_json_gz(path.replace('.csv.gz','.candles.json.gz'),cs)
    except Exception as exc:
        print(f'[경고] 가격 캔들 저장 실패: {type(exc).__name__}: {exc}')
    print(f"[{s['ts']}] mode={mode} px=${px:,.0f} scanned={len(addrs):,} "
          f"btc={len(rows):,} {elapsed:.0f}s")
    print(f"  long {s['long_btc']:,.0f} BTC / short {s['short_btc']:,.0f} BTC")
    print(f"  -> {os.path.relpath(path, ROOT)}")
    print(f"  성공 {counts['ok_btc']+counts['ok_no_btc']:,} / 최종 실패 {len(failed):,} / 재조회 복구 {counts['recovered']:,}")


if __name__ == "__main__":
    main()
