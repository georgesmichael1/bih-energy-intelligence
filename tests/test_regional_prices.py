import datetime as dt
import io,csv,os,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from zoneinfo import ZoneInfo
import regional_prices as r
from pipeline_status import DATASETS

def xml(market,day,price=-1):
    tz=ZoneInfo(r.MARKETS[market][1]);start=dt.datetime.combine(day,dt.time(),tz).astimezone(dt.timezone.utc);end=dt.datetime.combine(day+dt.timedelta(days=1),dt.time(),tz).astimezone(dt.timezone.utc)
    eic={'RS':'10YCS-SERBIATSOV','HU':'10YHU-MAVIR----U'}[market]
    return f'<Publication_MarketDocument><TimeSeries><in_Domain.mRID>{eic}</in_Domain.mRID><out_Domain.mRID>{eic}</out_Domain.mRID><currency_Unit.name>EUR</currency_Unit.name><price_Measure_Unit.name>MWH</price_Measure_Unit.name><curveType>A03</curveType><Period><timeInterval><start>{start.isoformat()}</start><end>{end.isoformat()}</end></timeInterval><resolution>PT15M</resolution><Point><position>1</position><price.amount>{price}</price.amount></Point></Period></TimeSeries></Publication_MarketDocument>'.encode()

class RegionalTests(unittest.TestCase):
 def setUp(self):DATASETS.clear()
 def run_feed(self,session,day):
  return r.update_regional_prices('prices.csv',session,dt.datetime.combine(day,dt.time(12),dt.timezone.utc))
 def test_today_both_markets_dst_negative_zero(self):
  for day,count in [(dt.date(2026,11,24),96),(dt.date(2026,3,29),92),(dt.date(2026,10,25),100)]:
   with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,ENTSOE_API_TOKEN='fake-test-only'):
    old=os.getcwd();os.chdir(tmp)
    try:
     session=Mock();session.get.side_effect=[Mock(status_code=200,content=xml('HU',day,0)),Mock(status_code=200,content=xml('RS',day,-2))]
     rows=self.run_feed(session,day);self.assertEqual(len(rows),count*2);self.assertEqual(DATASETS['dam']['state'],'OK');self.assertNotIn('price_me',DATASETS)
     for args,kwargs in session.get.call_args_list:
      self.assertTrue(kwargs['verify']);self.assertEqual(kwargs['params']['documentType'],'A44');self.assertEqual(kwargs['params']['contract_MarketAgreement.type'],'A01')
     self.assertNotIn('fake-test-only',Path('prices.csv').read_text())
    finally:os.chdir(old)
 def test_missing_token_and_failure_preserve_cache(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=True):
   path=Path(tmp)/'prices.csv';path.write_bytes(b'cache');session=Mock()
   self.assertEqual(r.update_regional_prices(path,session,dt.datetime(2026,11,24,tzinfo=dt.timezone.utc)),[]);self.assertEqual(path.read_bytes(),b'cache');session.get.assert_not_called();self.assertEqual(DATASETS['dam']['state'],'PARTIAL')
 def test_wrong_date_no_false_current_or_token_in_status(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,ENTSOE_API_TOKEN='secret-test-only'):
   session=Mock();session.get.return_value=Mock(status_code=200,content=xml('HU',dt.date(2026,11,23)))
   rows=r.update_regional_prices(Path(tmp)/'prices.csv',session,dt.datetime(2026,11,24,tzinfo=dt.timezone.utc));self.assertEqual(rows,[]);self.assertEqual(DATASETS['price_hu']['state'],'FAILED');self.assertNotIn('secret-test-only',str(DATASETS))
 def test_cache_replaces_each_market_day(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,ENTSOE_API_TOKEN='fake'):
   path=Path(tmp)/'prices.csv';day=dt.date(2026,11,24)
   for price in [1,2]:
    session=Mock();session.get.side_effect=[Mock(status_code=200,content=xml('HU',day,price)),Mock(status_code=200,content=xml('RS',day,price))]
    r.update_regional_prices(path,session,dt.datetime(2026,11,24,tzinfo=dt.timezone.utc))
   rows=list(csv.DictReader(io.StringIO(path.read_text())));self.assertEqual(len(rows),192);self.assertTrue(all(float(x['Price_EUR_MWh'])==2 for x in rows))
