import json
from pathlib import Path
import tempfile
import os
import unittest
from unittest.mock import patch

import pandas as pd
import update_bih as u
import pipeline_status as ps

class PipelineTests(unittest.TestCase):
    def setUp(self): ps.DATASETS.clear()

    def test_master_keeps_all_rows_columns_and_values_when_sources_unverified(self):
        # Includes a hypothetical newer consumer row, not just the current 487.
        original=pd.DataFrame([
            {'Record_ID':key,'Primary_Value':'original','Secondary_Value':'original',
             'Tariff_Price_Rate_Value':'original','Extra_Consumer_Field':'keep'}
            for key in ['BIH-0092','BIH-0128','BIH-0487','BIH-9999']])
        with tempfile.TemporaryDirectory() as tmp:
            csv=Path(tmp)/'bih_data.csv'; original.to_csv(csv,index=False)
            status=Path(tmp)/'status.json'; status.write_text('{"last_verified_date":"historic"}')
            u.sync_github_master_csv(None,{'Status':'FALLBACK'},None,pd.DataFrame(columns=['Year']),{},csv,status)
            pd.testing.assert_frame_equal(pd.read_csv(csv),original)
            self.assertEqual(json.loads(status.read_text())['last_verified_date'],'historic')

    def test_main_smoke_all_network_mocked(self):
        fx={'EUR_BAM_Middle':1.95,'USD_BAM_Buy':1.7,'USD_BAM_Middle':1.8,'USD_BAM_Sell':1.9,'Status':'FALLBACK'}
        with tempfile.TemporaryDirectory() as tmp:
            old=os.getcwd(); os.chdir(tmp)
            try:
                pd.DataFrame([{'Record_ID':'BIH-0128','Primary_Value':'old'}]).to_csv('bih_data.csv',index=False)
                with patch.object(u,'fetch_nosbih_balancing_prices',return_value=pd.DataFrame()), \
                     patch.object(u,'fetch_cbbh_fx_rates',return_value=fx), \
                     patch.object(u,'fetch_worldometer_bih_population',return_value=(1,pd.DataFrame(columns=['Year']))), \
                     patch.object(u,'fetch_bhas_headline_kpis',return_value={}), \
                     patch.object(u,'fetch_worldbank_bih_macro',return_value=pd.DataFrame()), \
                     patch.object(u,'update_nos_history',side_effect=RuntimeError('mocked')), \
                     patch.object(u,'update_flows_history',side_effect=RuntimeError('mocked')), \
                     patch.object(u,'build_bih_intelligence_workbook') as workbook, \
                     patch.dict(os.environ,{'BUILD_LEGACY_XLSX':'0'}):
                    u.main()
                    workbook.assert_not_called()
                status=json.loads(Path('status.json').read_text())
                self.assertEqual(status['rows'],1)
                self.assertNotIn('entsoe', status['dataset_details'])  # ENTSO-E now runs in entsoe_daily.yml
                self.assertFalse(list(Path('.').glob('*.xlsx')))
            finally: os.chdir(old)

    def test_default_requests_has_retry_adapter(self):
        session=u.create_resilient_session()
        self.assertIsInstance(session,u.std_requests.Session)
        self.assertEqual(session.get_adapter('https://').max_retries.total,3)
        session.close()

    def test_fallback_fx_does_not_claim_today(self):
        with patch.object(u,'safe_get',side_effect=RuntimeError('offline')):
            rates=u.fetch_cbbh_fx_rates()
        self.assertIsNone(rates['Date'])
        self.assertEqual(rates['Status'],'FALLBACK')
        self.assertEqual(ps.DATASETS['fx']['state'],'FALLBACK')

    def test_static_balancing_label(self):
        with patch.object(u,'safe_get',side_effect=RuntimeError('offline')):
            data=u.fetch_nosbih_balancing_prices()
        self.assertTrue(data.empty)
        self.assertEqual(ps.DATASETS['balancing']['state'],'FAILED')

    def test_successful_population_uses_returned_values_only(self):
        ps.DATASETS.clear(); ps.record('population','OK')
        data=pd.DataFrame([{'Record_ID':'BIH-0092','Primary_Value':'old','Secondary_Value':'historical narrative',
                            'Tariff_Price_Rate_Value':'old'}])
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'data.csv'; data.to_csv(path,index=False)
            u.sync_github_master_csv(None,{},3000001,pd.DataFrame([{'Year':2026,'WM_Population_Headcount':3000002}]),{},path,Path(tmp)/'status.json')
            result=pd.read_csv(path)
            self.assertIn('3,000,002',result.iloc[0]['Tariff_Price_Rate_Value'])
            self.assertEqual(result.iloc[0]['Secondary_Value'],'historical narrative')

if __name__=='__main__': unittest.main()
