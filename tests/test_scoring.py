import os
import smtplib
import unittest
from datetime import datetime
from email import message_from_string
from unittest.mock import MagicMock, patch

import pandas as pd

import stock_ai_scanner as scanner
from email_reports import render_email, sample_reports


def frame(close=105.0, rsi=50.0, hist=(-3.0, -2.0, -1.0)):
    return pd.DataFrame({"Close": [close]*3, "BOLL_LOWER": [100.0]*3,
                         "BOLL_MID": [110.0]*3, "RSI": [rsi]*3,
                         "MACD_DIF": [v/2 for v in hist], "MACD_DEA": [0.0]*3,
                         "MACD_HIST": list(hist)}, index=pd.date_range("2020-01-01", periods=3))


class ScoringTests(unittest.TestCase):
    def test_boll_bands_and_boundaries(self):
        for ticker, near, mid in [("QQQ",30,15), ("NVDA",25,10), ("ASML",25,10), ("COST",25,10)]:
            for close, expected in [(99,40), (100,near), (101 if ticker == "QQQ" else 101.5,near), (105,mid), (110,0)]:
                with self.subTest(ticker=ticker,close=close):
                    self.assertEqual(scanner.score_stock(ticker,frame(close=close))["parts"][0]["score"],expected)

    def test_rsi_bands_and_boundaries(self):
        for ticker, bands in [("QQQ", [(31.9,35),(32,25),(37.9,25),(38,15),(42.9,15),(43,0)]),
                              ("COST", [(29.9,35),(30,25),(34.9,25),(35,10),(39.9,10),(40,0)])]:
            for rsi, points in bands:
                with self.subTest(ticker=ticker,rsi=rsi):
                    self.assertEqual(scanner.score_stock(ticker,frame(rsi=rsi))["parts"][1]["score"],points)

    def test_macd_requires_three_negative_bars_for_two_days(self):
        for hist, expected in [((-3,-2,-1),20),((-1,-3,-2),10),((-1,-2,-3),0),
                               ((-1,-1,-1),0),((1,-2,-1),10),((-3,-1,0),0),((-2,-1,1),25)]:
            with self.subTest(hist=hist):
                self.assertEqual(scanner.score_stock("QQQ",frame(hist=hist))["parts"][2]["score"],expected)

    def test_total_tiers_and_exclusive_components(self):
        maximum = scanner.score_stock("QQQ",frame(close=99,rsi=20,hist=(-2,-1,1)))
        self.assertEqual(maximum["total"],100)
        self.assertEqual(maximum["tier"],"黄金坑候选")
        threshold = scanner.score_stock("COST",frame(close=99,rsi=35))
        self.assertEqual(threshold["total"],70)
        self.assertTrue(threshold["eligible"])
        self.assertEqual(threshold["tier"],"轻仓试探")
        self.assertFalse(scanner.score_stock("COST",frame())["eligible"])

    def test_threshold_gates_ai_and_email(self):
        data = pd.DataFrame({"Close":[100.0]*35},index=pd.date_range("2020-01-01",periods=35))
        below = scanner.score_stock("QQQ",frame())
        above = scanner.score_stock("COST",frame(close=99,rsi=35))
        for score, count in [(below,0),(above,1)]:
            with self.subTest(score=score["total"]), patch.dict(os.environ, {"AI_API_KEY":"test","QQ_EMAIL":"test@qq.com","QQ_AUTH_CODE":"test"}), patch.object(scanner,"WATCHLIST",[score["ticker"]]), patch.object(scanner.yf,"Ticker") as ticker, patch.object(scanner,"calculate_indicators",return_value=frame()), patch.object(scanner,"score_stock",return_value=score), patch.object(scanner,"ask_ai_analyst",return_value="test advice") as ai, patch.object(scanner,"send_qq_email") as send:
                ticker.return_value.history.return_value=data
                self.assertEqual(scanner.run_scan(),count)
                self.assertEqual(ai.call_count,count)
                self.assertEqual(send.call_count,count)

    def test_open_session_is_excluded(self):
        df=pd.DataFrame({"Close":[100,101]},index=pd.date_range("2026-11-18",periods=2,tz=scanner.NEW_YORK))
        self.assertEqual(len(scanner.completed_daily_bars(df,datetime(2026,11,19,15,59,tzinfo=scanner.NEW_YORK))),1)
        self.assertEqual(len(scanner.completed_daily_bars(df,datetime(2026,11,19,16,1,tzinfo=scanner.NEW_YORK))),2)

    def test_flat_price_rsi_is_neutral(self):
        self.assertEqual(scanner.calculate_indicators(pd.DataFrame({"Close":[100.0]*30})).iloc[-1]["RSI"],50.0)

    def test_email_escapes_ai_and_has_plain_fallback(self):
        reports=sample_reports()
        reports[0]["advice"]='<script>alert("x")</script>'
        plain, formatted=render_email(reports,reports,preview=True)
        self.assertIn("虚构",plain)
        self.assertIn("&lt;script&gt;",formatted)
        self.assertNotIn("<script>",formatted)
        self.assertIn("COST",formatted)
        with patch.dict(os.environ,{"QQ_EMAIL":"test@qq.com","QQ_AUTH_CODE":"test"}), patch.object(scanner.smtplib,"SMTP_SSL") as smtp:
            scanner.send_qq_email("测试",plain,formatted)
            smtp.assert_called_once_with("smtp.qq.com",465,timeout=20)
            raw=smtp.return_value.__enter__.return_value.sendmail.call_args.args[2]
        msg=message_from_string(raw)
        self.assertEqual(msg.get_content_type(),"multipart/alternative")
        self.assertEqual([p.get_content_type() for p in msg.get_payload()],["text/plain","text/html"])


if __name__ == "__main__":
    unittest.main()
