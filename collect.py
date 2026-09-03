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
# VPS 는 정시 슬롯의 주인이라 40분을 쓴다. 전수 스캔이 18분 걸려 스냅샷이
# HH:18 에 찍히고 다음 정시에는 42분 전으로 보인다. 기본값 45분이면 매번
# 걸러져 두 시간에 한 번밖에 못 돈다.
#
# Actions 는 백업이라 70분을 쓴다. VPS 가 정상이면 항상 걸러지고,
# VPS 가 슬롯을 놓쳤을 때만 깨어난다.
MIN_GAP_MIN = 45


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
    """하이퍼리퀴드 마크가를 쓴다.

    바이낸스는 GitHub Actions 러너 IP(미국)를 지역 차단해 451을 준다.
    그리고 청산가를 계산하는 주체가 하이퍼리퀴드이므로, 기준 가격도
    같은 곳에서 받아야 지도와 가격이 어긋나지 않는다.
    """
    d = post({"type": "metaAndAssetCtxs"})
    if d:
        try:
            i = next(k for k, u in enumerate(d[0]["universe"]) if u["name"] == "BTC")
            return float(d[1][i]["markPx"])
        except (StopIteration, KeyError, TypeError, ValueError):
            pass
    d = post({"type": "allMids"})
    if d and "BTC" in d:
        return float(d["BTC"])
    raise RuntimeError("BTC 가격 조회 실패")


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

    # 필터를 두지 않는다. 3주를 모으는 동안 가격이 15~20% 움직이면
    # 오늘 먼 청산가가 나중엔 가까워진다. 그때 데이터가 없으면 소급이 안 되고,
    # 전량 저장해도 스냅샷이 120KB 남짓이라 아낄 이유가 없다.
    buf = io.StringIO()
    w = csv.writer(buf)
    # 첫 줄은 스냅샷 메타. 실제 실행 시각을 남겨야 나중에 지연을 보정할 수 있다.
    w.writerow(["#ts", ts.isoformat(), "px", f"{px:.2f}", "mode", mode,
                "scanned", scanned, "btc_positions", len(rows),
                "elapsed_s", f"{elapsed:.0f}"])
    w.writerow(["addr", "size", "entry", "liq", "lev", "margin", "pnl", "acct_value"])
    for r in rows:
        w.writerow([r["a"], f"{r['sz']:.6f}", f"{r['ep']:.2f}",
                    f"{r['liq']:.2f}" if r["liq"] else "",
                    r["lev"], r["mt"], f"{r['pnl']:.2f}", f"{r['av']:.2f}"])

    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())

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
        t = datetime.strptime(stamp, "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
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
        # 스캔이 중간에 깨지면 결과가 잘린 채로 명단을 덮어쓸 수 있다.
        # 직전 명단보다 크게 줄었으면 갱신을 건너뛴다.
        prev_n = 0
        if os.path.exists(HOLDERS):
            try:
                with open(HOLDERS, encoding="utf-8") as f:
                    prev_n = len(json.load(f)["addresses"])
            except Exception:
                prev_n = 0
        if prev_n and len(rows) < prev_n * 0.7:
            print(f"  [경고] BTC 포지션 {len(rows):,}개가 직전 {prev_n:,}개보다 30% 이상 적다. "
                  f"명단 갱신을 건너뛴다")
        else:
            with open(HOLDERS, "w", encoding="utf-8") as f:
                json.dump({"updated": datetime.now(timezone.utc).isoformat(),
                           "addresses": [r["a"] for r in rows]}, f)

    path, s = write_snapshot(rows, px, mode, elapsed, len(addrs))
    print(f"[{s['ts']}] mode={mode} px=${px:,.0f} scanned={len(addrs):,} "
          f"btc={len(rows):,} {elapsed:.0f}s")
    print(f"  long {s['long_btc']:,.0f} BTC / short {s['short_btc']:,.0f} BTC")
    print(f"  -> {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
    main()
