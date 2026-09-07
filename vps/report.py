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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import market
from vps import tracking

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
SINCE = os.environ.get("LIQMAP_SINCE", "2026-09-03T13:00:00+00:00")

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


def load_snapshots(days, as_of=None, since=SINCE):
    end = as_of or datetime.now(timezone.utc)
    cut = max(end-timedelta(days=days), datetime.fromisoformat(since))
    out = []
    for p in sorted(glob.glob(os.path.join(DATA, '*', '*.csv.gz'))):
        with gzip.open(p,'rt',encoding='utf-8',newline='') as f:
            reader=csv.reader(f)
            meta_row=next(reader)
            meta=dict(zip(meta_row[::2],meta_row[1::2]))
            ts=datetime.fromisoformat(meta['#ts'])
            if not cut<=ts<=end or meta.get('mode')!='full' or 'kept' in meta:
                continue
            next(reader)
            pos=[(float(r[1]),float(r[3]) if r[3] else None) for r in reader if r]
        out.append(dict(ts=ts,px=float(meta['px']),pos=pos,scanned=int(meta['scanned']),
                        path=p,meta=meta,complete=meta.get('complete')=='true'))
    return sorted(out,key=lambda x:x['ts'])


def load_full(path):
    return tracking.load_full(path)


def candles(t0,t1,offline=False):
    rows={}
    for path in sorted(glob.glob(os.path.join(DATA,'*','*.candles.json.gz'))):
        try:
            with gzip.open(path,'rt',encoding='utf-8') as f:
                for r in json.load(f):
                    if t0*1000<=r['t'] and r['T']<t1*1000:
                        rows[r['t']]=r
        except (OSError,ValueError,KeyError,TypeError) as exc:
            print(f'[경고] 가격 파일 읽기 실패: {path}: {exc}',file=sys.stderr)
    if not offline:
        try:
            rows.update({r['t']:r for r in market.candle_history(t0,t1)})
        except Exception as exc:
            print(f'[경고] 하이퍼리퀴드 캔들 조회 실패: {exc}',file=sys.stderr)
    ordered=[rows[t] for t in sorted(rows)]
    return {k:[r[k] for r in ordered] for k in ('t','h','l','c')}


def load_marks(t0,t1):
    rows={}
    for path in sorted(glob.glob(os.path.join(DATA,'*','*.marks.json.gz'))):
        try:
            with gzip.open(path,'rt',encoding='utf-8') as f:
                for r in json.load(f):
                    if r.get('source')=='hyperliquid_mark' and t0*1000<=r['t']<=t1*1000:
                        rows[r['t']]=r['px']
        except (OSError,ValueError,KeyError,TypeError):
            continue
    for r in market.read_marks(t0,t1):
        rows[r['t']]=r['px']
    return {'t':sorted(rows),'p':[rows[t] for t in sorted(rows)]}


def contact(M,lo,hi,t0,t1):
    points=[(t/1000,p) for t,p in zip(M.get('t',[]),M.get('p',[])) if t0<=t/1000<=t1]
    hit=any(lo<=p<hi for _,p in points)
    cross=any(b[0]-a[0]<=15 and ((a[1]<lo and b[1]>=hi) or (b[1]<lo and a[1]>=hi)) for a,b in zip(points,points[1:]))
    complete=bool(points) and points[0][0]-t0<=15 and t1-points[-1][0]<=15 and all(b[0]-a[0]<=15 for a,b in zip(points,points[1:]))
    if cross:
        return '구간 관통 관측(표본 사이 통과)'
    if hit:
        return '구간 내부 마크가격 관측'
    return '기록된 마크가격에서 미도달' if complete else '마크가격 기록 부족으로 도달 여부 미확인'


def forward_grid(snaps,G):
    # A column starts at completion, never before it. Stop after 90 minutes.
    times=mdates.date2num([kst(x['ts']) for x in snaps])
    edges=[times[0]]
    columns=[]
    for i,t in enumerate(times):
        next_t=times[i+1] if i+1<len(times) else t+1/24
        stop=min(next_t,t+90/1440)
        columns.append(G[:,i])
        edges.append(stop)
        if stop<next_t:
            columns.append(np.full(G.shape[0],np.nan))
            edges.append(next_t)
    return np.array(edges),np.column_stack(columns)


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


def draw(snaps, K, path, days, M=None):
    bins, G = grid(snaps)
    ts = [s["ts"] for s in snaps]
    cur = snaps[-1]["px"]
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
    ex, display = forward_grid(snaps, G)
    ey = np.concatenate([bins, [bins[-1] + BIN]])
    pm = ax.pcolormesh(ex, ey, np.ma.masked_invalid(np.clip(display, 0, 420)), cmap=cmap, shading="flat")
    for i,s in enumerate(snaps):
        if not s.get('complete'):
            stop=min(tn[i]+90/1440,tn[i+1] if i+1<len(tn) else ex[-1])
            ax.axvspan(tn[i],stop,facecolor='none',edgecolor='#697080',hatch='..',linewidth=0,alpha=.30)
    if M and M['t']:
        mt,mp=[],[]
        last=None
        for t,price in zip(M['t'],M['p']):
            dt=mdates.date2num(kst(datetime.fromtimestamp(t/1000,timezone.utc)))
            if last is not None and t-last>15000:
                mt.append(dt); mp.append(np.nan)
            mt.append(dt); mp.append(price)
            last=t
        ax.plot(mt,mp,color='#f4c75e',lw=.9,zorder=8,label='마크가격')

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
             "   ·   가격 = 하이퍼리퀴드 5분봉 %s개"
             % (len(snaps), kst(ts[0]).strftime("%m/%d %H:%M"),
                kst(ts[-1]).strftime("%m/%d %H:%M"),
                format(snaps[-1]["scanned"], ","), format(len(K["t"]), ",")),
             color="#8f97a6", fontsize=10.5, va="top")
    fig.text(0.055, 0.882,
             "흰 띠·선 = 거래가격 고저·종가 · 노랑 = 마크가격(미수집 구간은 표시 없음) · 점무늬 = 조회 완전성 미확인",
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
    ax.set_xlim(ex[0], ex[-1])

    bx.barh(bins + BIN / 2, G[:, -1], height=BIN * 0.92,
            color=["#e8443a" if b < cur else "#3aa0e8" for b in bins])
    if K['t']:
        bx.axhspan(lo, hi, color="white", alpha=0.09, zorder=0)
    bx.axhline(cur, color="white", lw=1.5, ls=(0, (4, 3)))
    bx.annotate("기준 $%s" % format(cur, ",.0f"), (0.98, cur),
                xycoords=("axes fraction", "data"), color="white",
                fontsize=10, ha="right", va="bottom")
    bx.annotate("흐린 띠 = 확보된 거래가격\n캔들의 고저 범위" if K['t'] else "가격 캔들 미확보\n도달 범위 표시 없음", (0.97, 0.985),
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
    bx.annotate("빨강 = 기준가격 아래\n파랑 = 기준가격 위", (0.97, 0.015),
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


def approach(K,lo,hi,since,above):
    bars=[(h,l) for t,h,l in zip(K['t'],K['h'],K['l']) if t/1000>=since]
    if not bars:
        return None,False
    return (max(h for h,l in bars) if above else min(l for h,l in bars),
            any(h>=lo and l<hi for h,l in bars))


def fate(path_then,path_now,lo,hi):
    return tracking.fate(path_then,path_now,lo,hi)


def josa(n, pair=("으로", "로")):
    """숫자 뒤 조사. 끝자리를 읽은 소리의 받침으로 갈린다.

    삼(ㅁ)·육(ㄱ)·영(ㅇ) 은 '으로', 일·칠·팔 은 ㄹ 받침이라 '로',
    이·사·오·구 는 받침이 없어 '로'.
    """
    return pair[0] if int(n) % 10 in (0, 3, 6) else pair[1]


def origin(path_then,path_now,lo,hi):
    return tracking.origin(path_then,path_now,lo,hi)


def narrate(snaps, M, cur, hours=24):
    if len(snaps)<2:
        return []
    now=snaps[-1]
    then=min(snaps[:-1],key=lambda s:abs((now['ts']-s['ts']).total_seconds()-hours*3600))
    gap=(now['ts']-then['ts']).total_seconds()/3600
    def peak(span):
        c=bin_vol(now,cur,span)
        return max(c,key=lambda b:sum(c.get(b+k*BIN,0) for k in (-1,0,1))) if c else None
    zones=[]
    for b in (peak(.15),peak(.035)):
        if b is not None and all(abs(b-z)>2*BIN for z in zones):
            zones.append(b)
    out=[]
    for b in zones:
        lo,hi=b-BIN,b+2*BIN
        members=[x for x in load_full(now['path']).values() if x['liq'] is not None and lo<=x['liq']<hi]
        total=sum(abs(x['sz']) for x in members)
        prev=sum(abs(x['sz']) for x in load_full(then['path']).values() if x['liq'] is not None and lo<=x['liq']<hi)
        out.append(f"\n**${lo:,}~{hi:,}** 관측 {total:,.1f} BTC ({gap:.1f}시간 전 {prev:,.1f} BTC).")
        if total:
            largest=max(abs(x['sz']) for x in members)
            out.append(f"{len(members)}개 계정 중 최대 계정 {largest:,.1f} BTC ({largest/total:.0%}).")
        o=origin(then['path'],now['path'],lo,hi)
        labels={'held':'같은 구간 기존 수량','moved_in':'다른 구간에서 이동한 기존 수량',
                'added':'동일 방향 수량 순증','flipped':'방향 전환 후 수량','new':'BTC 신규 관측(이전 미보유 확인)',
                'unknown':'이전 상태 미확인','became_available':'청산가 표시 전환'}
        pieces=[f'{lab} {o[k][1]:,.1f}' for k,lab in labels.items() if o[k][1]>=.1]
        if pieces:
            out.append('현재 물량 구성(BTC): '+', '.join(pieces)+'.')
        out.append(contact(M,lo,hi,then['ts'].timestamp(),now['ts'].timestamp())+'.')
        f=fate(then['path'],now['path'],lo,hi)
        if f and total<prev:
            labs={'closed':'미보유 확인','reduced':'수량 축소','flipped':'방향 전환',
                  'moved':'청산가 이동','unavailable':'청산가 미제공 전환','unknown':'현재 조회 미확인'}
            parts=[f'{lab} {f[k][1]:,.1f}' for k,lab in labs.items() if f[k][1]>=.1]
            if parts:
                out.append('이전 물량 변화(BTC): '+', '.join(parts)+'. 실제 청산 여부는 미확인.')
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


def find_liquidation(snaps, M, within_h=24):
    """Observed decrease near a mark contact; never confirmation of liquidation."""
    best=None
    for a,b in zip(snaps,snaps[1:]):
        if (snaps[-1]['ts']-b['ts']).total_seconds()>within_h*3600:
            continue
        if (b['ts']-a['ts']).total_seconds()>90*60:
            continue
        for bn,v in bin_vol(a,a['px'],.10).items():
            if v<80:
                continue
            vb=sum(abs(q) for q,l in b['pos'] if l is not None and bn<=l<bn+BIN)
            state=contact(M,bn,bn+BIN,a['ts'].timestamp(),b['ts'].timestamp())
            if vb<=v*.4 and ('내부' in state or '관통' in state):
                item={'bin':bn,'before':v,'after':vb,'t':b['ts'],'contact':state}
                if best is None or v-vb>best['before']-best['after']:
                    best=item
    return best


def alerts(snaps,M,cur,hours=24):
    if len(snaps)<2:
        return []
    now,prev=snaps[-1],snaps[-2]
    gap=(now['ts']-prev['ts']).total_seconds()/3600
    changes=tracking.position_changes(prev['path'],now['path'])
    out=[]
    def side(q):
        return '롱' if q>0 else '숏'
    def lp(x):
        return f"${x['liq']:,.2f}" if x['liq'] is not None else '미제공'
    for kind,label in (('flipped','방향 전환'),('expanded','포지션 확대'),('reduced','포지션 축소')):
        entries=changes[kind]
        if entries:
            r=entries[0]
            a,b=r['before'],r['after']
            addr=r['addr'][:8]+'…'+r['addr'][-4:]
            out.append(f"**{label}** 직전 {gap:.1f}시간 비교에서 {len(entries)}개 계정. 최대 변화 {addr}: "
                       f"{side(a['sz'])} {abs(a['sz']):,.2f} → {side(b['sz'])} {abs(b['sz']):,.2f} BTC, "
                       f"청산가 {lp(a)} → {lp(b)}.")
    liq=find_liquidation(snaps,M)
    if liq:
        out.append(f"**관측 물량 감소** ${liq['bin']:,}~{liq['bin']+BIN:,}: {liq['before']:,.1f} → {liq['after']:,.1f} BTC. "
                   f"{liq['contact']}. 실제 청산 여부는 미확인.")
    # Preserve the concentration warning; no assumption of independent wallets.
    vn=bin_vol(now,cur)
    then=min(snaps[:-1],key=lambda s:abs((now['ts']-s['ts']).total_seconds()-hours*3600))
    baseline_gap=(now['ts']-then['ts']).total_seconds()/3600
    v0=bin_vol(then,cur)
    fresh=[(b,v) for b,v in vn.items() if v>=200 and v0.get(b,0)<50]
    if fresh:
        bn,v=max(fresh,key=lambda x:x[1])
        out.append(f"**관측 군집 증가** ${bn:,}~{bn+BIN:,}: {baseline_gap:.1f}시간 전 {v0.get(bn,0):,.1f} → {v:,.1f} BTC. 신규 자금 유입을 의미하지는 않습니다.")
    B=load_full(now['path'])
    for bn,v in sorted(vn.items(),key=lambda x:-x[1])[:3]:
        mem=[abs(x['sz']) for x in B.values() if x['liq'] is not None and bn<=x['liq']<bn+BIN]
        if v>=150 and mem and max(mem)>=sum(mem)*.5:
            out.append(f"**한 계정이 절반 이상** ${bn:,}~{bn+BIN:,}: {sum(mem):,.1f} BTC 중 최대 계정 {max(mem)/sum(mem):.0%}.")
            break
    def balance(s):
        px=s['px']
        return (sum(abs(q) for q,l in s['pos'] if l is not None and px<l<=px*1.03),
                sum(abs(q) for q,l in s['pos'] if l is not None and px*.97<=l<px))
    u1,d1=balance(now)
    u0,d0=balance(then)
    if min(u1,d1,u0,d0)>30 and (u1>d1)!=(u0>d0):
        out.append(f"**균형 반전** 각 시점 기준가격 ±3% 물량(위:아래): {u0:,.0f}:{d0:,.0f} → {u1:,.0f}:{d1:,.0f} BTC.")
    for bn,v in sorted(vn.items(),key=lambda x:-x[1])[:5]:
        old=v0.get(bn,0)
        if old>=50 and v>=old*1.4 and abs(bn-cur)>abs(bn-then['px'])*1.3:
            out.append(f"**멀어지는데 쌓인다** ${bn:,}~{bn+BIN:,}: 가격에서 멀어졌으나 관측 물량 {old:,.1f} → {v:,.1f} BTC.")
            break
    previous_total=sum(bin_vol(prev,cur).values())
    current_total=sum(vn.values())
    if previous_total>200 and abs(current_total/previous_total-1)>=.35:
        out.append(f"**관측 물량 급변** 직전 {gap:.1f}시간, 현재 기준가격 ±6% 물량 {previous_total:,.1f} → {current_total:,.1f} BTC. 조회 미확인 물량에 유의하세요.")
    return out


def summary(snaps,cur,extra=None,github_failed=False):
    s=snaps[-1]
    info=tracking.status_info(s['path'])
    head=[]
    if github_failed:
        head.append('⚠️ 이번 수집 데이터의 GitHub 저장에 실패했습니다. 이미지·브리핑은 서버 자료로 작성했지만 원본은 이후 분석에서 빠질 수 있습니다.')
    head.append(f"**BTC 기준가격 ${cur:,.2f}** · 수집 완료 {kst(s['ts']):%m-%d %H:%M} KST")
    if info:
        start=kst(datetime.fromisoformat(info['started_at']))
        price=info['price']
        pt=kst(datetime.fromtimestamp(price['t']/1000,timezone.utc))
        c=info['counts']
        head.append(f"수집 {start:%H:%M}~{kst(s['ts']):%H:%M}, 마크가격 관측 {pt:%H:%M:%S}. "
                    f"조회 성공 {c['ok_btc']+c['ok_no_btc']:,}/{c['requested']:,}.")
        if c['failed']:
            head.append(f"⚠️ 재조회 후에도 {c['failed']:,}개 계정 미확인. 물량 감소·신규 여부를 단정하지 않습니다.")
        if info.get('end_price_error'):
            head.append('⚠️ 완료 시 가격 조회 실패: 수집 시작 시 마크가격을 표시합니다.')
    else:
        head.append('과거 자료: 지갑별 조회 성공 여부 미기록. 기준가격은 수집 시작 부근의 값입니다.')
    return '\n'.join(head+(extra or []))


def post(url, text, png):
    b = "----liqmap"
    body = io.BytesIO()

    def w(x):
        body.write(x.encode() if isinstance(x, str) else x)

    w("--%s\r\n" % b)
    w('Content-Disposition: form-data; name="payload_json"\r\n')
    w("Content-Type: application/json\r\n\r\n")
    payload={"content":text if len(text)<=1900 else text[:1800]+"\n…전체 브리핑은 첨부 파일을 확인하세요.",
             "allowed_mentions":{"parse":[]}}
    w(json.dumps(payload) + "\r\n")
    w("--%s\r\n" % b)
    w('Content-Disposition: form-data; name="files[0]"; filename="liqmap.png"\r\n')
    w("Content-Type: image/png\r\n\r\n")
    with open(png, "rb") as f:
        w(f.read())
    if len(text)>1900:
        w("\r\n--%s\r\n" % b)
        w('Content-Disposition: form-data; name="files[1]"; filename="briefing.txt"\r\n')
        w("Content-Type: text/plain; charset=utf-8\r\n\r\n")
        w(text)
    w("\r\n--%s--\r\n" % b)

    req = urllib.request.Request(
        url, data=body.getvalue(),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % b,
                 "User-Agent": "liqmap/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--out',default='/tmp/liqmap.png')
    ap.add_argument('--days',type=int,default=DAYS)
    ap.add_argument('--since',default=SINCE)
    ap.add_argument('--as-of',help='UTC ISO timestamp for reproducible rendering')
    ap.add_argument('--dry',action='store_true',help='do not send to Discord')
    ap.add_argument('--offline',action='store_true',help='no network, implies dry')
    ap.add_argument('--github-failed',action='store_true')
    ap.add_argument('--text-out')
    ap.add_argument('--snapshot',help='report this completed snapshot even if another writer published later')
    args=ap.parse_args()
    end=datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc)
    if args.snapshot:
        with gzip.open(args.snapshot,'rt',encoding='utf-8',newline='') as f:
            end=datetime.fromisoformat(next(csv.reader(f))[1])
    if end.utcoffset() is None:
        ap.error('--as-of needs a timezone')
    snaps=load_snapshots(args.days,end,args.since)
    if not snaps:
        print('스냅샷 없음')
        return 1
    K=candles(snaps[0]['ts'].timestamp(),end.timestamp(),args.offline)
    M=load_marks(snaps[0]['ts'].timestamp(),end.timestamp())
    draw(snaps,K,args.out,args.days,M)
    cur=snaps[-1]['px']
    body=narrate(snaps,M,cur)
    al=alerts(snaps,M,cur)
    extra=[]
    if not K['t']:
        extra.append('⚠️ 하이퍼리퀴드 가격 캔들 없음: 가격 움직임 해석 불가.')
    extra.extend(al+body)
    text=summary(snaps,cur,extra,args.github_failed)
    print(text)
    if args.text_out:
        Path(args.text_out).write_text(text,encoding='utf-8')
    if args.dry or args.offline:
        return 0
    if not os.path.exists(WEBHOOK_FILE):
        print('[건너뜀] 웹훅 파일 없음')
        return 0
    with open(WEBHOOK_FILE,encoding='utf-8') as f:
        url=f.read().strip()
    if url:
        print('디스코드 전송:',post(url,text,args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
