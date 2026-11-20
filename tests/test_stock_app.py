import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_local_report import snapshot
from stock_app import Dashboard, validate_settings, write_json


class DashboardTests(unittest.TestCase):
    def make_app(self, folder):
        directory = Path(folder)
        write_json(directory / "latest-data.json", snapshot())
        return Dashboard(directory)

    def test_failed_refresh_keeps_original_data_and_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            before = (app.directory / "latest-data.json").read_bytes()
            stamp = app.state()["fetched_at_utc"]
            with patch("stock_app.fetch_cloud_snapshot", side_effect=OSError("offline")):
                app._refresh({"source": "cloud", "proxy": ""})
            self.assertTrue(app.state()["ready"])
            self.assertTrue(app.state()["error"])
            self.assertEqual(app.state()["fetched_at_utc"], stamp)
            self.assertEqual((app.directory / "latest-data.json").read_bytes(), before)

    def test_auto_fallback_does_not_present_old_snapshot_as_new(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            with patch("stock_app.fetch_yahoo_snapshot", side_effect=OSError("limited")), patch("stock_app.fetch_cloud_snapshot", return_value=snapshot()):
                app._refresh({"source": "auto", "proxy": ""})
            self.assertEqual(app.state()["source_used"], "cloud")
            self.assertIn("暂无更新", app.state()["message"])
            self.assertIn("Yahoo", app.state()["message"])
            self.assertFalse(app.state()["error"])

    def test_repeated_clicks_do_not_start_parallel_downloads(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            app.settings["source"] = "cloud"
            gate = threading.Event()
            entered = threading.Event()
            def download(**kwargs):
                entered.set()
                gate.wait(3)
                return snapshot()
            with patch("stock_app.fetch_cloud_snapshot", side_effect=download) as fetch:
                self.assertTrue(app.start_refresh())
                self.assertTrue(entered.wait(2))
                self.assertFalse(app.start_refresh())
                gate.set()
                deadline = time.monotonic() + 3
                while app.state()["busy"] and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(app.state()["busy"])
                self.assertEqual(fetch.call_count, 1)

    def test_settings_are_validated_and_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            app = self.make_app(folder)
            app.update_settings({"source": "cloud", "proxy": "http://127.0.0.1:7890", "auto_refresh_minutes": 15})
            restored = Dashboard(folder)
            self.assertEqual(restored.settings["proxy"], "http://127.0.0.1:7890")
            self.assertEqual(restored.settings["auto_refresh_minutes"], 15)
        for value in ({"source": "unknown"}, {"proxy": "file:///secret"}, {"proxy": "http://user:secret@127.0.0.1:7890"}, {"auto_refresh_minutes": 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_settings(value)


if __name__ == "__main__":
    unittest.main()

