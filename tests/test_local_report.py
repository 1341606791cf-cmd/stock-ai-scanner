import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import local_stock_report as local
import stock_ai_scanner as scanner


def snapshot():
    bars = [{"Date": d.isoformat(), "Open": 100, "High": 101, "Low": 99,
             "Close": 100 if i < 39 else 50, "Volume": 1000}
            for i, d in enumerate(pd.date_range(end="2026-11-20", periods=40, tz=scanner.NEW_YORK))]
    stamp = "2026-11-20T16:46:00+00:00"
    return {"schema_version": 1, "adjusted": True, "source": "test fixture", "fetched_at_utc": stamp,
            "symbols": {ticker: {"fetched_at_utc": stamp, "bars": bars} for ticker in scanner.WATCHLIST}, "errors": []}


class LocalReportTests(unittest.TestCase):
    def test_closed_score_excludes_current_bar_across_dst(self):
        data = local.analyze_snapshot(snapshot())
        self.assertEqual(data["errors"], [])
        self.assertEqual(len(data["reports"]["closed"]), len(scanner.WATCHLIST))
        closed = data["reports"]["closed"][0]
        intraday = data["reports"]["intraday"][0]
        self.assertEqual(closed["date"], "2026-11-19")
        self.assertEqual(closed["close"], 100)
        self.assertEqual(intraday["date"], "2026-11-20")
        self.assertEqual(intraday["close"], 50)
        self.assertTrue(intraday["unconfirmed"])
        self.assertNotEqual(closed["total"], intraday["total"])

    def test_offline_report_requires_no_keys_and_sends_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.json"
            report = Path(folder) / "report.html"
            source.write_text(json.dumps(snapshot()), encoding="utf-8")
            with patch.object(scanner, "send_qq_email", side_effect=AssertionError("email forbidden")) as send, patch.object(scanner, "ask_ai_analyst", side_effect=AssertionError("AI forbidden")) as ai:
                local.main(["--input-json", str(source), "--output", str(report), "--no-open"])
                send.assert_not_called()
                ai.assert_not_called()
            html = report.read_text(encoding="utf-8")
            self.assertIn("盘中试算", html)
            self.assertIn("已收盘评分", html)
            self.assertIn("2026-11-19", html)
            self.assertIn("BOLL", html)
            self.assertTrue(report.with_suffix(".json").exists())

    def test_failed_data_does_not_replace_last_good_report(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "report.html"
            report.write_text("last good report", encoding="utf-8")
            data = snapshot()
            data["symbols"] = {}
            with self.assertRaises(RuntimeError):
                local.save_report(data, report)
            self.assertEqual(report.read_text(encoding="utf-8"), "last good report")

    def test_error_text_is_escaped(self):
        data = snapshot()
        data["errors"] = ["<script>do_not_execute()</script>"]
        html = local.render_report(data, local.analyze_snapshot(data), "raw.json")
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>do_not_execute", html)

    def test_scanner_command_delegates_to_local_report(self):
        with patch.object(local, "main") as report:
            scanner.main()
            report.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

