#!/usr/bin/env python3
"""청산가 지도를 그려 디스코드로 보낸다.

매시간 collect.py 가 끝난 뒤 run.sh 가 호출한다.
웹훅 주소는 /root/.discord_webhook 에서 읽는다. 레포에는 넣지 않는다.
"""
import argparse
import collections
import csv
import glob
import gzip
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import Ellipse, Polygon

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("LIQMAP_DATA") or os.path.join(ROOT, "..", "data")
WEBHOOK_FILE = os.environ.get("LIQMAP_WEBHOOK") or "/root/.discord_webhook"
BG = "#0d1015"
BIN = 250
DAYS = 7

# 윈도우는 맑은 고딕, 우분투는 나눔고딕. 없으면 기본 폰트로 떨어진다.
for _f in ("Malgun Gothic", "NanumGothic", "Noto Sans CJK KR"):
    if any(_f == f.name for f in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = _f
        break
plt.rcParams["axes.unicode_minus"] = False

# 저장은 UTC 로 한다 (파일명·메타 모두). 보여줄 때만 한국시간으로 바꾼다.
KST = timezone(timedelta(hours=9))


def kst(dt):
    """표시용 한국시간. tzinfo 를 떼서 matplotlib 이 다시 변환하지 않게 한다."""
    return dt.astimezone(KST).replace(tzinfo=None)


PLANE = [(0.08, 0.50), (0.94, 0.82), (0.63, 0.12), (0.44, 0.42)]


def signature(fig, handle="@kyokyokyooo", color="#5c6473", knock=BG,
              x=0.985, y=0.018, fs=10.5, scale=1.0, gap=0.45):
    """우측 하단에 텔레그램 마크와 아이디. 글자 높이를 재서 마크 크기를 맞춘다."""
    t = fig.text(x, y, handle, color=color, fontsize=fs,
                 ha="right", va="bottom", family="DejaVu Sans")
    fig.canvas.draw()
    bb = t.get_window_extent(fig.canvas.get_renderer())
    (x0, y0), (x1, y1) = fig.transFigure.inverted().transform(
        [(bb.x0, bb.y0), (bb.x1, bb.y1)])
    asp = fig.get_figwidth() / fig.get_figheight()
    dy = (y1 - y0) * scale
    dx = dy / asp
    cx, cy = x0 - dx * (0.5 + gap), (y0 + y1) / 2
    fig.patches.append(Ellipse((cx, cy), dx, dy, transform=fig.transFigure,
                               facecolor=color, edgecolor="none", zorder=10))
    fig.patches.append(Polygon(
        [(cx + (px - 0.5) * dx * 0.72, cy + (py - 0.5) * dy * 0.72)
         for px, py in PLANE],
        closed=True, transform=fig.transFigure, facecolor=knock,
        edgecolor="none", zorder=11))


def load_snapshots(days):
    """가벼운 형태로 읽는다 — 주소는 버린다.

    7일치를 주소까지 들고 있으면 40만 건이 넘어 955MB 짜리 서버에서 위험하다.
    주소가 필요한 계정 추적은 필요한 스냅샷만 load_full 로 다시 읽는다.
    """
    cut = datetime.now(timezone.utc).timestamp() - days * 86400
    out = []
    for p in sorted(glob.glob(os.path.join(DATA, "*", "*.csv.gz"))):
        with gzip.open(p, "rt", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        ts = datetime.fromisoformat(rows[0][1])
        if ts.timestamp() < cut:
            continue
        pos = [(float(r[1]), float(r[3]) if r[3] else None)
               for r in rows[2:] if r]
        out.append({"ts": ts, "px": float(rows[0][3]), "pos": pos,
                    "scanned": int(rows[0][7]), "path": p})
    return out


def load_full(path):
    """주소를 키로 한 딕셔너리. 계정 단위 추적용."""
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    d = {}
    for r in rows[2:]:
        if not r:
            continue
        d[r[0]] = {"sz": float(r[1]), "liq": float(r[3]) if r[3] else None}
    return d


def candles(t0, t1):
    """바이낸스 5분봉. 도쿄 VPS 에서는 붙는다 (미국 IP 는 451 로 막힌다)."""
    out = {"t": [], "h": [], "l": [], "c": []}
    cur, end = int(t0 * 1000), int(t1 * 1000)
    while cur < end:
        u = ("https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT"
             "&interval=5m&startTime=%d&endTime=%d&limit=1500" % (cur, end))
        try:
            k = json.load(urllib.request.urlopen(
                urllib.request.Request(u, headers={"User-Agent": "liqmap/1.0"}),
                timeout=20))
        except Exception as e:
            print("  [경고] 봉 조회 실패: %s" % e, file=sys.stderr)
            break
        if not k:
            break
        for c in k:
            out["t"].append(c[0])
            out["h"].append(float(c[2]))
            out["l"].append(float(c[3]))
            out["c"].append(float(c[4]))
        cur = k[-1][0] + 300000
    return out


def grid(snaps):
    lo = int(min(s["px"] for s in snaps) * 0.88 // BIN) * BIN
    hi = int(max(s["px"] for s in snaps) * 1.12 // BIN) * BIN
    bins = list(range(lo, hi + BIN, BIN))
    idx = {b: i for i, b in enumerate(bins)}
    G = np.zeros((len(bins), len(snaps)))
    for j, s in enumerate(snaps):
        for sz, liq in s["pos"]:
            if liq is None:
                continue
            b = int(liq // BIN) * BIN
            if b in idx:
                G[idx[b], j] += abs(sz)
    return np.array(bins, float), G


def draw(snaps, K, path, days):
    bins, G = grid(snaps)
    ts = [s["ts"] for s in snaps]
    cur = K["c"][-1] if K["c"] else snaps[-1]["px"]
    hi, lo = (max(K["h"]), min(K["l"])) if K["h"] else (cur, cur)

    m = (bins >= cur * 0.86) & (bins <= cur * 1.14)
    G, bins = G[m], bins[m]

    cmap = LinearSegmentedColormap.from_list(
        "liq", ["#12151c", "#1c3050", "#1f6f8b", "#4fb286",
                "#d8d24a", "#f07d3a", "#e8443a"])
    fig = plt.figure(figsize=(15.8, 9.4))
    fig.patch.set_facecolor(BG)
    gs = fig.add_gridspec(1, 3, width_ratios=[3.4, 1, 0.055],
                          left=0.055, right=0.955, top=0.855, bottom=0.075,
                          wspace=0.035)
    ax = fig.add_subplot(gs[0])
    bx = fig.add_subplot(gs[1], sharey=ax)
    cax = fig.add_subplot(gs[2])

    tn = mdates.date2num([kst(t) for t in ts])
    ex = np.concatenate([[tn[0] - 0.02], (tn[:-1] + tn[1:]) / 2, [tn[-1] + 0.02]])
    ey = np.concatenate([bins - BIN / 2, [bins[-1] + BIN / 2]])
    pm = ax.pcolormesh(ex, ey, np.clip(G, 0, 420), cmap=cmap, shading="flat")

    if K["t"]:
        kt = mdates.date2num([kst(datetime.fromtimestamp(t / 1000, timezone.utc))
                              for t in K["t"]])
        ax.fill_between(kt, K["l"], K["h"], color="white", alpha=0.30,
                        lw=0, zorder=4)
        ax.plot(kt, K["c"], color=BG, lw=3.6, zorder=5)
        ax.plot(kt, K["c"], color="white", lw=1.5, zorder=6)
        for y, lab in ((hi, "실제 고 $%s" % format(hi, ",.0f")),
                       (lo, "실제 저 $%s" % format(lo, ",.0f"))):
            ax.axhline(y, color="white", lw=0.8, ls=(0, (2, 4)),
                       alpha=0.55, zorder=7)
            ax.annotate(lab, (0.006, y), xycoords=("axes fraction", "data"),
                        color="white", fontsize=9, zorder=8,
                        va="bottom" if y == hi else "top")

    fig.text(0.055, 0.955, "하이퍼리퀴드 BTC 청산가 지도", color="white",
             fontsize=17.5, va="top", weight="bold")
    fig.text(0.055, 0.912,
             "청산가 = 수집 스냅샷 %d개 (%s ~ %s KST, 계정 %s개 전수)"
             "   ·   가격 = 바이낸스 5분봉 %s개"
             % (len(snaps), kst(ts[0]).strftime("%m/%d %H:%M"),
                kst(ts[-1]).strftime("%m/%d %H:%M"),
                format(snaps[-1]["scanned"], ","), format(len(K["t"]), ",")),
             color="#8f97a6", fontsize=10.5, va="top")
    fig.text(0.055, 0.882,
             "흰 띠가 5분봉 고저 범위, 가운데 선이 종가 · "
             "배경이 밝을수록 그 가격대에 청산가가 몰려 있다",
             color="#6f7889", fontsize=9.8, va="top")

    ax.set_facecolor(BG)
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.set_ylabel("가격 / 청산가 (USD)", color="#8f97a6", fontsize=11)
    ax.tick_params(colors="#78808f")
    for s in ax.spines.values():
        s.set_color("#2b323d")
    ax.grid(alpha=0.09, color="white", lw=0.5)
    ax.set_xlim(tn[0] - 0.02, tn[-1] + 0.02)

    bx.barh(bins, G[:, -1], height=BIN * 0.92,
            color=["#e8443a" if b < cur else "#3aa0e8" for b in bins])
    bx.axhspan(lo, hi, color="white", alpha=0.09, zorder=0)
    bx.axhline(cur, color="white", lw=1.5, ls=(0, (4, 3)))
    bx.annotate("현재 $%s" % format(cur, ",.0f"), (0.98, cur),
                xycoords=("axes fraction", "data"), color="white",
                fontsize=10, ha="right", va="bottom")
    bx.annotate("흐린 띠 = %d일간\n가격이 닿은 범위" % days, (0.97, 0.985),
                xycoords="axes fraction", color="#7b8494", fontsize=9,
                ha="right", va="top")
    bx.set_facecolor(BG)
    bx.set_title("최신 단면 · %s KST" % kst(ts[-1]).strftime("%m/%d %H:%M"),
                 color="#c8cdd6", fontsize=11, pad=10, loc="left")
    bx.set_xlabel("BTC", color="#8f97a6", fontsize=10)
    bx.tick_params(colors="#78808f", labelleft=False)
    for s in bx.spines.values():
        s.set_color("#2b323d")
    bx.grid(alpha=0.09, color="white", lw=0.5, axis="x")
    bx.annotate("빨강 = 롱 청산(가격 아래)\n파랑 = 숏 청산(가격 위)", (0.97, 0.015),
                xycoords="axes fraction", color="#7b8494", fontsize=9,
                ha="right", va="bottom")

    cb = fig.colorbar(pm, cax=cax)
    cb.set_label("BTC / $%d 구간 (420 이상 동일색)" % BIN, color="#8f97a6",
                 fontsize=9.5, labelpad=10)
    cb.ax.tick_params(colors="#78808f")
    cb.outline.set_edgecolor("#2b323d")

    signature(fig)
    fig.savefig(path, dpi=110, facecolor=BG)
    plt.close(fig)
    return cur


def zone_series(snaps, lo, hi):
    """그 가격 칸에 청산가가 걸린 물량과 계정 수를 시간순으로."""
    out = []
    for s in snaps:
        v = n = 0
        for sz, liq in s["pos"]:
            if liq is not None and lo <= liq < hi:
                v += abs(sz)
                n += 1
        out.append((s["ts"], s["px"], v, n))
    return out


def approach(K, lo, hi, since, above):
    """since 이후 가격이 그 칸 쪽으로 얼마나 갔나. (도달 극단값, 칸에 들어왔는지)

    칸이 현재가 위면 고가의 최대치, 아래면 저가의 최소치가 관심사다.
    칸 안에 들어온 봉의 반대쪽 끝을 집으면 안 된다.
    """
    ext = None
    for t, h, l in zip(K["t"], K["h"], K["l"]):
        if t / 1000 < since:
            continue
        v = h if above else l
        if ext is None or (v > ext if above else v < ext):
            ext = v
    if ext is None:
        return None, False
    return ext, lo <= ext < hi


def fate(path_then, path_now, lo, hi):
    """그때 그 칸에 있던 계정들이 지금 어떻게 됐나.

    stay_now 는 남아 있는 계정의 '지금' 물량이다. 현재 칸 물량에서 이걸 빼면
    그 뒤에 새로 들어온 양이 나온다 — 물량이 늘었을 때 갈아탄 건지
    원래 있던 게 버틴 건지 갈린다.
    """
    A, B = load_full(path_then), load_full(path_now)
    coh = {a: v for a, v in A.items()
           if v["liq"] is not None and lo <= v["liq"] < hi}
    if not coh:
        return None
    r = {"n": len(coh), "btc": sum(abs(v["sz"]) for v in coh.values()),
         "gone": [0, 0.0], "moved": [0, 0.0], "stay": [0, 0.0],
         "grew": 0, "cut": 0, "stay_now": 0.0}
    for a, v in coh.items():
        n = B.get(a)
        if n is None or n["sz"] == 0:
            k = "gone"
        elif n["liq"] is not None and lo <= n["liq"] < hi:
            k = "stay"
            r["stay_now"] += abs(n["sz"])
        else:
            k = "moved"
            if abs(n["sz"]) > abs(v["sz"]) * 1.05:
                r["grew"] += 1
            elif abs(n["sz"]) < abs(v["sz"]) * 0.95:
                r["cut"] += 1
        r[k][0] += 1
        r[k][1] += abs(v["sz"])
    return r


def narrate(snaps, K, cur, hours=24):
    """가장 큰 자리가 그동안 어떻게 변했는지, 줄었다면 청산인지 이탈인지."""
    if len(snaps) < 3:
        return []

    then = min(snaps, key=lambda s: abs(
        (snaps[-1]["ts"] - s["ts"]).total_seconds() - hours * 3600))
    if then is snaps[-1]:
        then = snaps[0]

    # 두 가지를 본다. 제일 큰 자리는 멀리 있을 수 있고(현재 -9% 에 1,385 BTC),
    # 가까운 자리는 작아도 실제로 밟힐 자리다. 하나만 고르면 다른 쪽이 안 보인다.
    def peak(src, px, span):
        c = collections.Counter()
        for sz, liq in src["pos"]:
            if liq is None or abs(liq - px) / px > span:
                continue
            c[int(liq // BIN) * BIN] += abs(sz)
        if not c:
            return None
        # 군집은 한 칸보다 넓다. 3칸 창의 합이 가장 큰 봉우리를 고른다
        return max(c, key=lambda b: sum(c.get(b + k * BIN, 0) for k in (-1, 0, 1)))

    cands = []
    big = peak(snaps[-1], cur, 0.15)
    near = peak(snaps[-1], cur, 0.035)
    if big is not None:
        cands.append((big, "가장 큰 자리"))
    if near is not None:
        cands.append((near, "가장 가까운 자리"))

    zones = []
    for b, tag in cands:
        if all(abs(b - z) > 2 * BIN for z, _ in zones):   # 겹치면 같은 군집이다
            zones.append((b, tag))

    NOW = load_full(snaps[-1]["path"])
    out = []
    for b, tag in zones[:2]:
        # 실제 군집은 $250 한 칸보다 넓게 퍼져 있다. 봉우리 칸 좌우를 묶는다.
        lo, hi = b - BIN, b + 2 * BIN
        ser = zone_series(snaps, lo, hi)
        i0 = ser.index(next(x for x in ser if x[0] == then["ts"]))
        v0, vN = ser[i0][2], ser[-1][2]
        above = b > cur
        chg = (vN - v0) / v0 * 100 if v0 else 0.0
        near, hit = approach(K, lo, hi, then["ts"].timestamp(), above)
        f = fate(then["path"], snaps[-1]["path"], lo, hi)
        mem = sorted((abs(x["sz"]) for x in NOW.values()
                      if x["liq"] is not None and lo <= x["liq"] < hi), reverse=True)

        out.append("")
        out.append("**%s — $%s~%s**  (현재가 %+.1f%%)"
                   % (tag, format(lo, ","), format(hi, ","),
                      (b - cur) / cur * 100))

        # 한 문장: 무엇이 얼마나 걸려 있고, 몇 명이 나눠 갖고 있나.
        s1 = ("터지면 강제 %s가 나올 %s %s BTC($%sM)가 걸려 있"
              % ("매수" if above else "매도", "숏" if above else "롱",
                 format(vN, ",.0f"), format(vN * cur / 1e6, ",.0f")))
        if mem:
            share = mem[0] / sum(mem)
            if share >= 0.5:
                s1 += ("고, %d개 계정 중 하나가 %s BTC로 %.0f%%를 차지한다 — "
                       "그 한 명이 빠지면 절반이 사라진다."
                       % (len(mem), format(mem[0], ",.0f"), share * 100))
            else:
                s1 += ("고, %d개 계정이 나눠 가져 가장 큰 하나도 %.0f%%에 그친다."
                       % (len(mem), share * 100))
        else:
            s1 += "다."
        out.append(s1)

        # 한 문장: 하루 사이 어떻게 변했고, 가격이 실제로 닿았나.
        cl = ["하루 전 %s BTC에서 %.0f%% %s"
              % (format(v0, ",.0f"), abs(chg), "불었" if chg > 0 else "줄었")]
        if f and vN > 0:
            fresh = max(vN - f["stay_now"], 0.0)
            if fresh / vN >= 0.3:      # 새 물량이 적으면 굳이 안 적는다
                cl[-1] += "는데"
                cl.append("그중 %s BTC가 새로 들어온 것이고"
                          % format(fresh, ",.0f"))
            else:
                cl[-1] += "고"
        else:
            cl[-1] += "고"
        if near is not None:
            cl.append("가격은 $%s까지%s"
                      % (format(near, ",.0f"),
                         (" 올라 이 구간 안으로 들어왔다" if above
                          else " 내려 이 구간 안으로 들어왔다") if hit else
                         ("밖에 못 올라가 이 구간에는 닿지 않았다" if above
                          else "밖에 못 내려가 이 구간에는 닿지 않았다")))
        out.append(" ".join(cl).rstrip(" 고는데") + ".")

        # 마지막 문장은 갈리는 대목일 때만. 뻔하면 안 쓴다.
        if f:
            turn = f["stay"][0] / f["n"] if f["n"] else 1.0
            if chg <= -25 and not hit:
                out.append("가격이 닿지도 않았는데 줄었으니 청산이 아니라 스스로 뺀 것이다.")
            elif chg <= -25:
                out.append("가격이 닿은 뒤 줄었는데, 하루 전 %d개 중 %d개는 포지션을 닫았고 "
                           "%d개는 청산가만 옮겼다 — 청산과 이탈이 섞여 있다."
                           % (f["n"], f["gone"][0], f["moved"][0]))
            elif chg >= 25 and turn < 0.3:
                out.append("원래 있던 %d개 중 %d개만 남았으니 자리는 같아도 사람이 통째로 바뀌었다."
                           % (f["n"], f["stay"][0]))
    if out:
        out.append("")
        out.append("-# 청산가는 하이퍼리퀴드가 계산한 값이다. cross 계정은 그 포지션이 아니라 "
                   "계좌 전체 담보 기준이라 \"여기서 이 계좌가 무너진다\"에 가깝다. "
                   "가격이 도착하기 전에 스스로 빠지는 물량이 많으니 예약된 체결로 읽으면 안 된다.")
    return out


def bin_vol(snap, cur, span=0.06):
    """현재가 주변 칸별 물량. (칸 -> BTC)"""
    c = collections.Counter()
    for sz, liq in snap["pos"]:
        if liq is None or abs(liq - cur) / cur > span:
            continue
        c[int(liq // BIN) * BIN] += abs(sz)
    return c


def span_range(K, t0, t1):
    """두 시각 사이 가격이 훑고 간 범위."""
    hs = [h for t, h in zip(K["t"], K["h"]) if t0 <= t / 1000 < t1]
    ls = [l for t, l in zip(K["t"], K["l"]) if t0 <= t / 1000 < t1]
    return (min(ls), max(hs)) if hs else (None, None)


def find_liquidation(snaps, K, within_h=24):
    """가격이 실제로 통과한 칸에서 물량이 사라졌는지 찾는다.

    이 프로젝트가 던진 질문 자체다. 지도에서 물량이 빠지는 것은 흔한데,
    그중 '가격이 그 자리를 지나갔고 그때 없어진 것' 만이 진짜 청산이다.
    통과하지 않았는데 빠졌으면 자발적 이탈이다.
    """
    best = None
    cut = snaps[-1]["ts"].timestamp() - within_h * 3600
    for i in range(len(snaps) - 1):
        a, b = snaps[i], snaps[i + 1]
        if b["ts"].timestamp() < cut:      # 최근 것만 소식이다
            continue
        lo_p, hi_p = span_range(K, a["ts"].timestamp(), b["ts"].timestamp())
        if lo_p is None:
            continue
        va = bin_vol(a, a["px"], 0.10)
        for bn, v in va.items():
            if v < 80:                      # 너무 얇으면 잡음이다
                continue
            if not (lo_p <= bn + BIN / 2 <= hi_p):   # 가격이 그 칸을 지났나
                continue
            vb = bin_vol(b, b["px"], 0.10).get(bn, 0.0)
            drop = (v - vb) / v
            if drop < 0.6:
                continue
            if best is None or v * drop > best[0]:
                best = (v * drop, i, bn, v, vb, a, b)
    if best is None:
        return None

    _, _, bn, v, vb, a, b = best
    A, B = load_full(a["path"]), load_full(b["path"])
    coh = {k: x for k, x in A.items()
           if x["liq"] is not None and bn <= x["liq"] < bn + BIN}
    gone = sum(abs(x["sz"]) for k, x in coh.items()
               if k not in B or B[k]["sz"] == 0)
    moved = sum(abs(x["sz"]) for k, x in coh.items()
                if k in B and B[k]["sz"] != 0
                and not (B[k]["liq"] is not None
                         and bn <= B[k]["liq"] < bn + BIN))
    return {"bin": bn, "before": v, "after": vb, "gone": gone, "moved": moved,
            "n": len(coh), "t": b["ts"]}


def alerts(snaps, K, cur, hours=24):
    """조건이 맞을 때만 뜨는 것들. 평소에는 아무것도 안 뜬다."""
    out = []
    if len(snaps) < 4:
        return out
    then = min(snaps, key=lambda s: abs(
        (snaps[-1]["ts"] - s["ts"]).total_seconds() - hours * 3600))
    now, prev = snaps[-1], snaps[-2]
    vn, v0, vp = (bin_vol(now, cur), bin_vol(then, cur), bin_vol(prev, cur))

    # ① 가격이 실제로 지나간 자리에서 물량이 사라졌나 — 진짜 청산
    liq = find_liquidation(snaps, K)
    if liq:
        out.append("**청산 흔적** $%s~%s 칸을 가격이 통과했고 %s → %s BTC 로 빠졌다. "
                   "%d개 계정 중 %s BTC 는 포지션이 사라졌고 %s BTC 는 청산가만 옮겼다. "
                   "(%s KST)"
                   % (format(liq["bin"], ","), format(liq["bin"] + BIN, ","),
                      format(liq["before"], ",.0f"),
                      format(liq["after"], ",.0f"), liq["n"],
                      format(liq["gone"], ",.0f"), format(liq["moved"], ",.0f"),
                      kst(liq["t"]).strftime("%m/%d %H:%M")))

    # ② 없던 자리에 새로 생긴 군집
    fresh = [(b, v) for b, v in vn.items() if v >= 200 and v0.get(b, 0) < 50]
    if fresh:
        b, v = max(fresh, key=lambda x: x[1])
        out.append("**새 군집** $%s~%s 에 %s BTC 가 새로 생겼다. 하루 전에는 %s BTC 였다. (%+.1f%%)"
                   % (format(b, ","), format(b + BIN, ","), format(v, ",.0f"),
                      format(v0.get(b, 0), ",.0f"), (b - cur) / cur * 100))

    # ③ 한 계정이 그 칸을 사실상 혼자 채우고 있나
    B = load_full(now["path"])
    for b, v in sorted(vn.items(), key=lambda x: -x[1])[:3]:
        if v < 150:
            break
        mem = [abs(x["sz"]) for x in B.values()
               if x["liq"] is not None and b <= x["liq"] < b + BIN]
        if mem and max(mem) / sum(mem) >= 0.5:
            out.append("**한 명이 절반** $%s~%s 의 %s BTC 중 %s BTC 가 계정 하나다 (%.0f%%). "
                       "군집이 아니라 큰 한 명이다."
                       % (format(b, ","), format(b + BIN, ","), format(v, ",.0f"),
                          format(max(mem), ",.0f"), max(mem) / sum(mem) * 100))
            break

    # ④ 위아래 연료 균형이 뒤집혔나
    def bal(vv, px):
        u = sum(x for bb, x in vv.items() if px < bb <= px * 1.03)
        d = sum(x for bb, x in vv.items() if px * 0.97 <= bb < px)
        return u, d
    u1, d1 = bal(vn, cur)
    u0, d0 = bal(v0, then["px"])
    if min(u1, d1, u0, d0) > 30:
        if (u0 > d0) != (u1 > d1):
            out.append("**균형 반전** ±3%% 안 연료가 하루 전 %s(위):%s(아래) 였는데 "
                       "지금 %s:%s 로 뒤집혔다."
                       % (format(u0, ",.0f"), format(d0, ",.0f"),
                          format(u1, ",.0f"), format(d1, ",.0f")))

    # ⑤ 가격이 멀어지는데 오히려 쌓이는 자리
    for b, v in sorted(vn.items(), key=lambda x: -x[1])[:5]:
        o = v0.get(b, 0)
        if o < 50 or v < o * 1.4:
            continue
        d_now, d_then = abs(b - cur), abs(b - then["px"])
        if d_now > d_then * 1.3:
            out.append("**멀어지는데 쌓인다** $%s~%s 는 가격에서 멀어졌는데도 "
                       "%s → %s BTC 로 늘었다."
                       % (format(b, ","), format(b + BIN, ","),
                          format(o, ",.0f"), format(v, ",.0f")))
            break

    # ⑥ 한 시간 만에 현재가 주변 총량이 크게 변했나
    tn, tp = sum(vn.values()), sum(vp.values())
    if tp > 200 and abs(tn - tp) / tp >= 0.35:
        out.append("**한 시간 급변** 현재가 ±6%% 안 총량이 %s → %s BTC (%+.0f%%)."
                   % (format(tp, ",.0f"), format(tn, ",.0f"),
                      (tn - tp) / tp * 100))

    return out[:4]


def summary(snaps, cur, extra=None):
    """머리말 한 줄과 본문. 숫자 표는 빼고 서술만 남긴다 —
    같은 값이 아래 서술에 문장으로 다시 나오고, 표는 잘 안 읽힌다."""
    s = snaps[-1]
    prev = snaps[-2]["px"] if len(snaps) > 1 else cur
    chg = (cur - prev) / prev * 100
    head = ("**BTC $%s**  (%+.2f%% / 1h)   ·   %s KST"
            % (format(cur, ",.0f"), chg, kst(s["ts"]).strftime("%m-%d %H:%M")))
    return "\n".join([head] + (extra or []))


def post(url, text, png):
    b = "----liqmap"
    body = io.BytesIO()

    def w(x):
        body.write(x.encode() if isinstance(x, str) else x)

    w("--%s\r\n" % b)
    w('Content-Disposition: form-data; name="payload_json"\r\n')
    w("Content-Type: application/json\r\n\r\n")
    w(json.dumps({"content": text}) + "\r\n")
    w("--%s\r\n" % b)
    w('Content-Disposition: form-data; name="files[0]"; filename="liqmap.png"\r\n')
    w("Content-Type: image/png\r\n\r\n")
    with open(png, "rb") as f:
        w(f.read())
    w("\r\n--%s--\r\n" % b)

    req = urllib.request.Request(
        url, data=body.getvalue(),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % b,
                 "User-Agent": "liqmap/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/liqmap.png")
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--dry", action="store_true", help="그리기만 하고 보내지 않는다")
    a = ap.parse_args()

    snaps = load_snapshots(a.days)
    if not snaps:
        print("스냅샷 없음")
        return 1
    K = candles(snaps[0]["ts"].timestamp(),
                datetime.now(timezone.utc).timestamp())
    cur = draw(snaps, K, a.out, a.days)
    body = narrate(snaps, K, cur)
    al = alerts(snaps, K, cur)
    if al:
        body = ["", "**눈에 띄는 것**"] + ["· " + a for a in al] + body
    text = summary(snaps, cur, body)
    print(text)
    print("-> %s" % a.out)

    if a.dry:
        return 0
    if not os.path.exists(WEBHOOK_FILE):
        print("[건너뜀] 웹훅 파일이 없다: %s" % WEBHOOK_FILE)
        return 0
    url = open(WEBHOOK_FILE).read().strip()
    if not url:
        print("[건너뜀] 웹훅이 비어 있다")
        return 0
    print("디스코드 전송: %s" % post(url, text, a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
