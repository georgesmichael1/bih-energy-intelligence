import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import openpyxl
import imbalance_feed as f
import pipeline_status as ps


def book(day, periods=96, price=(80, 200), blank_at=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = day.strftime('%d.%m.%Y.')
    ws['B2'] = 'Imbalance price'
    ws['B4'] = 'Date:'
    ws['C4'] = day.strftime('%d. %m. %Y')
    ws.cell(6, 5, 'aFRR\nUP'); ws.cell(6, 8, 'mFRR\nUP')
    ws.cell(6, 11, 'Imbalance price\nPOSITIVE'); ws.cell(6, 12, 'Imbalance price\nNEGATIVE')
    for i in range(periods):
        r = 9 + i
        ws.cell(r, 2, f'{i * 15 // 60}:{i * 15 % 60:02}')
        ws.cell(r, 3, f'{(i + 1) * 15 // 60}:{(i + 1) * 15 % 60:02}')
        ws.cell(r, 5, 0); ws.cell(r, 6, 0.5); ws.cell(r, 8, 0); ws.cell(r, 9, 0)
        ws.cell(r, 11, None if blank_at == i else price[0]); ws.cell(r, 12, price[1])
    ws.cell(9 + periods + 1, 2, f'{day:%Y%m%d}_REGULATION-DATA, ver. 2, Intermediate')
    out = io.BytesIO(); wb.save(out)
    return out.getvalue()


class ImbalanceTests(unittest.TestCase):
    def test_parse_keeps_blank_empty_and_converts_eur(self):
        d = dt.date(2026, 11, 25)
        days = f.parse_workbook(book(d, blank_at=3), 'src')
        rows = days[d]
        self.assertEqual(len(rows), 96)
        self.assertIsNone(rows[3]['Price_Positive_KM_MWh'])
        self.assertIsNone(rows[3]['Price_Positive_EUR_MWh'])
        self.assertEqual(rows[0]['Price_Negative_EUR_MWh'], round(200 / 1.95583, 2))
        self.assertEqual(rows[95]['End'], '24:00')

    def test_bad_period_count_and_non_excel_rejected(self):
        with self.assertRaises(ValueError):
            f.parse_workbook(book(dt.date(2026, 11, 25), periods=50), 's')
        with self.assertRaises(ValueError):
            f.parse_workbook(b'<html>', 's')

    def test_dst_day_with_92_periods_accepted(self):
        self.assertEqual(len(f.parse_workbook(book(dt.date(2026, 10, 25), periods=92), 's')[dt.date(2026, 10, 25)]), 92)

    def test_update_handles_unpublished_day_and_failure_keeps_saved(self):
        ps.DATASETS.clear()
        today = dt.date(2026, 11, 3)
        def get(url):
            r = Mock()
            if '20261101' in url or '20261102' in url:
                r.status_code = 200; r.content = book(dt.date.fromisoformat(
                    url.split('/')[-1][:8][:4] + '-' + url.split('/')[-1][4:6] + '-' + url.split('/')[-1][6:8]))
            else:
                r.status_code = 404; r.content = b''
            return r
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'x.csv'
            days = f.update_imbalance(get, today=today, path=str(p), start=dt.date(2026, 11, 1))
            self.assertEqual(sorted(days), ['2026-11-01', '2026-11-02'])
            self.assertEqual(ps.DATASETS['nos_imbalance_15min']['state'], 'OK')
            saved = p.read_text()
            def bad(url):
                raise RuntimeError('offline')
            self.assertIsNone(f.update_imbalance(bad, today=today, path=str(p), start=dt.date(2026, 11, 1)))
            self.assertEqual(ps.DATASETS['nos_imbalance_15min']['state'], 'FAILED')
            self.assertEqual(p.read_text(), saved)


if __name__ == '__main__':
    unittest.main()
