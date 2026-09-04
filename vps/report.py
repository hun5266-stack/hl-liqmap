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
from datetime import datetime, timezone

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
                    "scanned": int(rows[0][7])})
    return out


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

    tn = mdates.date2num(ts)
    ex = np.concatenate([[tn[0] - 0.02], (tn[:-1] + tn[1:]) / 2, [tn[-1] + 0.02]])
    ey = np.concatenate([bins - BIN / 2, [bins[-1] + BIN / 2]])
    pm = ax.pcolormesh(ex, ey, np.clip(G, 0, 420), cmap=cmap, shading="flat")

    if K["t"]:
        kt = mdates.date2num([datetime.fromtimestamp(t / 1000, timezone.utc)
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
             "청산가 = 수집 스냅샷 %d개 (%s ~ %s UTC, 계정 %s개 전수)"
             "   ·   가격 = 바이낸스 5분봉 %s개"
             % (len(snaps), ts[0].strftime("%m/%d %H:%M"),
                ts[-1].strftime("%m/%d %H:%M"),
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
    bx.set_title("최신 단면 · %s" % ts[-1].strftime("%m/%d %H:%M"),
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


def summary(snaps, cur):
    s = snaps[-1]
    lb, sb = collections.Counter(), collections.Counter()
    for sz, liq in s["pos"]:
        if liq is None or abs(liq - cur) / cur > 0.12:
            continue
        (lb if sz > 0 else sb)[int(liq // BIN) * BIN] += abs(sz)

    up2 = sum(v for b, v in sb.items() if cur < b <= cur * 1.02)
    dn2 = sum(v for b, v in lb.items() if cur * 0.98 <= b < cur)
    # 멀리 있는 큰 자리보다 가까운 쪽이 쓸모 있다. ±5% 안에서 고른다.
    NEAR = 0.05
    ups = [(b, v) for b, v in sb.items() if cur < b <= cur * (1 + NEAR)]
    dns = [(b, v) for b, v in lb.items() if cur * (1 - NEAR) <= b < cur]
    topu = max(ups, key=lambda x: x[1], default=None)
    topd = max(dns, key=lambda x: x[1], default=None)

    prev = snaps[-2]["px"] if len(snaps) > 1 else cur
    chg = (cur - prev) / prev * 100
    L = [p for p in s["pos"] if p[0] > 0]
    S = [p for p in s["pos"] if p[0] < 0]

    out = ["**BTC $%s**  (%+.2f%% / 1h)   ·   %s UTC"
           % (format(cur, ",.0f"), chg, s["ts"].strftime("%m-%d %H:%M")),
           "```",
           "±2%% 안    위(숏청산) %6s BTC    아래(롱청산) %6s BTC"
           % (format(up2, ",.0f"), format(dn2, ",.0f"))]
    if topu:
        out.append("±5% 안 가장 큰 자리")
        out.append("   ↑ $%-9s %6s BTC  %+.1f%%"
                   % (format(topu[0], ","), format(topu[1], ",.0f"),
                      (topu[0] - cur) / cur * 100))
    if topd:
        out.append("   ↓ $%-9s %6s BTC  %+.1f%%"
                   % (format(topd[0], ","), format(topd[1], ",.0f"),
                      (topd[0] - cur) / cur * 100))
    out.append("포지션         롱 %s개 %s BTC   숏 %s개 %s BTC"
               % (format(len(L), ","), format(sum(p[0] for p in L), ",.0f"),
                  format(len(S), ","), format(sum(-p[0] for p in S), ",.0f")))
    out.append("```")
    return "\n".join(out)


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
    text = summary(snaps, cur)
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
