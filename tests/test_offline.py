"""No exchange requests, Discord messages, or production files touched."""
import csv
import gzip
import json
import math
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
import numpy as np

import collect
import market
from vps import report, tracking


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        assert self.root.resolve().parent == Path(tempfile.gettempdir()).resolve()
        self.addCleanup(self.tmp.cleanup)
        p=patch.object(socket.socket,'connect',side_effect=AssertionError('NETWORK FORBIDDEN'))
        p.start()
        self.addCleanup(p.stop)
        tracking.load_full.cache_clear()
        tracking.status_info.cache_clear()

    def snap(self,name,rows,statuses=None):
        path=self.root/(name+'.csv.gz')
        with gzip.open(path,'wt',encoding='utf-8',newline='') as f:
            w=csv.writer(f)
            w.writerow(['#ts','2026-09-05T08:00:00+00:00','px',1000,'mode','full','scanned',5,'btc_positions',len(rows),'elapsed_s',10])
            w.writerow(['addr','size','entry','liq','lev','margin','pnl','acct_value'])
            for a,q,l in rows:
                w.writerow([a,q,1000,'' if l is None else l,5,'cross',0,1000])
        if statuses is not None:
            market.write_json_gz(str(path).replace('.csv.gz','.status.json.gz'),
                                 {'accounts':{a:{'status':v} for a,v in statuses.items()}})
        return str(path)

    def test_fetch_failure_not_no_btc(self):
        with patch.object(collect,'post',side_effect=collect.RequestFailure([{'kind':'timeout'}])):
            self.assertEqual(collect.fetch('a')['status'],'transport_error')
        with patch.object(collect,'post',return_value=({'assetPositions':[],'marginSummary':{}},1,[])):
            self.assertEqual(collect.fetch('a')['status'],'ok_no_btc')
        for bad in ({}, {'assetPositions':None,'marginSummary':{}}, {'assetPositions':[{}],'marginSummary':{}}):
            with patch.object(collect,'post',return_value=(bad,1,[])):
                self.assertEqual(collect.fetch('a')['status'],'invalid_response')

    def test_invalid_position_not_no_btc(self):
        data={'marginSummary':{'accountValue':'10'},'assetPositions':[{'position':{
            'coin':'BTC','szi':'1','entryPx':None,'liquidationPx':None,
            'leverage':{'type':'cross','value':5},'unrealizedPnl':'0'}}]}
        with patch.object(collect,'post',return_value=(data,1,[])):
            self.assertEqual(collect.fetch('a')['status'],'invalid_response')

    def test_failed_only_second_pass(self):
        calls=[]
        def fetch(addr):
            calls.append(addr)
            first=addr=='b' and calls.count(addr)==1
            return dict(addr=addr,status='transport_error' if first else 'ok_no_btc',
                        errors=[{'kind':'timeout'}] if first else [],attempts=3 if first else 1,
                        observed_at='now',row=None)
        with patch.object(collect,'fetch',side_effect=fetch),patch.object(collect.time,'sleep'):
            rows,states=collect.scan(['a','b'])
        self.assertEqual(calls.count('a'),1)
        self.assertEqual(calls.count('b'),2)
        self.assertTrue(states['b']['recovered'])
        self.assertEqual(states['b']['attempts'],4)

    def test_post_retries_record_reason(self):
        with patch.object(collect.urllib.request,'urlopen',side_effect=TimeoutError('slow')),patch.object(collect.time,'sleep'):
            with self.assertRaises(collect.RequestFailure) as ctx:
                collect.post({})
        self.assertEqual(len(ctx.exception.errors),3)
        self.assertEqual(ctx.exception.errors[0]['kind'],'TimeoutError')

    def test_origin_partition_and_legacy_unknown(self):
        a=self.snap('a',[('move',10,500),('flip',-8,1500),('held',20,800),('null',4,None)],
                    {'new':'ok_no_btc','fail':'transport_error'})
        b=self.snap('b',[('move',100,850),('flip',5,850),('held',12,850),('null',6,850),('new',3,850),('fail',7,850),('unlisted',9,850)])
        o=tracking.origin(a,b,750,1000)
        self.assertEqual(o['moved_in'][1],10)
        self.assertEqual(o['added'][1],92)
        self.assertEqual(o['flipped'][1],5)
        self.assertEqual(o['new'][1],3)
        self.assertEqual(o['unknown'][1],16)
        self.assertAlmostEqual(sum(v[1] for v in o.values()),142)
        old=self.snap('legacy',[])
        self.assertEqual(tracking.origin(old,b,750,1000)['new'][1],0)

    def test_fate_conserves_previous_quantity(self):
        a=self.snap('a',[('cut',10,800),('flip',10,800),('gone',10,800),('failed',10,800),('null',10,800)])
        b=self.snap('b',[('cut',4,600),('flip',-6,1500),('null',10,None)],{'gone':'ok_no_btc','failed':'transport_error'})
        f=tracking.fate(a,b,750,1000)
        self.assertEqual(f['reduced'][1],6)
        self.assertEqual(f['moved'][1],4)
        self.assertEqual(f['closed'][1],10)
        self.assertEqual(f['unknown'][1],10)
        self.assertEqual(f['flipped'][1],10)
        self.assertEqual(sum(v[1] for v in f.values() if isinstance(v,list)),50)

    def test_changes_separate_flip_expansion_reduction(self):
        a=self.snap('a',[('flip',-10,1200),('up',2,800),('down',10,800)])
        b=self.snap('b',[('flip',5,700),('up',12,800),('down',4,600)])
        c=tracking.position_changes(a,b)
        self.assertEqual([r['addr'] for r in c['flipped']],['flip'])
        self.assertEqual(c['expanded'][0]['change'],10)
        self.assertEqual(c['reduced'][0]['change'],6)

    def test_contact_cross_and_gap(self):
        self.assertEqual(report.approach({'t':[0],'h':[120],'l':[90]},100,110,0,True),(120,True))
        self.assertIn('관통',report.contact({'t':[0,5000],'p':[90,120]},100,110,0,5))
        self.assertIn('미확인',report.contact({'t':[0,3600000],'p':[90,120]},100,110,0,3600))
        self.assertIn('미확인',report.contact({'t':[],'p':[]},100,110,0,5))
        self.assertIn('내부',report.contact({'t':[0],'p':[105]},100,110,0,0))

    def test_forward_grid_never_uses_future_column(self):
        t=datetime(2026,9,5,tzinfo=timezone.utc)
        snaps=[{'ts':t},{'ts':t+timedelta(hours=4)}]
        edges,grid=report.forward_grid(snaps,np.array([[10.,20.]]))
        self.assertEqual(grid.shape,(1,3))
        self.assertTrue(np.isnan(grid[0,1]))
        self.assertEqual(grid[0,2],20)
        self.assertAlmostEqual((edges[2]-edges[0])*24,4)

    def test_candles_source_and_closed_only(self):
        bars=[dict(t=0,T=299999,o='10',h='12',l='9',c='11',v='8'),
              dict(t=300000,T=599999,o='11',h='12',l='10',c='11',v='3')]
        with patch.object(market,'request',return_value=bars) as req:
            rows=market.candle_history(0,400)
        self.assertEqual(req.call_args.args[0]['type'],'candleSnapshot')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['v'],8)

    def test_draw_bin_bounds_and_no_false_price_range(self):
        t=datetime(2026,9,5,tzinfo=timezone.utc)
        snap=dict(ts=t,px=79000,pos=[(10,72100)],scanned=1,complete=True)
        original_close=report.plt.close
        try:
            with patch.object(report.plt,'close'):
                report.draw([snap],{'t':[],'h':[],'l':[],'c':[]},str(self.root/'map.png'),7)
            fig=report.plt.gcf()
            ax,bx=fig.axes[:2]
            coords=ax.collections[0].get_coordinates()
            self.assertTrue(np.any(coords[:,:,1]==72000))
            bars=[b for b in bx.patches if hasattr(b,'get_width') and b.get_width()==10]
            self.assertEqual(len(bars),1)
            self.assertAlmostEqual(bars[0].get_y()+bars[0].get_height()/2,72125)
            self.assertTrue(any('가격 캔들 미확보' in text.get_text() for text in bx.texts))
        finally:
            original_close('all')

    def test_recorder_offline_archive(self):
        point={'t':1788595200000,'px':79000,'source':'hyperliquid_mark'}
        with patch.object(market,'mark_price',return_value=point):
            market.record_once(self.root)
        self.assertEqual(market.read_marks(point['t']/1000-1,point['t']/1000+1,self.root),[point])

    def test_completed_collector_with_failed_holder(self):
        addr='0x'+'1'*40
        old='0x'+'2'*40
        holder=self.root/'holders.json'
        holder.write_text(json.dumps({'addresses':[old]}))
        row={'a':addr,'sz':10.,'ep':79000.,'liq':70000.,'lev':5,'mt':'cross','pnl':0.,'av':20000.}
        state={addr:dict(status='ok_btc',row=row,attempts=1,errors=[],observed_at='2026-09-07T00:00:00+00:00'),
               old:dict(status='transport_error',row=None,attempts=6,errors=[{'kind':'timeout'}],observed_at='2026-09-07T00:00:00+00:00')}
        point={'t':int(datetime.now(timezone.utc).timestamp()*1000),'px':79000,'source':'hyperliquid_mark'}
        with patch.object(collect,'ROOT',str(self.root)),patch.object(collect,'HOLDERS',str(holder)),\
             patch.object(collect,'leaderboard',return_value=[addr]),patch.object(collect,'scan',return_value=([row],state)) as scan,\
             patch.object(collect,'btc_price',return_value=point),patch.object(market,'read_marks',return_value=[]),\
             patch.object(market,'candle_history',return_value=[]),patch('sys.argv',['collect.py','--full','--force']):
            collect.main()
            self.assertEqual(set(scan.call_args.args[0]),{addr,old})
            self.assertIsNotNone(collect.last_snapshot_age_min())
        saved=list((self.root/'data').glob('*/*.csv.gz'))
        self.assertEqual(len(saved),1)
        status=tracking.status_info(str(saved[0]))
        self.assertFalse(status['complete'])
        self.assertFalse(status['accounts'][old]['in_leaderboard'])
        self.assertIn(old,json.loads(holder.read_text())['addresses'])

    def test_discord_failure_warning_and_attachment(self):
        now=datetime.now(timezone.utc)
        snap={'ts':now,'px':1000,'path':self.snap('legacy',[])}
        text=report.summary([snap],1000,['x'*2200],github_failed=True)
        self.assertTrue(text.startswith('⚠'))
        png=self.root/'x.png'
        png.write_bytes(b'png-fixture')
        class Response:
            status=204
            def __enter__(self): return self
            def __exit__(self,*args): pass
        with patch.object(report.urllib.request,'urlopen',return_value=Response()) as send:
            self.assertEqual(report.post('https://example.invalid',text,str(png)),204)
        body=send.call_args.args[0].data.decode('utf-8')
        self.assertIn('briefing.txt',body)
        payload=json.loads(body.split('application/json\r\n\r\n',1)[1].split('\r\n',1)[0])
        self.assertLessEqual(len(payload['content'].encode('utf-16-le'))//2,2000)
        self.assertIn('GitHub',payload['content'])


if __name__=='__main__':
    unittest.main()
