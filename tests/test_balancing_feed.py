import datetime as dt
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import openpyxl
import balancing_feed as b
import pipeline_status as ps

import current_gdp as g

class CurrentGdpTests(unittest.TestCase):
    def payload(self):
        return {'indicators': {'NGDPD': {'unit': 'Billions of U.S. dollars', 'dataset': 'WEO', 'label': 'GDP, current prices', 'source': 'World Economic Outlook (October 2026)', 'last-modified': '2026-10-08 14:43:33', 'projection-year': 2026}}, 'values': {'NGDPD': {'BIH': {'2026': 37.264, '2025': 33.589}}}}

    def test_current_nominal_metric_and_forecast(self):
        row=g.parse_current(self.payload(),2026,'stamp')
        self.assertEqual(row['Value_USD_Billions'],37.264)
        self.assertEqual(row['Status'],'IMF forecast')
        self.assertEqual(row['Year'],2026)

    def test_wrong_unit_missing_year_bad_value_rejected(self):
        p=self.payload();p['indicators']['NGDPD']['unit']='Percent'
        with self.assertRaises(ValueError):g.parse_current(p,2026,'stamp')
        p=self.payload();del p['values']['NGDPD']['BIH']['2026']
        with self.assertRaises(KeyError):g.parse_current(p,2026,'stamp')
        p=self.payload();p['values']['NGDPD']['BIH']['2026']=float('nan')
        with self.assertRaises(ValueError):g.parse_current(p,2026,'stamp')

    def test_failed_fetch_retains_saved_gdp(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'gdp.csv';p.write_text('saved')
            session=Mock();session.get.side_effect=RuntimeError('offline')
            self.assertIsNone(g.update_current_gdp(p,session))
            self.assertEqual(p.read_text(),'saved')

class BalancingTests(unittest.TestCase):
    def month(self):
        text = 'Balancing market report - September 2026\nKM/MWh 1EUR=1,95583KM\n'
        for label in b.PRODUCTS:
            text += label + ' 100 10.000 100,00 200,00\n'
        return text + 'Shortage 100 20 100,00 200,00\nSurplus 20 10 50,00 -20,00\n'

    def annual(self):
        w=openpyxl.Workbook(); s=w.active; s.title='Reg capacity'
        s.cell(2,1,'Report for the year 2025')
        for j,v in enumerate(['FCR','aFRR','aFRR','mFRR','mFRR'],3):s.cell(5,j,v)
        for j,v in enumerate(['','Off-peak','Peak','Upward','Downward'],3):s.cell(6,j,v)
        for i,(label,unit,val) in enumerate([('Required capacity','MW',20),('Delivered capacity','MW',10),('Price of contracted capacity','KM/MW/h',4)],8):
            s.cell(i,1,label);s.cell(i,2,unit)
            for j in range(3,8):s.cell(i,j,val)
        f=io.BytesIO();w.save(f);return f.getvalue()

    def test_monthly_negative_and_conversion(self):
        text=self.month().replace('mFRR Down 100 10.000 100,00 200,00','mFRR Down 100 -10.000 -100,00 -200,00')
        rows=b.parse_monthly(text,'2026-09','source','stamp')
        self.assertEqual(len(rows),18)
        self.assertAlmostEqual(next(r['Value'] for r in rows if r['Key']=='MFRR_DOWN_AVG'),-100/b.PEG)

    def test_missing_duplicate_wrong_title_units_rejected(self):
        for text in [self.month().replace('FCP Up 100 10.000 100,00 200,00\n',''),self.month()+'FCP Up 1 2 2,00 2,00\n',self.month().replace('September','October'),self.month().replace('KM/MWh','EUR/MWh'),self.month().replace('10.000','900.000')]:
            with self.assertRaises(ValueError):b.parse_monthly(text,'2026-09','s','t')

    def test_annual_five_products_units_and_values(self):
        rows=b.parse_capacity(self.annual(),'2025','s','t')
        self.assertEqual(len(rows),15)
        self.assertAlmostEqual(next(r['Value'] for r in rows if r['Key']=='AFRR_PEAK_PRICE'),4/b.PEG)
        with self.assertRaises(ValueError):b.parse_capacity(self.annual(),'2024','s','t')

    def test_discover_only_published_completed_periods(self):
        html=''.join('<a href="'+u+'">pdf</a>' for u in ['https://www.nosbih.ba/files/balansno/izvjestaji/mjeseci/202609-en-balansno-trziste-izvjestaj.pdf','https://www.nosbih.ba/files/balansno/izvjestaji/mjeseci/202612-en-balansno-trziste-izvjestaj.pdf','https://www.nosbih.ba/files/balansno/izvjestaji/godine/2025-en-balansno-trziste-tabele.xlsx'])
        self.assertEqual(b.links(html,dt.date(2026,11,24))[0],'2026-09')
        with self.assertRaises(ValueError):b.links('',dt.date(2026,11,24))

    def test_failure_preserves_saved_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'balancing.csv';p.write_text('unchanged')
            session=Mock();session.get.side_effect=RuntimeError('offline')
            self.assertIsNone(b.update_balancing(p,session))
            self.assertEqual(p.read_text(),'unchanged')
            self.assertEqual(ps.DATASETS['balancing']['state'],'FAILED')

if __name__=='__main__':unittest.main()
