import datetime as dt, os, tempfile, unittest, csv
from pathlib import Path
from unittest.mock import Mock, patch
import entsoe_actuals as a
from entsoe_adapter import AREAS, day_bounds
from pipeline_status import DATASETS

def pts(n, v=100.0, skip=()):
    return ''.join(f'<Point><position>{i}</position><quantity>{v + i}</quantity></Point>' for i in range(1, n + 1) if i not in skip)

def doc(kind, start, end, n, skip=(), series=None):
    s, e = start.strftime('%Y-%m-%dT%H:%MZ'), end.strftime('%Y-%m-%dT%H:%MZ')
    per = f'<Period><timeInterval><start>{s}</start><end>{e}</end></timeInterval><resolution>PT60M</resolution>'
    if kind == 'load':
        body = f'<TimeSeries><curveType>A01</curveType><outBiddingZone_Domain.mRID>{AREAS["BA"]}</outBiddingZone_Domain.mRID><quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>{per}{pts(n, skip=skip)}</Period></TimeSeries>'
        typ = 'A65'
    else:
        body = ''
        for code, flow in series:
            dom = ('inBiddingZone' if flow == 'g' else 'outBiddingZone')
            body += f'<TimeSeries><MktPSRType><psrType>{code}</psrType></MktPSRType><curveType>A01</curveType><{dom}_Domain.mRID>{AREAS["BA"]}</{dom}_Domain.mRID><quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>{per}{pts(n, skip=skip)}</Period></TimeSeries>'
        typ = 'A75'
    return f'<M><mRID>d1</mRID><revisionNumber>1</revisionNumber><type>{typ}</type><process.processType>A16</process.processType>{body}</M>'.encode()

DAY = dt.date(2026, 11, 23)

class T(unittest.TestCase):
    def setUp(self): DATASETS.clear()
    def test_load_complete_and_dst(self):
        for day, n in [(dt.date(2026, 11, 23), 24), (dt.date(2026, 3, 29), 23), (dt.date(2026, 10, 25), 25)]:
            s, e = day_bounds(day)
            self.assertEqual(len(a.parse_load(doc('load', s, e, n), s, e, 'x')), n)
    def test_load_gap_rejected_not_zero(self):
        s, e = day_bounds(DAY)
        with self.assertRaises(a.DataError): a.parse_load(doc('load', s, e, 24, skip=(5,)), s, e, 'x')
    def test_generation_keeps_published_points_only(self):
        s, e = day_bounds(DAY)
        rows = a.parse_generation(doc('gen', s, e, 24, skip=(3, 4), series=[('B02', 'g'), ('B10', 'c')]), s, e, 'x')
        self.assertEqual(len(rows), 44)
        self.assertEqual({(r['PSR_Type'], r['Flow']) for r in rows}, {('Fossil Brown coal/Lignite', 'generation'), ('Hydro Pumped Storage', 'consumption')})
        self.assertTrue(all(float(r['Value_MW']) > 0 for r in rows))
    def test_wrong_area_and_unknown_type_rejected(self):
        s, e = day_bounds(DAY)
        bad = doc('gen', s, e, 24, series=[('B02', 'g')]).replace(AREAS['BA'].encode(), b'10YHR-HEP------M')
        with self.assertRaises(a.DataError): a.parse_generation(bad, s, e, 'x')
        with self.assertRaises(a.DataError): a.parse_generation(doc('gen', s, e, 24, series=[('B99', 'g')]), s, e, 'x')
    def test_update_window_recovers_and_token_not_saved(self):
        now = dt.datetime(2026, 11, 24, 12, tzinfo=dt.timezone.utc)
        def get(url, params=None, **kw):
            import datetime as d
            s = d.datetime.strptime(params['periodStart'], '%Y%m%d%H%M').replace(tzinfo=d.timezone.utc)
            e = d.datetime.strptime(params['periodEnd'], '%Y%m%d%H%M').replace(tzinfo=d.timezone.utc)
            n = int((e - s).total_seconds() // 3600)
            body = doc('load', s, e, n) if params['documentType'] == 'A65' else doc('gen', s, e, n, series=[('B16', 'g'), ('B12', 'g')])
            return Mock(status_code=200, content=body)
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(4):
                sess = Mock(); sess.get.side_effect = get
                a.update_actuals(tmp, now, sess, 'secret-test-only', pause=0)
            self.assertEqual(DATASETS['entsoe_actual_load']['state'], 'OK')
            self.assertEqual(DATASETS['entsoe_actual_generation']['state'], 'OK')
            txt = (Path(tmp) / 'entsoe_actual_load.csv').read_text() + (Path(tmp) / 'entsoe_actual_generation.csv').read_text() + str(DATASETS)
            self.assertNotIn('secret-test-only', txt)
            rows = list(csv.DictReader(open(Path(tmp) / 'entsoe_actual_load.csv')))
            keys = [r['Interval_Start_UTC'] for r in rows]; self.assertEqual(len(keys), len(set(keys)))
            self.assertEqual(len({r['Interval_Start_Local'][:10] for r in rows}), 30)
    def test_missing_token_and_failure_keep_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            a.update_actuals(tmp, dt.datetime(2026, 11, 24, tzinfo=dt.timezone.utc), Mock(), '', pause=0)
            self.assertEqual(DATASETS['entsoe_actual_load']['state'], 'FAILED')
            self.assertFalse((Path(tmp) / 'entsoe_actual_load.csv').exists())
    def test_ack_document_is_failure(self):
        s, e = day_bounds(DAY)
        ack = b'<Acknowledgement_MarketDocument><Reason><code>999</code><text>No matching data found</text></Reason></Acknowledgement_MarketDocument>'
        with self.assertRaises(a.DataError): a.parse_load(ack, s, e, 'x')
if __name__ == '__main__': unittest.main()
