import datetime as dt
import io,csv,os,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from zoneinfo import ZoneInfo
import regional_prices as r
from pipeline_status import DATASETS

def xml(market,day,price=-1):
    tz=ZoneInfo(r.MARKETS[market][1]);start=dt.datetime.combine(day,dt.time(),tz).astimezone(dt.timezone.utc);end=dt.datetime.combine(day+dt.timedelta(days=1),dt.time(),tz).astimezone(dt.timezone.utc)
    eic=r.AREAS[market]
    return f'<Publication_MarketDocument><TimeSeries><in_Domain.mRID>{eic}</in_Domain.mRID><out_Domain.mRID>{eic}</out_Domain.mRID><currency_Unit.name>EUR</currency_Unit.name><price_Measure_Unit.name>MWH</price_Measure_Unit.name><curveType>A03</curveType><Period><timeInterval><start>{start.isoformat()}</start><end>{end.isoformat()}</end></timeInterval><resolution>PT15M</resolution><Point><position>1</position><price.amount>{price}</price.amount></Point></Period></TimeSeries></Publication_MarketDocument>'.encode()

def serve(prices=None,unavailable=()):
 """Fake ENTSO-E: returns the delivery day that was requested, or HTTP 404 for unavailable days."""
 prices=prices or {}
 def get(url,params=None,**kw):
  market={v:k for k,v in r.AREAS.items()}[params['in_Domain']]
  tz=ZoneInfo(r.MARKETS[market][1])
  day=dt.datetime.strptime(params['periodStart'],'%Y%m%d%H%M').replace(tzinfo=dt.timezone.utc).astimezone(tz).date()
  if day.isoformat() in unavailable:return Mock(status_code=404,content=b'')
  return Mock(status_code=200,content=xml(market,day,prices.get(market,-1)))
 return get

class RegionalTests(unittest.TestCase):
 def setUp(self):DATASETS.clear()
 def run_feed(self,session,day):
  return r.update_regional_prices('prices.csv',session,dt.datetime.combine(day,dt.time(12),dt.timezone.utc))
 def test_today_both_markets_dst_negative_zero(self):
  for day,count in [(dt.date(2026,11,24),96),(dt.date(2026,3,29),92),(dt.date(2026,10,25),100)]:
   with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,ENTSOE_API_TOKEN='fake-test-only'),patch.object(r.time,'sleep'):
    old=os.getcwd();os.chdir(tmp)
    try:
     session=Mock();session.get.side_effect=serve({'HU':0,'RS':-2,'HR':5.5,'ME':-0.01})
     rows=self.run_feed(session,day)
     today=[x for x in rows if x['Delivery_Date']==day.isoformat()]
     self.assertEqual(len(today),count*4);self.assertEqual(DATASETS['price_hu']['state'],'OK');self.assertEqual({x['Market'] for x in today},{'HU','RS','HR','ME'});self.assertIn('not named',[x for x in today if x['Market']=='HR'][0]['Attribution']);self.assertIn('not named',[x for x in today if x['Market']=='ME'][0]['Attribution']);self.assertEqual({float(x['Price_EUR_MWh']) for x in today if x['Market']=='HR'},{5.5});self.assertEqual({float(x['Price_EUR_MWh']) for x in today if x['Market']=='ME'},{-0.01})
     self.assertEqual({float(x['Price_EUR_MWh']) for x in today if x['Market']=='HU'},{0.0});self.assertEqual({float(x['Price_EUR_MWh']) for x in today if x['Market']=='RS'},{-2.0})
     for args,kwargs in session.get.call_args_list:
      self.assertTrue(kwargs['verify']);self.assertEqual(kwargs['params']['documentType'],'A44');self.assertEqual(kwargs['params']['contract_MarketAgreement.type'],'A01')
     self.assertNotIn('fake-test-only',Path('prices.csv').read_text())
    finally:os.chdir(old)
 def test_window_recovers_over_runs_then_ok_and_prunes_dst_safe(self):
  now=dt.datetime(2026,11,24,12,tzinfo=dt.timezone.utc)
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,ENTSOE_API_TOKEN='fake'),patch.object(r.time,'sleep'):
   path=Path(tmp)/'p.csv'
   states=[]
   for _ in range(4):
    session=Mock();session.get.side_effect=serve();r.update_regional_prices(path,session,now);states.append(DATASETS['dam']['state'])
   self.assertEqual(states,['OK','OK','OK','OK'])  # no per-run day cap: the whole window is recovered in one run
   rows=list(csv.DictReader(io.StringIO(path.read_text())))
   self.assertEqual({x['Delivery_Date'] for x in rows},{(dt.date(2026,11,24)-dt.timedelta(days=n)).isoformat() for n in range(31)})
   # Window rolls: a day later the oldest day is pruned and nothing duplicates
   session=Mock();session.get.side_effect=serve();r.update_regional_prices(path,session,now+dt.timedelta(days=1))
   rows=list(csv.DictReader(io.StringIO(path.read_text())))
   self.assertNotIn('2026-10-25',{x['Delivery_Date'] for x in rows})
   keys=[(x['Market'],x['Interval_Start_UTC']) for x in rows];self.assertEqual(len(keys),len(set(keys)))
 def test_all_missing_days_attempted_in_one_run_and_failures_named(self):
  now=dt.datetime(2026,11,24,12,tzinfo=dt.timezone.utc)
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,ENTSOE_API_TOKEN='fake'),patch.object(r.time,'sleep'):
   path=Path(tmp)/'p.csv'
   session=Mock();session.get.side_effect=serve(unavailable={'2026-11-10'})
   r.update_regional_prices(path,session,now)
   st=DATASETS['price_hr']
   self.assertEqual(len(st['attempted_days']),30);self.assertEqual(st['incomplete_days_remaining'],['2026-11-10'])
   self.assertEqual(st['state'],'PARTIAL');self.assertIn('2026-11-10',st['unavailable_reasons'])
   self.assertNotIn('securityToken',str(st))
 def test_verified_source_gap_is_ok_but_unverified_empty_day_stays_partial(self):
  now=dt.datetime(2026,11,24,12,tzinfo=dt.timezone.utc)
  empty=b'<Acknowledgement_MarketDocument><Reason><text>No matching data found</text></Reason></Acknowledgement_MarketDocument>'
  def make(empty_days):
   base=serve()
   def get(url,params=None,**kw):
    market={v:k for k,v in r.AREAS.items()}[params['in_Domain']]
    tz=ZoneInfo(r.MARKETS[market][1])
    day=dt.datetime.strptime(params['periodStart'],'%Y%m%d%H%M').replace(tzinfo=dt.timezone.utc).astimezone(tz).date()
    if (market,day.isoformat()) in empty_days:return Mock(status_code=200,content=empty)
    return base(url,params=params,**kw)
   return get
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,ENTSOE_API_TOKEN='fake'),patch.object(r.time,'sleep'):
   path=Path(tmp)/'p.csv'
   session=Mock();session.get.side_effect=make({('HR','2026-11-10'),('ME','2026-11-09')})
   with patch.dict(r.VERIFIED_SOURCE_GAPS,{('HR','2026-11-10'):{'checked_at':'t','evidence':'e','evidence_url':'u'}}):
    r.update_regional_prices(path,session,now)
   hr,me=DATASETS['price_hr'],DATASETS['price_me']
   self.assertEqual(hr['state'],'OK');self.assertEqual(hr['verified_source_gaps'][0]['date'],'2026-11-10');self.assertIn('verified source gap',hr['detail'])
   self.assertEqual(me['state'],'PARTIAL');self.assertEqual(me['incomplete_days_remaining'],['2026-11-09'])
   rows=list(csv.DictReader(io.StringIO(Path(r.extra_csv_path(path)).read_text())))
   self.assertIn('2026-11-09',{x['Delivery_Date'] for x in rows if x['Market']=='HR'})
   self.assertNotIn('2026-11-10',{x['Delivery_Date'] for x in rows if x['Market']=='HR'})  # empty, never zero
 def test_unavailable_day_not_zero_failure_visible_saved_kept(self):
  now=dt.datetime(2026,11,24,12,tzinfo=dt.timezone.utc)
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,ENTSOE_API_TOKEN='fake'),patch.object(r.time,'sleep'):
   path=Path(tmp)/'p.csv'
   for _ in range(4):
    session=Mock();session.get.side_effect=serve();r.update_regional_prices(path,session,now)
   before=path.read_text()
   session=Mock();session.get.side_effect=serve(unavailable={'2026-11-24','2026-11-10'})
   r.update_regional_prices(path,session,now)
   self.assertEqual(DATASETS['price_hu']['state'],'FAILED');self.assertEqual(path.read_text(),before)
 def test_missing_token_and_failure_preserve_cache(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=True):
   path=Path(tmp)/'prices.csv';path.write_bytes(b'cache');session=Mock()
   self.assertEqual(r.update_regional_prices(path,session,dt.datetime(2026,11,24,tzinfo=dt.timezone.utc)),[]);self.assertEqual(path.read_bytes(),b'cache');self.assertTrue(all('labs.hupx.hu' in c.args[0] for c in session.get.call_args_list),'no token-needing source may be called without a token');self.assertEqual(DATASETS['dam']['state'],'PARTIAL')
 def test_wrong_date_no_false_current_or_token_in_status(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,ENTSOE_API_TOKEN='secret-test-only'):
   session=Mock();session.get.return_value=Mock(status_code=200,content=xml('HU',dt.date(2026,11,23)))
   rows=r.update_regional_prices(Path(tmp)/'prices.csv',session,dt.datetime(2026,11,24,tzinfo=dt.timezone.utc));self.assertEqual(rows,[]);self.assertEqual(DATASETS['price_hu']['state'],'FAILED');self.assertNotIn('secret-test-only',str(DATASETS))
 def test_cache_replaces_each_market_day(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,ENTSOE_API_TOKEN='fake'):
   path=Path(tmp)/'prices.csv';day=dt.date(2026,11,24)
   for price in [1,2]:
    session=Mock();session.get.side_effect=serve({'HU':price,'RS':price,'HR':price,'ME':price})
    with patch.object(r.time,'sleep'):r.update_regional_prices(path,session,dt.datetime(2026,11,24,tzinfo=dt.timezone.utc))
   rows=[x for x in csv.DictReader(io.StringIO(path.read_text())) if x['Delivery_Date']=='2026-11-24'];self.assertEqual(len(rows),192);self.assertTrue(all(float(x['Price_EUR_MWh'])==2 for x in rows))
   self.assertEqual({x['Market'] for x in rows},{'HU','RS'})
   extra=Path(r.extra_csv_path(path));self.assertTrue(extra.exists())
   rows2=[x for x in csv.DictReader(io.StringIO(extra.read_text())) if x['Delivery_Date']=='2026-11-24'];self.assertEqual(len(rows2),192);self.assertTrue(all(float(x['Price_EUR_MWh'])==2 for x in rows2))
   self.assertEqual({x['Market'] for x in rows2},{'HR','ME'})

def hupx_payload(day):
    return {"data": [{"BaseloadPrice": 150.0, "DeliveryDay": day.isoformat() + "T00:00:00Z",
                      "Price": 100.0 + (i % 5), "ProductQH": str(i + 1), "Region": "HU",
                      "Status": "final", "Volume": 1000.0}
                     for i in range(int(((dt.datetime.combine(day+dt.timedelta(days=1),dt.time(),ZoneInfo(r.MARKETS["HU"][1])).astimezone(dt.timezone.utc)-(dt.datetime.combine(day,dt.time(),ZoneInfo(r.MARKETS["HU"][1])).astimezone(dt.timezone.utc))).total_seconds()//900)))]}

class HupxFallbackTests(unittest.TestCase):
 def setUp(self):DATASETS.clear()
 def test_hu_today_falls_back_to_hupx_when_entsoe_lacks_day(self):
  day=dt.date(2026,11,21)
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,ENTSOE_API_TOKEN='fake-test-only'),patch.object(r.time,'sleep'):
   old=os.getcwd();os.chdir(tmp)
   try:
    def routed(url,params=None,**kw):
     if 'labs.hupx.hu' in url:
      m=Mock(status_code=200);m.json=lambda:hupx_payload(day);return m
     return serve({},unavailable={day.isoformat()})(url,params=params,**kw)
    session=Mock();session.get.side_effect=routed
    rows=r.update_regional_prices('prices.csv',session,dt.datetime.combine(day,dt.time(12),dt.timezone.utc))
    hu_today=[x for x in rows if x['Market']=='HU' and x['Delivery_Date']==day.isoformat()]
    self.assertEqual(len(hu_today),96)
    self.assertEqual({x['Source'] for x in hu_today},{r.HUPX_SOURCE})
    self.assertEqual({x['License'] for x in hu_today},{r.HUPX_LICENSE_URL})
    self.assertIn('HUPX Labs',DATASETS['price_hu']['detail'])
    self.assertNotIn('fake-test-only',Path('prices.csv').read_text())
   finally:os.chdir(old)
 def test_rs_has_no_hupx_fallback(self):
  day=dt.date(2026,11,21)
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,ENTSOE_API_TOKEN='fake-test-only'),patch.object(r.time,'sleep'):
   old=os.getcwd();os.chdir(tmp)
   try:
    calls=[]
    def routed(url,params=None,**kw):
     calls.append(url)
     if 'labs.hupx.hu' in url:
      m=Mock(status_code=200);m.json=lambda:hupx_payload(day);return m
     return serve({},unavailable={day.isoformat()})(url,params=params,**kw)
    session=Mock();session.get.side_effect=routed
    rows=r.update_regional_prices('prices.csv',session,dt.datetime.combine(day,dt.time(12),dt.timezone.utc))
    rs_today=[x for x in rows if x['Market']=='RS' and x['Delivery_Date']==day.isoformat()]
    self.assertEqual(len(rs_today),0)
    self.assertEqual(DATASETS['price_rs']['state'],'FAILED')
   finally:os.chdir(old)
