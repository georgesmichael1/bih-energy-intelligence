import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
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


if __name__ == '__main__':
    unittest.main()
