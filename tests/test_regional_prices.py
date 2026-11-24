import csv
import io
import os
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import regional_prices as r
from pipeline_status import DATASETS


def payload(day):
    tz = ZoneInfo('Europe/Budapest')
    start = int(dt.datetime.combine(day, dt.time(), tz).timestamp())
    end = int(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tz).timestamp())
    times = list(range(start, end, 900))
    return {'license_info': 'CC BY 4.0 from Bundesnetzagentur | SMARD.de',
            'unit': 'EUR / MWh', 'deprecated': False,
            'unix_seconds': times, 'price': [-1.0] * len(times)}


class RegionalPriceTests(unittest.TestCase):
    def setUp(self):
        DATASETS.clear()

    def test_complete_days_and_dst(self):
        for day, count in [(dt.date(2026, 11, 24), 96), (dt.date(2026, 3, 29), 92),
                           (dt.date(2026, 10, 25), 100)]:
            rows = r.parse_hu_day(payload(day), day)
            self.assertEqual(len(rows), count)
            self.assertEqual(rows[0]['Price_EUR_MWh'], -1)
            self.assertEqual(rows[0]['Market'], 'HU')
            self.assertEqual(rows[0]['Product'], 'DAY_AHEAD')
            self.assertEqual(rows[0]['Delivery_Date'], day.isoformat())

    def test_rejects_bad_licence_unit_null_gaps_duplicates(self):
        day = dt.date(2026, 11, 24)
        mutations = [lambda p: p.update(license_info='internal use only'),
                     lambda p: p.update(unit='BAM/MWh'),
                     lambda p: p['price'].__setitem__(0, None),
                     lambda p: p['unix_seconds'].pop(),
                     lambda p: p['unix_seconds'].__setitem__(1, p['unix_seconds'][0])]
        for change in mutations:
            p = payload(day)
            change(p)
            with self.assertRaises(ValueError):
                r.parse_hu_day(p, day)

    def test_today_ok_tomorrow_unpublished_and_restricted_not_fetched(self):
        day = dt.date(2026, 11, 24)
        session = Mock()
        response = Mock()
        response.json.return_value = payload(day)
        future = Mock()
        future.json.return_value = {**payload(day), 'price': []}
        session.get.side_effect = [response, future]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'prices.csv'
            rows = r.update_regional_prices(target, session, dt.datetime(2026, 11, 24, 9, tzinfo=dt.timezone.utc))
            self.assertEqual(len(rows), 96)
            self.assertTrue(target.exists())
            self.assertEqual(DATASETS['price_hu']['state'], 'OK')
            self.assertEqual(DATASETS['price_rs']['state'], 'BLOCKED_RIGHTS')
            self.assertEqual(DATASETS['price_me']['state'], 'BLOCKED_RIGHTS')
            self.assertEqual(DATASETS['dam']['state'], 'PARTIAL')
        self.assertEqual(session.get.call_count, 2)
        for args, kwargs in session.get.call_args_list:
            self.assertEqual(args[0], r.HU_SOURCE)
            self.assertTrue(kwargs['verify'])

    def test_all_fail_preserves_cache(self):
        session = Mock()
        session.get.side_effect = r.requests.Timeout('mock timeout')
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'prices.csv'
            target.write_bytes(b'cached original\n')
            rows = r.update_regional_prices(target, session, dt.datetime(2026, 11, 24, tzinfo=dt.timezone.utc))
            self.assertEqual(rows, [])
            self.assertEqual(target.read_bytes(), b'cached original\n')
            self.assertEqual(DATASETS['price_hu']['state'], 'FAILED')


class IntegrationTests(unittest.TestCase):
    def test_master_labels_preserve_numbers_source_and_other_rows(self):
        row = {'Record_ID': 'BIH-0487', 'Primary_Value': '279.2',
               'Verification_Status_Source_URL': 'VERIFIED 22.11.2026 | RS',
               'Status_Stage_Tier': 'LIVE DAILY FEED',
               'Entity_Project_Indicator_Name': 'SEEPEX/HUPX', 'Extra': 'keep'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'master.csv'
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=list(row))
            writer.writeheader()
            writer.writerows([row, {**row, 'Record_ID': 'OTHER'}])
            path.write_text(output.getvalue())
            r.mark_legacy_master(path)
            rows = list(csv.DictReader(io.StringIO(path.read_text())))
            self.assertEqual(rows[0]['Primary_Value'], '279.2')
            self.assertEqual(rows[0]['Extra'], 'keep')
            self.assertTrue(rows[0]['Status_Stage_Tier'].startswith('HISTORICAL'))
            self.assertIn('VERIFIED 22.11.2026 | RS', rows[0]['Verification_Status_Source_URL'])
            self.assertNotIn('HUPX', rows[0]['Entity_Project_Indicator_Name'])
            self.assertEqual(rows[1], {**row, 'Record_ID': 'OTHER'})
            before = path.read_bytes()
            r.mark_legacy_master(path)
            self.assertEqual(before, path.read_bytes())

    def test_cli_preserves_prior_dataset_results_and_legacy_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                Path('status.json').write_text(json.dumps({
                    'dataset_details': {'nos': {'state': 'OK', 'checked_at': 'historic', 'detail': 'ok'}},
                    'rows': 487, 'nos_custom': 'keep', 'last_verified_date': 'historic'}))
                def update():
                    r.record('price_hu', 'FAILED', detail='mock failure')
                with patch.object(r, 'update_regional_prices', side_effect=update):
                    r.main()
                status = json.loads(Path('status.json').read_text())
                self.assertEqual(status['dataset_details']['nos']['state'], 'OK')
                self.assertEqual(status['dataset_details']['price_hu']['state'], 'FAILED')
                self.assertEqual(status['nos_custom'], 'keep')
                self.assertEqual(status['rows'], 487)
                self.assertEqual(status['last_verified_date'], 'historic')
            finally:
                os.chdir(old)

    def test_history_retention_replaces_dates_and_old_cache_never_current(self):
        day = dt.date(2026, 11, 24)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'prices.csv'
            prior = r.parse_hu_day(payload(day-dt.timedelta(days=1)), day-dt.timedelta(days=1))
            for row in prior:
                row['Fetched_At'] = 'old'
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=r.COLUMNS)
            writer.writeheader()
            writer.writerows(prior)
            target.write_text(output.getvalue())
            session = Mock()
            response = Mock()
            response.json.return_value = payload(day)
            session.get.return_value = response
            r.update_regional_prices(target, session, dt.datetime(2026,11,24,tzinfo=dt.timezone.utc))
            self.assertEqual(len(list(csv.DictReader(io.StringIO(target.read_text())))),192)
            session.get.side_effect = r.requests.Timeout('mock')
            before = target.read_bytes()
            r.update_regional_prices(target, session, dt.datetime(2026,11,25,tzinfo=dt.timezone.utc))
            self.assertEqual(DATASETS['price_hu']['state'], 'FAILED')
            self.assertEqual(target.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
