import unittest
from datetime import datetime, timezone

import pandas as pd

from domestic_data import parse_sina_quotes, add_sina_intraday, apply_sina_adjustment
from local_stock_report import analyze_snapshot, render_report
from scoring_core import WATCHLIST


class SinaIntradayTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 20, 18, tzinfo=timezone.utc)
        self.rows = [{"date": str(d.date()), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000} for d in pd.bdate_range('2026-09-01', '2026-11-19')]
        fields = ['name','95','-5','2026-11-21 01:59:00','-5','99','101','94','150','70','2000']
        self.text = 'var hq_str_gb_lly="' + ','.join(fields) + '";'

    def test_quote_timestamp_converts_beijing_to_new_york_date(self):
        quote = parse_sina_quotes(self.text)['LLY']
        self.assertEqual(quote['date'], '2026-11-20')
        self.assertEqual(quote['time'].astimezone(timezone.utc).hour, 17)
        self.assertEqual(quote['close'],95)
        self.assertEqual(quote['volume'],2000)

    def test_current_quote_changes_only_intraday_score(self):
        quote = parse_sina_quotes(self.text)['LLY']
        rows, added = add_sina_intraday(self.rows, quote, self.now)
        self.assertTrue(added)
        bars = apply_sina_adjustment(rows, [{'d':'2026-01-01','f':0.5,'c':-1}], self.now.date().replace(month=9,day=1), self.now)
        self.assertEqual(bars[-1]['Close'],46.5)
        stamp=self.now.isoformat()
        data={'schema_version':1,'adjusted':True,'source':'新浪财经','fetched_at_utc':stamp,'errors':[],
              'symbols':{t:{'fetched_at_utc':stamp,'quote_time_utc':quote['time'].isoformat(),'bars':bars} for t in WATCHLIST}}
        result=analyze_snapshot(data)
        self.assertEqual(result['errors'],[])
        self.assertEqual(result['reports']['closed'][0]['date'],'2026-11-19')
        self.assertEqual(result['reports']['closed'][0]['close'],49)
        self.assertEqual(result['reports']['intraday'][0]['date'],'2026-11-20')
        self.assertTrue(result['reports']['intraday'][0]['unconfirmed'])
        html=render_report(data,result,'/api/export')
        self.assertIn('报价时间：2026-11-21 01:59:00',html)
        self.assertIn('8/8',html)

    def test_stale_closed_and_weekend_quotes_are_not_used_as_live(self):
        quote=parse_sina_quotes(self.text)['LLY']
        for now in (datetime(2026,11,20,19,tzinfo=timezone.utc), datetime(2026,11,20,22,tzinfo=timezone.utc),datetime(2026,11,21,18,tzinfo=timezone.utc)):
            rows,added=add_sina_intraday(self.rows,quote,now)
            self.assertFalse(added)
            self.assertEqual(rows,self.rows)

    def test_bad_or_empty_quote_is_rejected(self):
        self.assertEqual(parse_sina_quotes('var hq_str_gb_lly="";'),{})
        self.assertEqual(parse_sina_quotes(self.text.replace(',95,',',NaN,')),{})


if __name__=='__main__':
    unittest.main()

