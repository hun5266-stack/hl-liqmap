#!/usr/bin/env python3
"""하이퍼리퀴드 BTC 청산가 스냅샷 수집기.

두 가지 모드로 돈다.
  --full   리더보드 전체를 훑어 BTC 보유 계정 명단(holders.json)을 갱신한다. 약 18분.
  (기본)   holders.json 의 계정만 재조회한다. 약 35초.

매시간 기본 모드로 찍고 하루 한 번 --full 로 명단을 새로 만드는 조합을 상정한다.
전수를 매시간 돌리면 하루 7시간을 스캔에 쓰게 되고, 명단만 재조회하면
그 사이 새로 진입한 계정을 놓치기 때문이다.

저장 형식은 gzip CSV다. 스냅샷 하나가 수십 KB라 몇 달 쌓아도 레포가 감당한다.
"""
import argparse
import csv
import gzip
import io
import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

INFO = "https://api.hyperliquid.xyz/info"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
UA = {"User-Agent": "hl-liqmap/1.0", "Content-Type": "application/json"}
ROOT = os.path.dirname(os.path.abspath(__file__))
HOLDERS = os.path.join(ROOT, "holders.json")

# 청산가가 현재가에서 이만큼 밖이면 저장하지 않는다. 검증 대상이 아니고
# 전부 담으면 스냅샷이 몇 배로 불어난다.
KEEP_PCT = 25.0
WORKERS = 14


def post(body, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(INFO, data=json.dumps(body).encode(), headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(0.5 * (i + 1))
    return None


def btc_price():
    req = urllib.request.Request(
        "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT",
        headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=20) as r:
        return float(json.load(r)["price"])


def leaderboard():
    req = urllib.request.Request(LEADERBOARD, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.load(r)
    rows = d.get("leaderboardRows", d)
    return [x["ethAddress"] for x in rows]


def fetch(addr):
    """한 주소의 BTC 포지션. 없으면 None."""
    st = post({"type": "clearinghouseState", "user": addr})
    if not st:
        return None
    for p in st.get("assetPositions", []):
        z = p["position"]
        if z["coin"] != "BTC":
            continue
        sz = float(z["szi"] or 0)
        ep = float(z.get("entryPx") or 0)
        if not sz or not ep:
            continue
        lev = z.get("leverage") or {}
        return {
            "a": addr,
            "sz": sz,
            "ep": ep,
            "liq": float(z["liquidationPx"]) if z.get("liquidationPx") else None,
            "lev": lev.get("value"),
            "mt": lev.get("type"),
            "pnl": float(z.get("unrealizedPnl") or 0),
            "av": float(st.get("marginSummary", {}).get("accountValue") or 0),
        }
    return None


def scan(addrs):
    out = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(fetch, addrs):
            if r:
                out.append(r)
    return out


def write_snapshot(rows, px, mode, elapsed, scanned):
    ts = datetime.now(timezone.utc)
    day = ts.strftime("%Y-%m")
    outdir = os.path.join(ROOT, "data", day)
    os.makedirs(outdir, exist_ok=True)
    name = ts.strftime("%Y%m%dT%H%M") + ("_full" if mode == "full" else "") + ".csv.gz"
    path = os.path.join(outdir, name)

    lo, hi = px * (1 - KEEP_PCT / 100), px * (1 + KEEP_PCT / 100)
    near = [r for r in rows if r["liq"] and lo <= r["liq"] <= hi]

    buf = io.StringIO()
    w = csv.writer(buf)
    # 첫 줄은 스냅샷 메타. 실제 실행 시각을 남겨야 나중에 지연을 보정할 수 있다.
    w.writerow(["#ts", ts.isoformat(), "px", f"{px:.2f}", "mode", mode,
                "scanned", scanned, "btc_positions", len(rows),
                "kept", len(near), "elapsed_s", f"{elapsed:.0f}"])
    w.writerow(["addr", "size", "entry", "liq", "lev", "margin", "pnl", "acct_value"])
    for r in near:
        w.writerow([r["a"], f"{r['sz']:.6f}", f"{r['ep']:.2f}", f"{r['liq']:.2f}",
                    r["lev"], r["mt"], f"{r['pnl']:.2f}", f"{r['av']:.2f}"])

    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())

    longs = [r for r in rows if r["sz"] > 0]
    shorts = [r for r in rows if r["sz"] < 0]
    summary = {
        "ts": ts.isoformat(), "price": px, "mode": mode,
        "scanned": scanned, "btc_positions": len(rows), "kept_near": len(near),
        "long_btc": round(sum(r["sz"] for r in longs), 2),
        "short_btc": round(sum(-r["sz"] for r in shorts), 2),
        "long_n": len(longs), "short_n": len(shorts),
        "elapsed_s": round(elapsed),
    }
    with open(os.path.join(ROOT, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return path, summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="리더보드 전체 스캔 후 명단 갱신")
    a = ap.parse_args()

    t0 = time.time()
    px = btc_price()

    if a.full or not os.path.exists(HOLDERS):
        addrs = leaderboard()
        mode = "full"
    else:
        with open(HOLDERS, encoding="utf-8") as f:
            addrs = json.load(f)["addresses"]
        mode = "known"

    rows = scan(addrs)
    elapsed = time.time() - t0

    if mode == "full":
        with open(HOLDERS, "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.now(timezone.utc).isoformat(),
                       "addresses": [r["a"] for r in rows]}, f)

    path, s = write_snapshot(rows, px, mode, elapsed, len(addrs))
    print(f"[{s['ts']}] mode={mode} px=${px:,.0f} scanned={len(addrs):,} "
          f"btc={len(rows):,} kept={s['kept_near']:,} {elapsed:.0f}s")
    print(f"  long {s['long_btc']:,.0f} BTC / short {s['short_btc']:,.0f} BTC")
    print(f"  -> {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
    main()
