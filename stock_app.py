"""Portable Windows stock dashboard. Local-only UI, no credentials, mail or trading."""
import argparse
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import yfinance as yf
from local_stock_report import analyze_snapshot, fetch_cloud_snapshot, fetch_yahoo_snapshot, render_report
from domestic_data import fetch_sina_snapshot, fetch_eastmoney_snapshot, MarketDatabase
from scoring_core import WATCHLIST

VERSION = "1.1.1"
APP_ID = "local-stock-assistant"
ASSETS = Path(__file__).resolve().parent / "app_assets"
DEFAULT_SETTINGS = {"source": "auto", "proxy": "", "auto_refresh_minutes": 0, "refresh_on_start": True}


def write_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def validate_settings(value):
    result = {**DEFAULT_SETTINGS, **value}
    if result["source"] not in ("auto", "sina", "eastmoney", "cloud", "yahoo"):
        raise ValueError("请选择有效的数据来源。")
    proxy = str(result["proxy"]).strip()
    if proxy:
        parsed = urlsplit(proxy)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not parsed.port or parsed.username or parsed.password:
            raise ValueError("代理格式示例：http://127.0.0.1:7890，不要填写账号或密钥。")
    result["proxy"] = proxy
    if result["auto_refresh_minutes"] not in (0, 15, 30, 60):
        raise ValueError("自动更新间隔应为15、30、60分钟或关闭。")
    if not isinstance(result["refresh_on_start"], bool):
        raise ValueError("启动更新设置无效。")
    return {key: result[key] for key in DEFAULT_SETTINGS}


class Dashboard:
    def __init__(self, data_dir, seed=None):
        self.directory = Path(data_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = MarketDatabase(self.directory / "market-data.sqlite3")
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.settings = dict(DEFAULT_SETTINGS)
        self.snapshot = None
        self.analysis = None
        self.busy = False
        self.progress = 0
        self.message = "可直接查看缓存，或点击更新数据。"
        self.error = ""
        self.source_used = "cache"
        self.revision = 0
        self.last_attempt = None
        self.last_seen = time.monotonic()
        self.last_refresh = time.monotonic()
        settings_path = self.directory / "settings.json"
        if settings_path.exists():
            try:
                self.settings = validate_settings(json.loads(settings_path.read_text(encoding="utf-8")))
            except (ValueError, OSError, TypeError):
                self.error = "设置文件无法读取，已使用默认设置。"
        candidates = [self.directory / "latest-data.json"]
        if seed:
            candidates.append(Path(seed))
        for path in candidates:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                analysis = analyze_snapshot(data)
                if len(analysis["reports"]["closed"]) != len(WATCHLIST) or analysis["errors"]:
                    continue
                self.snapshot, self.analysis = data, analysis
                self.database.save(data)
                self.revision = 1
                break
            except (ValueError, OSError, TypeError, KeyError):
                continue

    def state(self):
        with self.lock:
            fetched = self.snapshot["fetched_at_utc"] if self.snapshot else None
            return {"app": APP_ID, "version": VERSION, "busy": self.busy, "progress": self.progress,
                    "message": self.message, "error": self.error, "ready": self.snapshot is not None,
                    "revision": self.revision, "settings": dict(self.settings), "fetched_at_utc": fetched,
                    "age_seconds": (datetime.now(timezone.utc) - self.analysis["fetched"]).total_seconds() if self.analysis else None,
                    "source_used": self.source_used, "last_attempt": self.last_attempt,
                    "provider_name": self.snapshot.get("source") if self.snapshot else None}

    def update_settings(self, value):
        settings = validate_settings(value)
        with self.lock:
            if self.busy:
                raise ValueError("更新过程中请稍后再修改设置。")
            write_json(self.directory / "settings.json", settings)
            self.settings = settings
        return settings

    def start_refresh(self):
        with self.lock:
            if self.busy:
                return False
            self.busy, self.progress, self.error = True, 2, ""
            self.message = "正在连接行情来源…"
            self.last_attempt = datetime.now(timezone.utc).isoformat()
            options = dict(self.settings)
        threading.Thread(target=self._refresh, args=(options,), daemon=True).start()
        return True

    def _progress(self, done, total, ticker):
        with self.lock:
            self.progress = 5 + int(done / total * 70)
            self.message = f"正在读取 {ticker}（{done + 1}/{total}）…"

    def _refresh(self, options):
        failures = []
        try:
            source, proxy = options["source"], options["proxy"] or None
            labels = {"sina": "新浪财经", "eastmoney": "东方财富", "yahoo": "Yahoo", "cloud": "云端快照"}
            chain = ("sina", "eastmoney", "yahoo", "cloud") if source == "auto" else (source,)
            candidate = analysis = used = None
            for provider in chain:
                with self.lock:
                    self.message = "正在读取" + labels[provider] + "行情…"
                try:
                    if provider == "sina":
                        selected = fetch_sina_snapshot(progress=self._progress)
                    elif provider == "eastmoney":
                        selected = fetch_eastmoney_snapshot(progress=self._progress)
                    elif provider == "yahoo":
                        selected = fetch_yahoo_snapshot(proxy=proxy, progress=self._progress, stop_on_error=True)
                    else:
                        selected = fetch_cloud_snapshot(proxy=proxy)
                    checked = analyze_snapshot(selected)
                    if checked["errors"] or len(checked["reports"]["closed"]) != len(WATCHLIST):
                        raise ValueError("行情未完整返回8只标的")
                    candidate, analysis, used = selected, checked, provider
                    break
                except Exception:
                    logging.exception("Provider %s failed", provider)
                    failures.append(labels[provider])
            if candidate is None:
                raise RuntimeError("、".join(failures) + "连接失败或行情不完整。已保留上次报告；可稍后重试或切换来源。")
            with self.lock:
                self.progress = 90
                self.message = "正在计算指标并保存…"
                previous_stamp = self.snapshot.get("fetched_at_utc") if self.snapshot else None
                write_json(self.directory / "latest-data.json", candidate)
                self.database.save(candidate)
                self.snapshot, self.analysis = candidate, analysis
                self.source_used = used
                self.revision += 1
                stamp = analysis["fetched"].astimezone(__import__('zoneinfo').ZoneInfo("Asia/Shanghai")).strftime("%m月%d日 %H:%M")
                if used == "cloud":
                    self.message = ("云端暂无更新，仍为" if previous_stamp == candidate["fetched_at_utc"] else "已导入云端快照，获取于") + stamp + "（北京时间）。"
                    if failures:
                        self.message = "、".join(failures) + "暂不可用；" + self.message
                else:
                    self.message = "已重新下载行情并计算评分，获取于" + stamp + "（北京时间）。"
                self.progress = 100
        except Exception as exc:
            logging.exception("Refresh failed")
            with self.lock:
                self.error = str(exc) if isinstance(exc, RuntimeError) else "网络连接失败。请检查网络／代理，或稍后重试。"
                self.message = "更新未完成，保留上次正常报告及原始时间。"
        finally:
            with self.lock:
                self.busy = False
                self.last_refresh = time.monotonic()

    def report_html(self, history_id=None):
        with self.lock:
            selected = self.database.snapshot(history_id) if history_id else self.snapshot
            if not selected:
                return '<html lang="zh-CN"><meta charset="utf-8"><body style="font:16px system-ui;padding:40px;color:#52647a">暂无本地行情，请点击上方“更新数据”。</body></html>'
            analysis = analyze_snapshot(selected) if history_id else self.analysis
            html = render_report(selected, analysis, "/api/export" + ("?history=" + str(int(history_id)) if history_id else ""))
        html = html.replace("<style>", "<style>header{display:none!important}main{max-width:1200px!important;padding:22px!important}", 1)
        return html.replace('此文件是本地快照；按“刷新评分报告.cmd”重新导入更新。', '使用应用顶部“更新数据”按钮更新；请始终核对行情获取时间。')


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, dashboard):
        self.dashboard = dashboard
        super().__init__(address, Handler)
        self.origin = f"http://127.0.0.1:{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logging.info(fmt, *args)

    def respond(self, status, body, content_type="application/json; charset=utf-8", attachment=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        if attachment:
            self.send_header("Content-Disposition", 'attachment; filename="stock-data.json"')
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        if self.headers.get("Host") != urlsplit(self.server.origin).netloc:
            self.respond(403, {"error": "Invalid local host"})
            return False
        return True

    def do_GET(self):
        if not self.host_ok():
            return
        route = urlsplit(self.path).path
        app = self.server.dashboard
        if route == "/api/health":
            self.respond(200, {"app": APP_ID, "version": VERSION})
        elif route == "/api/state":
            app.last_seen = time.monotonic()
            self.respond(200, app.state())
        elif route == "/api/export":
            if not app.snapshot:
                self.respond(404, {"error": "暂无数据"})
            else:
                with app.lock:
                    query = parse_qs(urlsplit(self.path).query)
                    try:
                        selected = app.database.snapshot(query["history"][0]) if query.get("history") else app.snapshot
                        self.respond(200, selected, attachment=True)
                    except ValueError:
                        self.respond(404, {"error": "历史记录不存在"})
        elif route == "/api/history":
            self.respond(200, app.database.history())
        elif route == "/report":
            query = parse_qs(urlsplit(self.path).query)
            try:
                self.respond(200, app.report_html(query.get("history", [None])[0]), "text/html; charset=utf-8")
            except (ValueError, TypeError):
                self.respond(404, {"error": "历史记录不存在"})
        elif route == "/":
            html = (ASSETS / "index.html").read_text(encoding="utf-8").replace("__APP_TOKEN__", app.token)
            self.respond(200, html, "text/html; charset=utf-8")
        elif route in ("/app.js", "/app.css", "/icon.svg"):
            types = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
            path = ASSETS / route[1:]
            self.respond(200, path.read_bytes(), types[path.suffix])
        else:
            self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if not self.host_ok():
            return
        app = self.server.dashboard
        if self.headers.get("X-App-Token") != app.token or self.headers.get("Origin", self.server.origin) != self.server.origin:
            self.respond(403, {"error": "请从本地应用窗口执行此操作。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 4096:
                raise ValueError("请求过大。")
            body = json.loads(self.rfile.read(length) or b"{}")
            route = urlsplit(self.path).path
            if route == "/api/refresh":
                self.respond(202 if app.start_refresh() else 200, app.state())
            elif route == "/api/settings":
                self.respond(200, app.update_settings(body))
            elif route == "/api/shutdown":
                self.respond(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.respond(404, {"error": "Not found"})
        except (ValueError, TypeError, OSError) as exc:
            self.respond(400, {"error": str(exc)})


def open_app_window(url, profile):
    # Edge app mode provides a separate application window without browser tabs.
    candidates = [Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
                  Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe"]
    for executable in candidates:
        if executable.exists():
            subprocess.Popen([str(executable), "--app=" + url, "--user-data-dir=" + str(profile), "--no-first-run", "--window-size=1380,940"])
            return
    webbrowser.open(url)


def main(argv=None):
    parser = argparse.ArgumentParser(description="美股评分助手")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-start-refresh", action="store_true")
    args = parser.parse_args(argv)
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent / "outputs" / "美股评分助手"
    directory = args.data_dir or base / "数据"
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=directory / "app.log", level=logging.INFO, encoding="utf-8", format="%(asctime)s %(levelname)s %(message)s")
    if sys.stdout is None:
        sys.stdout = open(directory / "console.log", "a", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout
    yf.set_tz_cache_location(str(directory / "行情缓存"))
    seed = ASSETS / "seed.json"
    if not seed.exists():
        seed = Path(__file__).resolve().parent / "outputs" / "stock-report-latest.json"
    app = Dashboard(directory, seed)
    try:
        server = AppServer(("127.0.0.1", args.port), app)
    except OSError:
        url = f"http://127.0.0.1:{args.port}"
        with urllib.request.urlopen(url + "/api/health", timeout=3) as response:
            if json.load(response).get("app") != APP_ID:
                raise RuntimeError("应用端口被其他程序占用。")
        if not args.no_browser:
            open_app_window(url, directory / "窗口配置")
        return
    write_json(directory / "running.json", {"pid": os.getpid(), "url": server.origin, "version": VERSION})
    if not args.no_browser:
        open_app_window(server.origin, directory / "窗口配置")
    if app.settings["refresh_on_start"] and not args.no_start_refresh:
        app.start_refresh()

    def watchdog():
        while True:
            time.sleep(2)
            state = app.state()
            if time.monotonic() - app.last_seen > 180 and not state["busy"]:
                server.shutdown()
                break
            minutes = state["settings"]["auto_refresh_minutes"]
            if minutes and not state["busy"] and time.monotonic() - app.last_refresh > minutes * 60:
                app.start_refresh()

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        (directory / "running.json").unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Application startup failed")
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "应用启动失败。请查看应用旁“数据”文件夹内的app.log。", "美股评分助手", 0x10)
        raise

