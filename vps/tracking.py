"""Endpoint comparisons; missing observations never imply closed positions."""
import csv
import gzip
import json
from functools import lru_cache


@lru_cache(maxsize=8)
def load_full(path):
    with gzip.open(path,'rt',encoding='utf-8',newline='') as f:
        next(f)
        return {r['addr'].lower():{'sz':float(r['size']),
                                  'liq':float(r['liq']) if r['liq'] else None}
                for r in csv.DictReader(f)}


@lru_cache(maxsize=4)
def status_info(path):
    try:
        with gzip.open(str(path).replace('.csv.gz','.status.json.gz'),'rt',encoding='utf-8') as f:
            d=json.load(f)
        return d if isinstance(d,dict) and isinstance(d.get('accounts'),dict) else {}
    except (OSError,ValueError,TypeError):
        return {}


def observed_status(path, addr):
    # Actual saved BTC rows are positive evidence, including legacy snapshots.
    if addr in load_full(path):
        return 'ok_btc'
    return status_info(path).get('accounts',{}).get(addr,{}).get('status','unknown')


def origin(path_then,path_now,lo,hi):
    A,B=load_full(path_then),load_full(path_now)
    out={k:[0,0.] for k in ('held','moved_in','added','flipped','new','unknown','became_available')}
    def add(k,q):
        if q>0:
            out[k][0]+=1
            out[k][1]+=q
    for addr,x in B.items():
        if x['liq'] is None or not lo<=x['liq']<hi:
            continue
        p=A.get(addr)
        q=abs(x['sz'])
        if p is None:
            add('new' if observed_status(path_then,addr)=='ok_no_btc' else 'unknown',q)
        elif p['sz']*x['sz']<0:
            add('flipped',q)
        else:
            retained=min(abs(p['sz']),q)
            k='became_available' if p['liq'] is None else ('held' if lo<=p['liq']<hi else 'moved_in')
            add(k,retained)
            add('added',max(q-abs(p['sz']),0))
    return out


def fate(path_then,path_now,lo,hi):
    A,B=load_full(path_then),load_full(path_now)
    cohort={a:x for a,x in A.items() if x['liq'] is not None and lo<=x['liq']<hi}
    if not cohort:
        return None
    out={k:[0,0.] for k in ('closed','unknown','flipped','reduced','moved','unavailable','stay')}
    out.update(n=len(cohort),btc=sum(abs(x['sz']) for x in cohort.values()))
    def add(k,q):
        if q>0:
            out[k][0]+=1
            out[k][1]+=q
    for addr,p in cohort.items():
        x=B.get(addr)
        q=abs(p['sz'])
        if x is None:
            add('closed' if observed_status(path_now,addr)=='ok_no_btc' else 'unknown',q)
        elif p['sz']*x['sz']<0:
            add('flipped',q)
        else:
            retained=min(q,abs(x['sz']))
            add('reduced',max(q-abs(x['sz']),0))
            k='unavailable' if x['liq'] is None else ('stay' if lo<=x['liq']<hi else 'moved')
            add(k,retained)
    return out


def position_changes(path_then,path_now):
    A,B=load_full(path_then),load_full(path_now)
    out={'expanded':[],'reduced':[],'flipped':[]}
    for a in sorted(A.keys()&B.keys()):
        p,x=A[a],B[a]
        if p['sz']*x['sz']<0:
            kind,delta='flipped',abs(p['sz'])+abs(x['sz'])
        else:
            delta=abs(x['sz'])-abs(p['sz'])
            if abs(delta)<1e-6:
                continue
            kind='expanded' if delta>0 else 'reduced'
        out[kind].append({'addr':a,'before':p,'after':x,'change':abs(delta)})
    for rows in out.values():
        rows.sort(key=lambda r:(-r['change'],r['addr']))
    return out
