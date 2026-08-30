"""러너에서 워커 수별 처리율을 잰다. 로컬과 다른 병목이 있는지 확인용."""
import json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
UA={"User-Agent":"hl-liqmap/1.0","Content-Type":"application/json"}
print("CPU:", os.cpu_count())
lb=json.load(urllib.request.urlopen(urllib.request.Request(
    "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",headers=UA),timeout=120))
rows=lb.get("leaderboardRows",lb); N=len(rows)
def q(a):
    try:
        r=urllib.request.urlopen(urllib.request.Request("https://api.hyperliquid.xyz/info",
            data=json.dumps({"type":"clearinghouseState","user":a}).encode(),headers=UA),timeout=15)
        json.load(r); return 1
    except urllib.error.HTTPError as e: return -e.code
    except Exception: return 0
print(f"{'워커':>5}{'초당':>9}{'성공':>7}{'실패':>7}{'전수환산':>10}")
for i,w in enumerate((36,64,100)):
    sub=[r["ethAddress"] for r in rows[3000+i*300:3000+i*300+300]]
    t0=time.time()
    with ThreadPoolExecutor(max_workers=w) as ex: res=list(ex.map(q,sub))
    el=time.time()-t0; ok=sum(1 for x in res if x==1)
    print(f"{w:>5}{len(sub)/el:>8.1f}{ok:>7}{len(sub)-ok:>7}{N/(len(sub)/el)/60:>8.1f}분")
    time.sleep(2)
