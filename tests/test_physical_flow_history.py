import datetime as dt
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import entsoe_adapter as e
import physical_flow_history as h
from test_entsoe import document

class HistoryTests(unittest.TestCase):
    def test_partial_today(self):
        day=dt.date(2026,11,24);start,end=e.day_bounds(day);right=start+dt.timedelta(hours=5)
        rows=h.parse_available(document(start=start,end=right,positions=[(1,0),(5,1)]),'BA','HR',day,right)
        self.assertEqual(len(rows),5);self.assertFalse(h.complete(rows,day))
    def test_full_day(self):
        day=dt.date(2026,11,23);start,end=e.day_bounds(day)
        rows=h.parse_available(document(start=start,end=end),'BA','HR',day,end)
        self.assertEqual(len(rows),24);self.assertTrue(h.complete(rows,day))
    def test_future_rejected(self):
        day=dt.date(2026,11,24);start,end=e.day_bounds(day)
        self.assertEqual(len(h.parse_available(document(start=start,end=end),'BA','HR',day,start+dt.timedelta(hours=5))),1)
    def test_dst_25hours(self):
        day=dt.date(2026,10,25);start,end=e.day_bounds(day)
        rows=h.parse_available(document(start=start,end=end),'BA','HR',day,end)
        self.assertEqual(len(rows),25);self.assertTrue(h.complete(rows,day))
    def test_wrong_direction(self):
        day=dt.date(2026,11,23);start,end=e.day_bounds(day)
        with self.assertRaises(e.DataError):h.parse_available(document(start=start,end=end),'HR','BA',day,end)
    def test_dynamic_window_and_missing_visible(self):
        with tempfile.TemporaryDirectory() as d,patch.object(h.time,'sleep'):
            out=h.update(d,now=dt.datetime(2026,12,25,12,tzinfo=dt.timezone.utc),token='',session=object())
            self.assertEqual(out['requested_first_day'],'2026-11-25');self.assertEqual(out['requested_last_day'],'2026-12-25')
            self.assertEqual(len(out['coverage']),186);self.assertEqual(out['state'],'PARTIAL');self.assertEqual(len(out['historical_incomplete_pairs']),180)

class WriterIsolationTests(unittest.TestCase):
    def test_legacy_can_skip_physical_without_writing_it(self):
        from unittest.mock import patch
        import pipeline_status as ps
        ps.DATASETS.clear()
        with tempfile.TemporaryDirectory() as tmp, patch.object(e,'fetch',side_effect=e.APIError('unavailable')):
            path=Path(tmp)/'entsoe_physical_flows.csv'
            path.write_text('sentinel')
            e.update_entsoe(tmp,session=object(),token='dummy',include_physical=False)
            self.assertEqual(path.read_text(),'sentinel')
            self.assertNotIn('entsoe_physical_flows',ps.DATASETS)
            self.assertEqual(set(ps.DATASETS),{'entsoe_load_forecast','entsoe_hr_schedules'})

class RangeFetchTests(unittest.TestCase):
    @patch.object(h.time,'sleep')
    def test_six_ranges_every_run_refetches_full_window(self,_):
        now=dt.datetime(2026,11,24,12,tzinfo=dt.timezone.utc)
        requests=[]
        def fetch(session,token,p):
            requests.append(p)
            a=next(k for k,v in e.AREAS.items() if v==p['out_Domain'])
            b=next(k for k,v in e.AREAS.items() if v==p['in_Domain'])
            start=dt.datetime.strptime(p['periodStart'],'%Y%m%d%H%M').replace(tzinfo=dt.timezone.utc)
            end=now
            return document('physical_flows',a,b,start=start,end=end,positions=[(1,10),(int((end-start).total_seconds()/3600),20)])
        with tempfile.TemporaryDirectory() as tmp,patch.object(e,'fetch',side_effect=fetch):
            result=h.update(tmp,now=now,token='dummy',session=object())
            self.assertEqual(len(requests),6)
            self.assertEqual(result['state'],'OK')
            self.assertEqual(len(result['coverage']),186)
            self.assertEqual(len({x['day'] for x in result['coverage'] if x['rows']}),31)
            self.assertEqual(requests[0]['periodStart'],'202610242200')
            requests.clear()
            result=h.update(tmp,now=now,token='dummy',session=object())
            self.assertEqual(len(requests),6)
            self.assertEqual(requests[0]['periodStart'],'202610242200')  # full window again, no reuse
