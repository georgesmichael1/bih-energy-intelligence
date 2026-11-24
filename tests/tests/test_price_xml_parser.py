import datetime as dt
import unittest
from price_xml_parser import parse_prices


def xml(curve='A03', points=((1,10),(5,-1)), resolution='PT15M', end='2026-11-24T01:00Z', zone='RS'):
    data=''.join(f'<Point><position>{k}</position><price.amount>{v}</price.amount></Point>' for k,v in points)
    return f'<Publication_MarketDocument><TimeSeries><in_Domain.mRID>{zone}</in_Domain.mRID><out_Domain.mRID>{zone}</out_Domain.mRID><currency_Unit.name>EUR</currency_Unit.name><price_Measure_Unit.name>MWH</price_Measure_Unit.name><curveType>{curve}</curveType><Period><timeInterval><start>2026-11-23T23:00Z</start><end>{end}</end></timeInterval><resolution>{resolution}</resolution>{data}</Period></TimeSeries></Publication_MarketDocument>'


class PriceParserTests(unittest.TestCase):
    def test_a03_change_points_expand_until_period_end(self):
        rows=parse_prices(xml(),'RS')
        self.assertEqual(len(rows),8)
        self.assertEqual([r['price'] for r in rows],[10]*4+[-1]*4)
        self.assertEqual(rows[-1]['end'].isoformat(),'2026-11-24T01:00:00+00:00')
    def test_one_point_covers_period(self):
        self.assertEqual(len(parse_prices(xml(points=((1,0),)),'RS')),8)
    def test_bad_positions_rejected(self):
        for points in [((2,1),),((1,1),(1,2)),((1,1),(9,2)),((5,1),(1,2))]:
            with self.assertRaises(ValueError):parse_prices(xml(points=points),'RS')
    def test_a01_must_have_all_points(self):
        with self.assertRaises(ValueError):parse_prices(xml(curve='A01'),'RS')
    def test_zone_unit_curve_and_nonfinite_rejected(self):
        for source in [xml(zone='HU'),xml().replace('EUR','BAM'),xml(curve='A09'),xml(points=((1,float('nan')),))]:
            with self.assertRaises(ValueError):parse_prices(source,'RS')
    def test_duplicate_periods_rejected(self):
        source=xml();ts=source[source.index('<TimeSeries>'):source.index('</TimeSeries>')+13]
        with self.assertRaises(ValueError):parse_prices(source.replace('</Publication_MarketDocument>',ts+'</Publication_MarketDocument>'),'RS')

if __name__=='__main__':unittest.main(verbosity=2)
