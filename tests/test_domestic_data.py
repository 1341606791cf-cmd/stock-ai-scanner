import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, date
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

from domestic_data import EASTMONEY_IDS, MarketDatabase, parse_eastmoney_response, apply_sina_adjustment
from scoring_core import WATCHLIST, score_stock
from stock_app import Dashboard
from test_local_report import snapshot
from test_scoring import frame


class DomesticDataTests(unittest.TestCase):
    def test_eastmoney_field_order_timezone_and_identity(self):
        import pandas as pd
        raw = {"rc": 0, "data": {"code": "LLY", "klines": [f"{d.date()},100,102,103,99,1234,0,0,0,0,0" for d in pd.date_range("2026-10-01", "2026-11-20")]}}
        bars = parse_eastmoney_response(raw, "LLY", date(2026, 10, 1), datetime(2026, 11, 20, 18, tzinfo=timezone.utc))
        self.assertEqual(bars[-1]["Close"], 102)
        self.assertEqual(bars[-1]["High"], 103)
        self.assertTrue(bars[0]["Date"].endswith("-04:00"))
        self.assertTrue(bars[-1]["Date"].endswith("-05:00"))
        with self.assertRaises(ValueError):
            parse_eastmoney_response(raw, "NVDA", date(2026, 10, 1), datetime.now(timezone.utc))
        self.assertEqual(EASTMONEY_IDS["LLY"], "106.LLY")

    def test_sina_qfq_uses_factor_and_cash_and_preserves_trading_date(self):
        import pandas as pd
        rows = [{"date": str(d.date()) + "T00:00:00Z", "open": 100, "high": 110, "low": 90, "close": 104, "volume": 12} for d in pd.date_range("2026-10-01", "2026-11-19")]
        bars = apply_sina_adjustment(rows, [{"d": "2026-01-01", "f": 0.5, "c": -1}, {"d": "2026-11-01", "f": 1, "c": 0}], date(2026, 10, 1), datetime(2026, 11, 20, tzinfo=timezone.utc))
        self.assertEqual(bars[0]["Close"], 51)
        self.assertEqual(bars[-1]["Close"], 104)
        self.assertTrue(bars[0]["Date"].startswith("2026-10-01T00:00:00"))
        self.assertEqual(bars[-1]["Volume"], 12)

    def test_sina_is_first_automatic_source(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Dashboard(folder)
            data = snapshot()
            data["source"] = "新浪财经"
            with patch("stock_app.fetch_sina_snapshot", return_value=data), patch("stock_app.fetch_eastmoney_snapshot") as eastmoney:
                app._refresh({"source": "auto", "proxy": ""})
            eastmoney.assert_not_called()
            self.assertEqual(app.state()["source_used"], "sina")
            self.assertFalse(app.state()["error"])

    def test_lilly_is_an_individual_stock(self):
        self.assertIn("LLY", WATCHLIST)
        result = score_stock("LLY", frame(close=101.25, rsi=34))
        self.assertEqual([p["score"] for p in result["parts"]], [25, 25, 20])
        self.assertEqual(result["total"], 70)

    def test_sqlite_archives_exact_snapshot_without_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            db = MarketDatabase(Path(folder) / "market.sqlite3")
            data = snapshot()
            db.save(data)
            db.save(data)
            records = db.history()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["symbols"], 8)
            self.assertEqual(db.snapshot(records[0]["id"]), data)
            with closing(sqlite3.connect(db.path)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM daily_bars WHERE ticker='LLY'").fetchone()[0], 40)

    def test_domestic_source_is_tried_before_overseas_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Dashboard(folder)
            data = snapshot()
            data["source"] = "东方财富"
            data["adjustment"] = "前复权"
            with patch("stock_app.fetch_sina_snapshot", side_effect=OSError("offline")), patch("stock_app.fetch_eastmoney_snapshot", return_value=data), patch("stock_app.fetch_yahoo_snapshot") as yahoo, patch("stock_app.fetch_cloud_snapshot") as cloud:
                app._refresh({"source": "auto", "proxy": ""})
            yahoo.assert_not_called()
            cloud.assert_not_called()
            self.assertFalse(app.state()["error"])
            self.assertEqual(app.state()["source_used"], "eastmoney")
            self.assertEqual(len(app.database.history()), 1)
            self.assertIn("东方财富", app.report_html())

    def test_seven_stock_snapshot_is_not_presented_as_eight(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Dashboard(folder)
            data = snapshot()
            del data["symbols"]["LLY"]
            with patch("stock_app.fetch_cloud_snapshot", return_value=data):
                app._refresh({"source": "cloud", "proxy": ""})
            self.assertFalse(app.state()["ready"])
            self.assertTrue(app.state()["error"])


if __name__ == "__main__":
    unittest.main()

