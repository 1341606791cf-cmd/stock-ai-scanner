"""Mainland-hosted Eastmoney US daily bars and a local SQLite archive."""
import json
import ast
import math
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from scoring_core import WATCHLIST, NEW_YORK

EASTMONEY_IDS = {ticker: ("106." if ticker == "LLY" else "105.") + ticker for ticker in WATCHLIST}


def parse_eastmoney_response(payload, ticker, start_date, now):
    data = payload.get("data") or {}
    if payload.get("rc") != 0 or data.get("code", "").upper() != ticker:
        raise ValueError("东方财富返回的证券代码不匹配")
    bars = []
    for text in data.get("klines", []):
        parts = text.split(",")
        if len(parts) < 6:
            raise ValueError("日线字段不完整")
        day = pd.Timestamp(parts[0]).date()
        if day < start_date or day > now.astimezone(NEW_YORK).date():
            continue
        values = [float(v) for v in parts[1:6]]
        if not all(math.isfinite(v) for v in values) or min(values[:4]) <= 0 or values[4] < 0:
            raise ValueError("日线价格或成交量无效")
        open_, close, high, low, volume = values
        if high < max(open_, close, low) or low > min(open_, close, high):
            raise ValueError("日线高低价不一致")
        bars.append({"Date": pd.Timestamp(day).tz_localize(NEW_YORK).isoformat(),
                     "Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume})
    bars.sort(key=lambda row: row["Date"])
    if len(bars) < 30:
        raise ValueError("东方财富有效日线不足30根")
    if (now.astimezone(NEW_YORK).date() - pd.Timestamp(bars[-1]["Date"]).date()).days > 7:
        raise ValueError("东方财富最新交易日过旧")
    return bars


def fetch_eastmoney_snapshot(progress=None):
    # Public US-stock kline route; fqt=1 requests Eastmoney forward-adjusted OHLC.
    # Domestic source connects directly rather than depending on an overseas proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    result = {"schema_version": 1, "source": "东方财富", "provider": "eastmoney", "adjusted": True,
              "adjustment": "东方财富前复权（qfq）", "period": "3mo", "interval": "1d", "symbols": {}, "errors": []}
    for position, ticker in enumerate(WATCHLIST):
        if progress:
            progress(position, len(WATCHLIST), ticker)
        now = datetime.now(timezone.utc)
        start = (pd.Timestamp(now.astimezone(NEW_YORK).date()) - pd.DateOffset(months=3)).date()
        params = {"secid": EASTMONEY_IDS[ticker], "klt": "101", "fqt": "1", "end": "20500000", "lmt": "100",
                  "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"}
        # Retry a dropped connection once through the provider's other public host.
        # Explicit HTTP denials/rate limits are not bypassed or repeatedly retried.
        payload = None
        for attempt, host in enumerate(("push2his.eastmoney.com", "63.push2his.eastmoney.com")):
            request = urllib.request.Request("https://" + host + "/api/qt/stock/kline/get?" + urllib.parse.urlencode(params),
                                             headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
            try:
                with opener.open(request, timeout=12) as response:
                    payload = json.load(response)
                break
            except urllib.error.HTTPError:
                raise
            except (OSError, TimeoutError):
                if attempt:
                    raise
                time.sleep(1)
        bars = parse_eastmoney_response(payload, ticker, start, now)
        result["symbols"][ticker] = {"fetched_at_utc": now.isoformat(), "provider": "eastmoney",
                                      "security_id": EASTMONEY_IDS[ticker], "bars": bars}
    result["fetched_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def apply_sina_adjustment(rows, factors, start_date, now):
    factors = sorted((pd.Timestamp(v["d"]).date(), float(v["f"]), float(v["c"])) for v in factors)
    if not factors or not all(math.isfinite(f) and f > 0 and math.isfinite(c) for _, f, c in factors):
        raise ValueError("新浪前复权因子无效")
    normalized = []
    for row in rows:
        day = pd.Timestamp(row["date"]).date()
        if day < start_date or day > now.astimezone(NEW_YORK).date():
            continue
        applicable = [v for v in factors if v[0] <= day]
        _, factor, cash = applicable[-1] if applicable else factors[0]
        bar = {"Date": pd.Timestamp(day).tz_localize(NEW_YORK).isoformat()}
        for key in ("open", "high", "low", "close"):
            value = round(float(row[key]) * factor + cash, 4)
            if not math.isfinite(value) or value <= 0:
                raise ValueError("新浪复权价格无效")
            bar[key.title()] = value
        volume = float(row["volume"])
        if not math.isfinite(volume) or volume < 0:
            raise ValueError("新浪成交量无效")
        bar["Volume"] = volume
        normalized.append(bar)
    normalized.sort(key=lambda v: v["Date"])
    if len(normalized) < 30 or (now.astimezone(NEW_YORK).date() - pd.Timestamp(normalized[-1]["Date"]).date()).days > 7:
        raise ValueError("新浪日线不足或过旧")
    return normalized


def fetch_sina_snapshot(progress=None):
    # The bundled AKShare decoder is MIT-licensed; it decodes data, not website code.
    # Never evaluate the downloaded assignment as JavaScript or Python code.
    from py_mini_racer import MiniRacer
    decoder = (Path(__file__).resolve().parent / "app_assets" / "sina_decoder.js").read_text(encoding="utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def read(url):
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
        with opener.open(request, timeout=12) as response:
            return response.read().decode("utf-8")
    result = {"schema_version": 1, "source": "新浪财经", "provider": "sina", "adjusted": True,
              "adjustment": "新浪前复权（qfq）", "period": "3mo", "interval": "1d", "symbols": {}, "errors": []}
    for position, ticker in enumerate(WATCHLIST):
        if progress:
            progress(position, len(WATCHLIST), ticker)
        now = datetime.now(timezone.utc)
        start = (pd.Timestamp(now.astimezone(NEW_YORK).date()) - pd.DateOffset(months=3)).date()
        raw = read("https://finance.sina.com.cn/staticdata/us/" + ticker)
        encoded = json.loads(raw.partition("=")[2].partition(";")[0].strip())
        context = MiniRacer()
        context.eval(decoder)
        rows = context.call("d", encoded, timeout=3000)
        del context
        raw_factors = read("https://finance.sina.com.cn/us_stock/company/reinstatement/" + ticker + "_qfq.js")
        value = raw_factors.partition("=")[2].strip().rstrip(";")
        try:
            factors = json.JSONDecoder().raw_decode(value.lstrip())[0]["data"]
        except json.JSONDecodeError:
            factors = ast.literal_eval(value.splitlines()[0])["data"]
        bars = apply_sina_adjustment(rows, factors, start, now)
        result["symbols"][ticker] = {"fetched_at_utc": now.isoformat(), "provider": "sina", "bars": bars}
    result["fetched_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


class MarketDatabase:
    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, source TEXT NOT NULL,
                adjustment TEXT NOT NULL, symbols INTEGER NOT NULL, last_date TEXT NOT NULL,
                payload TEXT NOT NULL, UNIQUE(fetched_at,source));
                CREATE TABLE IF NOT EXISTS daily_bars (
                source TEXT NOT NULL, ticker TEXT NOT NULL, trade_date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume REAL, fetched_at TEXT,
                PRIMARY KEY(source,ticker,trade_date));''')

    def save(self, snapshot):
        payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        source, stamp = snapshot["source"], snapshot["fetched_at_utc"]
        last_date = max(row["Date"][:10] for entry in snapshot["symbols"].values() for row in entry["bars"])
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO snapshots(fetched_at,source,adjustment,symbols,last_date,payload) VALUES(?,?,?,?,?,?)",
                       (stamp, source, snapshot.get("adjustment", "Yahoo自动复权"), len(snapshot["symbols"]), last_date, payload))
            rows = [(source, ticker, row["Date"][:10], row["Open"], row["High"], row["Low"], row["Close"], row["Volume"], stamp)
                    for ticker, entry in snapshot["symbols"].items() for row in entry["bars"]]
            db.executemany("INSERT OR REPLACE INTO daily_bars VALUES(?,?,?,?,?,?,?,?,?)", rows)

    def history(self):
        with self.connection() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT id,fetched_at,source,adjustment,symbols,last_date FROM snapshots ORDER BY fetched_at DESC LIMIT 30")]

    def snapshot(self, identity):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM snapshots WHERE id=?", (int(identity),)).fetchone()
        if not row:
            raise ValueError("历史记录不存在")
        return json.loads(row[0])

