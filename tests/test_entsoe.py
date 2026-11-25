import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import entsoe_adapter as e
import pipeline_status as ps

DAY = dt.date(2026, 11, 23)
START, END = e.day_bounds(DAY)


def document(dataset='physical_flows', source='BA', target='HR', curve='A03',
             positions=None, start=START, end=END, resolution='PT60M', unit='MAW'):
    root = ET.Element('Document')
    def add(parent, tag, value):
        ET.SubElement(parent, tag).text = str(value)
    add(root, 'type', {'physical_flows':'A11', 'load_forecast':'A65', 'hr_schedules':'A09', 'ntc_day_ahead':'A61', 'nominated_daily':'A26'}[dataset])
    add(root, 'process.processType', 'A01')
    add(root, 'mRID', 'synthetic-test')
    add(root, 'revisionNumber', 2)
    ts = ET.SubElement(root, 'TimeSeries')
    add(ts, 'out_Domain.mRID', e.AREAS[source])
    add(ts, 'in_Domain.mRID', e.AREAS[target])
    add(ts, 'outBiddingZone_Domain.mRID', e.AREAS['BA'])
    add(ts, 'contract_MarketAgreement.type', 'A01')
    add(ts, 'businessType', 'B08')
    add(ts, 'quantity_Measure_Unit.name', unit)
    add(ts, 'curveType', curve)
    p = ET.SubElement(ts, 'Period')
    ti = ET.SubElement(p, 'timeInterval')
    add(ti, 'start', e.iso(start)); add(ti, 'end', e.iso(end))
    add(p, 'resolution', resolution)
    for position, value in (positions if positions is not None else [(1, 0)]):
        point = ET.SubElement(p, 'Point')
        add(point, 'position', position); add(point, 'quantity', value)
    return ET.tostring(root)


def parse(body, dataset='physical_flows', source='BA', target='HR', start=START, end=END):
    return e.parse_document(body, dataset, source, target, start, end, 'test-time')


class CapacityParserTests(unittest.TestCase):
    def test_ntc_and_nominated_parse_directional_hourly(self):
        for dataset in ('ntc_day_ahead', 'nominated_daily'):
            rows = parse(document(dataset, 'BA', 'HR', positions=[(1, 500)]), dataset)
            self.assertEqual(len(rows), 24)
            self.assertEqual({(r['From_Area'], r['To_Area'], r['Value_MW']) for r in rows}, {('BA', 'HR', 500)})
    def test_wrong_direction_rejected(self):
        with self.assertRaises(e.DataError):
            parse(document('ntc_day_ahead', 'HR', 'BA'), 'ntc_day_ahead', 'BA', 'HR')
    def test_nominated_requires_total_nominated_business_type(self):
        body = document('nominated_daily', 'BA', 'HR').replace(b'B08', b'A25')
        with self.assertRaises(e.DataError):
            parse(body, 'nominated_daily')
    def test_scheduled_total_requires_total_contract(self):
        body = document('hr_schedules', 'BA', 'ME')
        with self.assertRaises(e.DataError):
            parse(body, 'scheduled_total', 'BA', 'ME')
        body = body.replace(b'contract_MarketAgreement.type>A01<',
                            b'contract_MarketAgreement.type>A05<')
        rows = parse(body, 'scheduled_total', 'BA', 'ME')
        self.assertEqual(len(rows), 24)
    def test_capacity_groups_run_and_skip(self):
        import inspect
        self.assertFalse(inspect.signature(e.update_entsoe).parameters['include_capacity'].default)


class ParserTests(unittest.TestCase):
    def test_a03_single_zero(self):
        rows = parse(document())
        self.assertEqual(len(rows), 24)
        self.assertEqual({r['Value_MW'] for r in rows}, {0})

    def test_a03_blocks(self):
        rows = parse(document(positions=[(1, 10), (4, 20), (24, 30)]))
        self.assertEqual([r['Value_MW'] for r in rows], [10]*3+[20]*20+[30])

    def test_a01(self):
        rows = parse(document(curve='A01', positions=[(i, i) for i in range(1,25)]))
        self.assertEqual(len(rows), 24)

    def test_missing_a01_rejected(self):
        with self.assertRaises(e.DataError): parse(document(curve='A01'))

    def test_a03_first_point_required(self):
        with self.assertRaises(e.DataError): parse(document(positions=[(2, 1)]))

    def test_bad_values(self):
        for quantity in ('nan', 'inf', -1):
            with self.subTest(quantity=quantity), self.assertRaises(e.DataError):
                parse(document(positions=[(1, quantity)]))

    def test_bad_positions(self):
        for points in ([(0,1)], [(25,1)], [(1,1),(1,2)]):
            with self.subTest(points=points), self.assertRaises(e.DataError):
                parse(document(positions=points))

    def test_wrong_unit_curve_resolution(self):
        for kwargs in ({'unit':'MWH'}, {'curve':'A02'}, {'resolution':'P1D'}, {'resolution':'PT0M'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(e.DataError): parse(document(**kwargs))

    def test_wrong_direction(self):
        with self.assertRaises(e.DataError): parse(document(source='HR', target='BA'))

    def test_load_and_schedule(self):
        for name in ('load_forecast', 'hr_schedules'):
            self.assertEqual(len(parse(document(dataset=name), dataset=name)), 24)

    def test_acknowledgement(self):
        with self.assertRaises(e.DataError):
            parse(b'<Acknowledgement_MarketDocument><Reason><code>999</code><text>Do not log me</text></Reason></Acknowledgement_MarketDocument>')

    def test_multiple_series(self):
        root = ET.fromstring(document()); root.append(ET.fromstring(document()).find('TimeSeries'))
        with self.assertRaises(e.DataError): parse(ET.tostring(root))

    def test_overlapping_period(self):
        root = ET.fromstring(document()); ts = root.find('TimeSeries')
        ts.append(ET.fromstring(document()).find('TimeSeries/Period'))
        with self.assertRaises(e.DataError): parse(ET.tostring(root))

    def test_period_gap(self):
        middle = START + dt.timedelta(hours=10)
        root = ET.fromstring(document(end=middle)); ts=root.find('TimeSeries')
        ts.append(ET.fromstring(document(start=middle+dt.timedelta(hours=1))).find('TimeSeries/Period'))
        with self.assertRaises(e.DataError): parse(ET.tostring(root))

    def test_adjacent_periods(self):
        middle = START + dt.timedelta(hours=10)
        root = ET.fromstring(document(end=middle)); ts=root.find('TimeSeries')
        ts.append(ET.fromstring(document(start=middle)).find('TimeSeries/Period'))
        self.assertEqual(len(parse(ET.tostring(root))),24)

    def test_partial_window(self):
        with self.assertRaises(e.DataError): parse(document(end=END-dt.timedelta(hours=1)))

    def test_dst_day_lengths(self):
        # Computed timezone transitions, 2026-03-29 and 2026-10-25.
        for day, count in [(dt.date(2026,3,29),23), (dt.date(2026,10,25),25)]:
            start,end=e.day_bounds(day)
            rows=parse(document(start=start,end=end), start=start,end=end)
            self.assertEqual(len(rows),count)
            self.assertEqual(len({r['Interval_Start_UTC'] for r in rows}),count)
            self.assertEqual(len({r['Interval_Start_Local'] for r in rows}),count)

    def test_subhourly(self):
        self.assertEqual(len(parse(document(resolution='PT15M'))),96)

    def test_xml_declarations(self):
        with self.assertRaises(e.DataError): parse(b'<!DOCTYPE a><a/>')


class RunTests(unittest.TestCase):
    def setUp(self): ps.DATASETS.clear()

    def test_token_only_in_query_param(self):
        session=Mock(); session.get.return_value=Mock(status_code=200,content=b'xml')
        self.assertEqual(e.fetch(session,'dummy-test-token',{'documentType':'A11'}),b'xml')
        args,kwargs=session.get.call_args
        self.assertEqual(args[0],e.ENDPOINT)
        self.assertEqual(kwargs['params']['securityToken'],'dummy-test-token')
        self.assertFalse(kwargs['allow_redirects'])

    def test_network_error_hides_url_and_token(self):
        session=Mock(); session.get.side_effect=e.requests.ConnectionError('https://x?securityToken=dummy-test-token')
        with self.assertRaises(e.APIError) as ctx:
            e.fetch(session,'dummy-test-token',{'documentType':'A11'})
        self.assertNotIn('dummy-test-token',str(ctx.exception))

    def test_auth_and_rate_limit_stop(self):
        for code in (401,403,429):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                ps.DATASETS.clear(); session=Mock()
                session.get.return_value=Mock(status_code=code)
                e.update_entsoe(tmp,DAY,session=session,token='dummy')
                self.assertEqual(session.get.call_count,1)
                self.assertEqual(len(ps.DATASETS),3)
                self.assertFalse(list(Path(tmp).glob('*.csv')))

    def test_missing_token(self):
        session=Mock()
        e.update_entsoe(day=DAY,session=session,token='')
        session.get.assert_not_called()
        self.assertEqual(len(ps.DATASETS),3)

    @patch('entsoe_adapter.time.sleep')
    def test_nine_requests_and_files(self,_):
        bodies=[document('load_forecast','BA','BA')]
        for a,b in [('BA','HR'),('HR','BA'),('BA','RS'),('RS','BA'),('BA','ME'),('ME','BA')]:
            bodies.append(document('physical_flows',a,b))
        bodies.extend([document('hr_schedules','BA','HR'),document('hr_schedules','HR','BA')])
        session=Mock(); session.get.side_effect=[Mock(status_code=200,content=b) for b in bodies]
        with tempfile.TemporaryDirectory() as tmp:
            e.update_entsoe(tmp,DAY,session=session,token='dummy')
            self.assertEqual(session.get.call_count,9)
            self.assertEqual([ps.DATASETS['entsoe_'+k]['row_count'] for k in ('load_forecast','physical_flows','hr_schedules')],[24,144,48])
            self.assertEqual(len(list(Path(tmp).glob('*.csv'))),3)

    @patch('entsoe_adapter.time.sleep')
    def test_failed_direction_keeps_entire_dataset(self,_):
        session=Mock()
        session.get.side_effect=[Mock(status_code=200,content=document('load_forecast','BA','BA')),
                                  Mock(status_code=200,content=document()),
                                  Mock(status_code=200,content=b'<bad>'),
                                  Mock(status_code=200,content=b'<bad>')]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'entsoe_physical_flows.csv'; path.write_text('unchanged')
            e.update_entsoe(tmp,DAY,session=session,token='dummy')
            self.assertEqual(path.read_text(),'unchanged')
            self.assertEqual(ps.DATASETS['entsoe_physical_flows']['state'],'FAILED')

    def test_no_data_no_secret_in_status(self):
        session=Mock(); session.get.side_effect=e.requests.ConnectionError('dummy-secret')
        with tempfile.TemporaryDirectory() as tmp:
            e.update_entsoe(tmp,DAY,session=session,token='dummy-secret')
            self.assertNotIn('dummy-secret',json.dumps(ps.DATASETS))

    def test_idempotent_replace(self):
        rows=parse(document())
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'data.csv'
            e.persist(path,rows,START); e.persist(path,rows,START)
            self.assertEqual(len(path.read_text().splitlines()),25)
            rows[0]['Value_MW']=12
            e.persist(path,rows,START)
            self.assertIn(',12,',path.read_text())

    def test_status_preserves_legacy_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'status.json'
            path.write_text(json.dumps({'last_verified_date':'historic','dataset_details':{'a':{'last_success_at':'old'}}}))
            ps.record('a','FAILED',detail='fetch failed')
            result=ps.write_status(path,rows=487)
            self.assertEqual(result['last_verified_date'],'historic')
            self.assertEqual(result['dataset_details']['a']['last_success_at'],'old')
            self.assertTrue(ps.problems(result))

    def test_static_reference_warning_not_success(self):
        ps.record('balancing','STATIC_REFERENCE',detail='not fresh')
        with tempfile.TemporaryDirectory() as tmp:
            result=ps.write_status(Path(tmp)/'status.json')
            self.assertNotEqual(result['datasets']['balancing'],'OK')
            self.assertEqual(ps.problems(result),[])

if __name__ == '__main__': unittest.main()

class RevisionTests(unittest.TestCase):
    def test_older_same_document_revision_preserves_csv(self):
        rows=parse(document())
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'data.csv'; e.persist(path,rows,START)
            prior=path.read_bytes()
            rows[0]['Revision']=1
            with self.assertRaises(e.DataError): e.persist(path,rows,START)
            self.assertEqual(path.read_bytes(),prior)


class AreaCodeTests(unittest.TestCase):
    def test_bosnia_eic_literal(self):
        # Literal check: the mocks read AREAS, so a wrong constant would otherwise pass.
        import entsoe_adapter as e
        self.assertEqual(e.AREAS['BA'], '10YBA-JPCC-----D')


class BackfillTests(unittest.TestCase):
    @patch('entsoe_adapter.time.sleep')
    def test_auto_mode_recovers_incomplete_days_and_reports_unavailable(self,_):
        today=dt.datetime.now(e.TZ).date(); yesterday=today-dt.timedelta(days=1)
        bad={yesterday-dt.timedelta(days=3)}
        def get(url,params=None,**kw):
            start=dt.datetime.strptime(params['periodStart'],'%Y%m%d%H%M').replace(tzinfo=dt.timezone.utc)
            day=start.astimezone(e.TZ).date(); s,en=e.day_bounds(day)
            if day in bad:return Mock(status_code=200,content=b'<bad>')
            if params['documentType']=='A65':return Mock(status_code=200,content=document('load_forecast','BA','BA',start=s,end=en))
            a=next(k for k,v in e.AREAS.items() if v==params['out_Domain']);b=next(k for k,v in e.AREAS.items() if v==params['in_Domain'])
            return Mock(status_code=200,content=document('hr_schedules',a,b,start=s,end=en))
        session=Mock();session.get.side_effect=get
        with tempfile.TemporaryDirectory() as tmp:
            ps.DATASETS.clear()
            e.update_entsoe(tmp,session=session,token='dummy',include_physical=False)
            st=ps.DATASETS['entsoe_load_forecast']
            self.assertEqual(st['state'],'PARTIAL');self.assertIn((yesterday-dt.timedelta(days=3)).isoformat(),st['unavailable_days'])
            self.assertEqual(len(st['recovered_days']),9)   # newest 10 attempted, one of them unavailable
            self.assertEqual(len(st['unavailable_days']),1+19)  # 1 failed + 19 not yet attempted, all reported
            rows=list(__import__('csv').DictReader(open(Path(tmp)/'entsoe_load_forecast.csv')))
            self.assertEqual(len({r['Interval_Start_Local'][:10] for r in rows}),10)  # yesterday + 9 recovered
            self.assertTrue(all(float(r['Value_MW'])>=0 for r in rows))
            bad.clear()
            for _ in range(3):
                e.update_entsoe(tmp,session=session,token='dummy',include_physical=False)
            self.assertEqual(ps.DATASETS['entsoe_load_forecast']['state'],'OK')
            rows=list(__import__('csv').DictReader(open(Path(tmp)/'entsoe_load_forecast.csv')))
            self.assertEqual(len({r['Interval_Start_Local'][:10] for r in rows}),30)  # yesterday + 29 earlier days
            session.get.reset_mock()
            e.update_entsoe(tmp,session=session,token='dummy',include_physical=False)
            self.assertEqual(session.get.call_count,1+2)  # complete window: only yesterday is refreshed (load 1, HR 2)

    @patch('entsoe_adapter.time.sleep')
    def test_failed_yesterday_keeps_saved_and_failed(self,_):
        session=Mock();session.get.return_value=Mock(status_code=200,content=b'<bad>')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'entsoe_load_forecast.csv';ps.DATASETS.clear()
            e.persist(path,parse(document('load_forecast','BA','BA'),'load_forecast','BA','BA'),START-dt.timedelta(days=40));before=path.read_text()
            e.update_entsoe(tmp,session=session,token='dummy',include_physical=False)
            self.assertEqual(ps.DATASETS['entsoe_load_forecast']['state'],'FAILED');self.assertEqual(path.read_text(),before)



class ParallelFetchTests(unittest.TestCase):
    def setUp(self): ps.DATASETS.clear()

    def _session(self):
        def get(url, params=None, **kw):
            start = dt.datetime.strptime(params['periodStart'], '%Y%m%d%H%M').replace(tzinfo=dt.timezone.utc)
            day = start.astimezone(e.TZ).date(); s, en = e.day_bounds(day)
            kind = {'A65': 'load_forecast', 'A11': 'physical_flows'}.get(params['documentType'], 'hr_schedules')
            if kind == 'load_forecast':
                return Mock(status_code=200, content=document(kind, 'BA', 'BA', start=s, end=en))
            a = next(k for k, v in e.AREAS.items() if v == params['out_Domain'])
            b = next(k for k, v in e.AREAS.items() if v == params['in_Domain'])
            return Mock(status_code=200, content=document(kind, a, b, start=s, end=en, positions=[(1, len(a + b))]))
        session = Mock(); session.get.side_effect = get
        return session

    @staticmethod
    def _rows(tmp, name):
        import csv
        rows = list(csv.DictReader(open(Path(tmp) / name)))
        return [{k: v for k, v in r.items() if k != 'Fetched_At_UTC'} for r in rows]

    @patch('entsoe_adapter.time.sleep')
    def test_three_workers_match_sequential_output_and_status(self, _):
        out = {}
        for workers in (1, 3):
            ps.DATASETS.clear()
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session()
                e.update_entsoe(tmp, session=session, token='dummy', include_physical=True, workers=workers)
                names = ('entsoe_load_forecast.csv', 'entsoe_physical_flows.csv', 'entsoe_hr_schedules.csv')
                out[workers] = ({n: self._rows(tmp, n) for n in names},
                                {k: (v['state'], v.get('row_count'), v.get('recovered_days'), v.get('unavailable_days'))
                                 for k, v in ps.DATASETS.items()},
                                session.get.call_count)
        self.assertEqual(out[1][0], out[3][0])   # identical rows in every CSV
        self.assertEqual(out[1][1], out[3][1])   # identical per-dataset state, counts, recovered/unavailable days
        self.assertEqual(out[1][2], out[3][2])   # same number of API requests
        self.assertTrue(all(v[0] == 'PARTIAL' or v[0] == 'OK' for v in out[3][1].values()))

    @patch('entsoe_adapter.time.sleep')
    def test_rate_limit_stops_all_workers(self, _):
        for code in (401, 403, 429):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                ps.DATASETS.clear(); session = Mock()
                session.get.return_value = Mock(status_code=code)
                e.update_entsoe(tmp, DAY, session=session, token='dummy', workers=3)
                self.assertLessEqual(session.get.call_count, 3)   # only requests already in flight
                self.assertEqual(len(ps.DATASETS), 3)
                self.assertTrue(all(v['state'] == 'FAILED' for v in ps.DATASETS.values()))
                self.assertFalse(list(Path(tmp).glob('*.csv')))

    def test_stop_flag_blocks_next_request_after_in_flight_failure(self):
        calls = []
        def get(url, params=None, **kw):
            calls.append(params['documentType'])
            return Mock(status_code=429)
        session = Mock(); session.get.side_effect = get
        with patch('entsoe_adapter.time.sleep'), tempfile.TemporaryDirectory() as tmp:
            e.update_entsoe(tmp, DAY, session=session, token='dummy', workers=2)
        self.assertLessEqual(len(calls), 2)

    def test_pacer_spaces_requests_token_wide(self):
        sleeps = []
        clock = [100.0]
        with patch('entsoe_adapter.time.monotonic', lambda: clock[0]), \
             patch('entsoe_adapter.time.sleep', lambda s: sleeps.append(round(s, 2))):
            p = e._Pacer(0.35)
            for _ in range(4):
                p.wait()
        self.assertEqual(sleeps, [0.35, 0.7, 1.05])

    def test_default_is_sequential_and_workers_capped(self):
        import inspect
        self.assertIsNone(inspect.signature(e.update_entsoe).parameters['workers'].default)
        self.assertEqual(e.MAX_WORKERS, 4)
