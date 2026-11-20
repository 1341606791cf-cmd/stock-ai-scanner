"""Fetch public market data and create a self-contained local report. No email or AI."""

import argparse
import json
import math
import os
import urllib.request
import webbrowser
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from scoring_core import WATCHLIST, NEW_YORK, calculate_indicators, completed_daily_bars, score_stock
from email_reports import NAMES

ROOT = Path(__file__).resolve().parent
BEIJING = ZoneInfo("Asia/Shanghai")
SNAPSHOT_URL = "https://raw.githubusercontent.com/1341606791cf-cmd/stock-ai-scanner/main/reports/latest-data.json"


def fetch_yahoo_snapshot(proxy=None, progress=None, stop_on_error=False):
    yf.set_config(proxy=proxy or None)
    result = {"schema_version": 1, "source": "Yahoo Finance / yfinance", "adjusted": True,
              "period": "3mo", "interval": "1d", "symbols": {}, "errors": []}
    for position, ticker in enumerate(WATCHLIST):
        if progress:
            progress(position, len(WATCHLIST), ticker)
        started = datetime.now(timezone.utc).isoformat()
        try:
            df = yf.Ticker(ticker).history(period="3mo", interval="1d", auto_adjust=True, timeout=12)
            if df.empty or len(df) < 30:
                raise RuntimeError("日线不足30根")
            bars = []
            for date, row in df.iterrows():
                bar = {"Date": pd.Timestamp(date).isoformat()}
                for column in ("Open", "High", "Low", "Close", "Volume"):
                    value = float(row[column])
                    if not math.isfinite(value):
                        raise RuntimeError("行情包含非有限数值")
                    bar[column] = value
                bars.append(bar)
            result["symbols"][ticker] = {"fetched_at_utc": started, "bars": bars}
            print(f"{ticker}: downloaded {len(bars)} daily bars", flush=True)
        except Exception as exc:
            result["errors"].append(f"{ticker}: {type(exc).__name__}")
            if stop_on_error:
                break
    result["fetched_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def fetch_cloud_snapshot(proxy=None):
    # Public, non-secret market snapshot. No API key, email account or model call.
    request = urllib.request.Request(SNAPSHOT_URL + "?t=" + str(int(datetime.now().timestamp())),
                                     headers={"User-Agent": "LocalStockReport/1.0", "Cache-Control": "no-cache"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy})) if proxy else urllib.request.build_opener()
    with opener.open(request, timeout=25) as response:
        return json.load(response)


def analyze_snapshot(snapshot):
    if snapshot.get("schema_version") != 1 or snapshot.get("adjusted") is not True:
        raise RuntimeError("不支持的行情文件格式，需使用复权日线快照。")
    fetched = datetime.fromisoformat(snapshot["fetched_at_utc"])
    if fetched.tzinfo is None:
        raise RuntimeError("行情获取时间缺少时区。")
    reports = {"closed": [], "intraday": []}
    errors = list(snapshot.get("errors", []))
    for ticker in WATCHLIST:
        try:
            entry = snapshot["symbols"][ticker]
            bars = entry["bars"]
            if len(bars) < 30:
                raise RuntimeError("日线不足30根")
            # ISO timestamps may span DST: parse in UTC before converting to New York.
            df = pd.DataFrame(bars)
            df.index = pd.to_datetime(df.pop("Date"), utc=True).dt.tz_convert(NEW_YORK)
            df = df.sort_index()
            if df.index.has_duplicates:
                raise RuntimeError("日线日期重复")
            for column in ("Open", "High", "Low", "Close", "Volume"):
                df[column] = pd.to_numeric(df[column], errors="raise")
                if not all(math.isfinite(float(v)) for v in df[column]):
                    raise RuntimeError("行情包含非有限数值")
            symbol_time = datetime.fromisoformat(entry["fetched_at_utc"])
            closed = completed_daily_bars(df, symbol_time)
            for mode, data in [("closed", closed), ("intraday", df)]:
                if len(data) < 30:
                    raise RuntimeError("已收盘日线不足30根")
                indicators = calculate_indicators(data)
                report = score_stock(ticker, indicators)
                report["history"] = [{"date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                                      **{column: float(row[column]) for column in
                                         ("Close", "RSI", "BOLL_UPPER", "BOLL_MID", "BOLL_LOWER", "MACD_DIF", "MACD_DEA", "MACD_HIST")}}
                                     for date, row in indicators.tail(3).iterrows()]
                report["start_date"] = indicators.index[0].strftime("%Y-%m-%d")
                report["bar_count"] = len(indicators)
                report["unconfirmed"] = mode == "intraday" and len(df) != len(closed)
                reports[mode].append(report)
        except Exception as exc:
            errors.append(f"{ticker}: 评分失败（{type(exc).__name__}）")
    return {"fetched": fetched, "reports": reports, "errors": sorted(set(errors))}


def score_reason(report):
    latest = report["history"][-1]
    lower, mid, close, rsi = latest["BOLL_LOWER"], latest["BOLL_MID"], latest["Close"], latest["RSI"]
    band = 1.01 if report["ticker"] == "QQQ" else 1.015
    points = report["parts"]
    boll = (f"收盘 {close:.2f}；下轨 {lower:.2f}；接近下轨上限 {lower * band:.2f}；中轨 {mid:.2f}。"
            f"{points[0]['reason']}，计 {points[0]['score']}/40。")
    thresholds = "32 / 38 / 43" if report["ticker"] == "QQQ" else "30 / 35 / 40"
    rsi_text = f"RSI {rsi:.4f}；本档分界 {thresholds}。{points[1]['reason']}，计 {points[1]['score']}/35。"
    histogram = " → ".join(f"{r['MACD_HIST']:.4f}" for r in report["history"])
    macd = (f"最近三根柱值 {histogram}；DIF {latest['MACD_DIF']:.4f}，DEA {latest['MACD_DEA']:.4f}。"
            f"{points[2]['reason']}，计 {points[2]['score']}/25。")
    return [boll, rsi_text, macd]


def render_report(snapshot, analysis, data_filename):
    fetched = analysis["fetched"]
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    stamp = fetched.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    ny_stamp = fetched.astimezone(NEW_YORK).strftime("%Y-%m-%d %H:%M:%S")
    warnings = list(analysis["errors"])
    if age > 26 * 3600:
        warnings.insert(0, "此行情快照已超过26小时；请核对日期，不要将它视为最新行情。")
    if not any(r["unconfirmed"] for r in analysis["reports"]["intraday"]):
        warnings.append("本次没有未收盘日线，两个视图使用相同的最近完整日线。")
    warning_html = "".join(f'<div class="notice">{escape(str(w))}</div>' for w in warnings)
    panels = []
    for mode in ("closed", "intraday"):
        rows = sorted(analysis["reports"][mode], key=lambda x: x["total"], reverse=True)
        highest = max((r["total"] for r in rows), default=0)
        qualifying = sum(r["eligible"] for r in rows)
        dates = "、".join(sorted({r["date"] for r in rows})) or "无法取得"
        unconfirmed = mode == "intraday" and any(r["unconfirmed"] for r in rows)
        status = f"{qualifying} 只达到70分及以上" if qualifying else "无70分及以上标的"
        status = ("盘中试算：" if unconfirmed else "已收盘评分：") + status
        table_rows, cards = [], []
        for i, r in enumerate(rows):
            ticker = r["ticker"]
            tone = "good" if r["eligible"] else "muted"
            table_rows.append(f'''<tr class="filter-row" data-symbol="{ticker}" data-score="{r['total']}"><td><a href="#{mode}-{ticker}">{ticker}</a><small>{escape(NAMES[ticker])}</small></td><td>${r['close']:,.2f}</td><td>{r['parts'][0]['score']}<small>/40</small></td><td>{r['parts'][1]['score']}<small>/35</small></td><td>{r['parts'][2]['score']}<small>/25</small></td><td><strong class="{tone}">{r['total']}</strong><small>/100</small></td><td><span class="pill {tone}">{escape(r['tier'])}</span></td></tr>''')
            parts = "".join(f'<div class="reason"><span>{escape(p["name"])}</span><p>{escape(text)}</p></div>' for p, text in zip(r["parts"], score_reason(r)))
            history = "".join(f'<tr><td>{v["date"]}</td><td>{v["Close"]:.2f}</td><td>{v["RSI"]:.4f}</td><td>{v["MACD_DIF"]:.4f}</td><td>{v["MACD_DEA"]:.4f}</td><td>{v["MACD_HIST"]:.4f}</td></tr>' for v in r["history"])
            cards.append(f'''<details id="{mode}-{ticker}" class="detail filter-row" data-symbol="{ticker}" data-score="{r['total']}" {'open' if i == 0 else ''}><summary><span><b>{ticker}</b> {escape(NAMES[ticker])}</span><span>{r['total']} / 100 · 查看判断过程</span></summary><div class="detail-body">{parts}<div class="sum">{r['parts'][0]['score']} + {r['parts'][1]['score']} + {r['parts'][2]['score']} = <b>{r['total']} 分</b></div><div class="scroll"><table class="history"><thead><tr><th>交易日（纽约）</th><th>复权收盘</th><th>RSI</th><th>DIF</th><th>DEA</th><th>MACD柱</th></tr></thead><tbody>{history}</tbody></table></div><p class="fine">计算窗口：{r['start_date']} 至 {r['date']}，{r['bar_count']} 根日线。每项只取最高命中档，不重复加分。</p></div></details>''')
        panels.append(f'''<section class="panel" id="panel-{mode}" {'hidden' if mode == 'intraday' else ''}><div class="stats"><div><small>成功评分</small><b>{len(rows)}<em> / {len(WATCHLIST)}</em></b></div><div><small>达到70分</small><b>{qualifying}<em> 只</em></b></div><div><small>本次最高</small><b>{highest}<em> / 100</em></b></div><div><small>纽约交易日</small><b class="date">{dates}</b></div></div><div class="status {'live' if unconfirmed else ''}">{status}<span>{'尚未收盘，分数和金叉信号都可能变化；不是正式收盘信号。' if unconfirmed else '使用最近已收盘的日线；这是规则匹配分，不是上涨概率。'}</span></div><div class="scroll table-card"><table><thead><tr><th>标的</th><th>{'盘中参考价' if unconfirmed else '复权收盘'}</th><th>BOLL</th><th>RSI</th><th>MACD</th><th>总分</th><th>规则分层</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table></div><p class="empty" hidden>当前筛选下没有标的。</p><h2>把数值代入，看评分过程</h2>{''.join(cards)}</section>''')
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>美股评分 · 本地报告</title><style>
:root{color-scheme:light;--ink:#142a43;--muted:#6e7f92;--line:#dce5ed;--green:#087b67}*{box-sizing:border-box}body{margin:0;background:#f1f5f8;color:var(--ink);font-family:system-ui,"Microsoft YaHei",sans-serif}header{background:#12293f;color:white;padding:36px max(24px,calc((100vw - 1120px)/2));border-bottom:4px solid #6ed2b5}.eyebrow{font-size:11px;letter-spacing:3px;color:#a4c9d5}h1{font-size:30px;margin:12px 0}header p{font-size:13px;line-height:1.9;color:#c2d2de;margin:0}main{max-width:1168px;margin:auto;padding:24px}.toolbar{display:flex;gap:12px;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap}.tabs{display:flex;border:1px solid var(--line);background:white;border-radius:10px;padding:4px;gap:4px}button{border:0;background:transparent;padding:11px 16px;border-radius:7px;font:inherit;color:var(--muted);cursor:pointer}button[aria-selected=true]{background:var(--ink);color:white}.filters{display:flex;gap:12px;align-items:center;font-size:13px}input[type=search]{padding:11px;border:1px solid var(--line);border-radius:8px;width:155px;font:inherit}.stats{display:grid;grid-template-columns:repeat(3,1fr) 1.5fr;gap:14px;margin-bottom:18px}.stats>div{background:white;border:1px solid var(--line);border-radius:12px;padding:20px}.stats small{display:block;color:var(--muted);font-size:12px}.stats b{display:block;font-size:30px;margin-top:9px}.stats em{font-size:13px;color:var(--muted);font-style:normal;font-weight:normal}.stats .date{font-size:20px;margin-top:15px}.status{border-left:4px solid var(--green);background:#e2f1ec;padding:16px 18px;font-size:16px;font-weight:650;border-radius:4px;margin-bottom:18px}.status span{display:block;font-size:12px;font-weight:normal;color:#4c6c65;margin-top:6px;line-height:1.7}.status.live{background:#fff2d8;border-color:#c58728}.status.live span{color:#876a36}.scroll{overflow-x:auto}.table-card{border:1px solid var(--line);border-radius:12px;background:white}table{border-collapse:collapse;width:100%;min-width:700px;text-align:left;font-variant-numeric:tabular-nums}th{font-size:12px;font-weight:500;color:var(--muted);padding:15px 18px;background:#f8fafc}td{padding:17px 18px;border-top:1px solid #eaf0f5;font-size:14px}td small{display:block;font-size:11px;color:var(--muted);margin-top:3px}td a{font-weight:bold;color:var(--ink);text-decoration:none}td strong{font-size:25px}.good{color:var(--green)}.muted{color:#677a8c}.pill{font-size:12px;padding:5px 9px;background:#edf2f6;border-radius:5px;white-space:nowrap}.pill.good{background:#e2f1ec}h2{font-size:17px;margin:28px 0 14px}.detail{border:1px solid var(--line);border-radius:10px;background:white;margin:12px 0;scroll-margin-top:20px}.detail summary{padding:19px;display:flex;justify-content:space-between;gap:14px;cursor:pointer;font-size:13px}.detail summary b{font-size:17px;margin-right:8px}.detail summary>span:last-child{color:var(--muted)}.detail-body{padding:0 20px 20px}.reason{display:grid;grid-template-columns:110px 1fr;border-top:1px solid #edf1f5;padding:14px 0;font-size:13px;line-height:1.9}.reason span{color:var(--green);font-weight:650}.reason p{margin:0}.sum{padding:12px;background:#f1f6f9;border-radius:7px;font-size:14px;margin-bottom:15px}.history{min-width:690px}.history td,.history th{font-size:12px;padding:10px}.fine{font-size:12px;line-height:1.9;color:var(--muted)}.notice{background:#fff1da;border:1px solid #f3d7a7;border-radius:8px;padding:13px;margin-bottom:14px;font-size:13px;line-height:1.8}footer{border-top:1px solid var(--line);margin-top:28px;padding:20px 0;font-size:12px;line-height:2;color:var(--muted)}footer a{color:var(--green)}[hidden]{display:none!important}.empty{color:var(--muted);padding:20px;background:white;border-radius:10px}@media(max-width:700px){header{padding:25px 18px}h1{font-size:24px}main{padding:16px}.stats{grid-template-columns:1fr 1fr;gap:10px}.stats>div{padding:15px}.stats b{font-size:26px}.stats .date{font-size:16px}.toolbar{gap:15px}.filters{width:100%;justify-content:space-between}.tabs{width:100%}.tabs button{flex:1;padding:10px}.reason{grid-template-columns:1fr;gap:5px}.detail summary{flex-wrap:wrap}.detail-body{padding:0 15px 15px}}
</style></head><body><header><div class="eyebrow">US EQUITY / LOCAL RESEARCH</div><h1>美股评分 · 本地报告</h1><p>__STAMP__ 北京时间获取 · 纽约时间 __NYSTAMP__<br>Yahoo Finance 复权日线 · QQQ / GOOGL / NVDA / AAPL / MSFT / ASML / COST</p></header><main>__WARNINGS__<div class="toolbar"><div class="tabs" role="tablist"><button role="tab" aria-selected="true" data-mode="closed">已收盘评分</button><button role="tab" aria-selected="false" data-mode="intraday">盘中试算 · 未确认</button></div><div class="filters"><input type="search" aria-label="搜索股票代码" placeholder="搜索代码"><label><input type="checkbox" id="qualified"> 只看≥70分</label></div></div>__PANELS__<footer>计算方法：BOLL(20,2)，样本标准差；RSI(14)，EW平滑；MACD(12,26,9)，柱值=2×(DIF−DEA)。<br>QQQ使用指数评分档，其他标的使用个股档。70分只是技术规则阈值，不代表胜率或买入指令。Yahoo行情可能延迟。<br>此文件是本地快照；按“刷新评分报告.cmd”重新导入更新。没有QQ推送，也没有付费AI调用。<br><a href="__DATAFILE__" download>下载本次原始日线数据 JSON</a></footer></main><script>
const panels=[...document.querySelectorAll('.panel')];const search=document.querySelector('input[type=search]');const qualified=document.querySelector('#qualified');function filter(){const q=search.value.trim().toUpperCase();for(const panel of panels){const rows=[...panel.querySelectorAll('.filter-row')];for(const row of rows)row.hidden=!row.dataset.symbol.includes(q)||(qualified.checked&&Number(row.dataset.score)<70);panel.querySelector('.empty').hidden=rows.some(r=>!r.hidden)}}for(const button of document.querySelectorAll('[data-mode]'))button.addEventListener('click',()=>{for(const b of document.querySelectorAll('[data-mode]'))b.setAttribute('aria-selected',String(b===button));for(const panel of panels)panel.hidden=panel.id!=='panel-'+button.dataset.mode;filter()});search.addEventListener('input',filter);qualified.addEventListener('change',filter);for(const link of document.querySelectorAll('a[href^="#"]'))link.addEventListener('click',()=>{const detail=document.getElementById(link.hash.slice(1));if(detail)detail.open=true});
</script></body></html>'''.replace("__STAMP__", escape(stamp)).replace("__NYSTAMP__", escape(ny_stamp)).replace("__WARNINGS__", warning_html).replace("__PANELS__", "".join(panels)).replace("__DATAFILE__", escape(data_filename, quote=True))


def save_report(snapshot, output, data_output=None):
    output = Path(output)
    data_output = Path(data_output) if data_output else output.with_suffix(".json")
    analysis = analyze_snapshot(snapshot)
    if not analysis["reports"]["closed"]:
        raise RuntimeError("没有可用的完整日线，未覆盖已有报告。")
    output.parent.mkdir(parents=True, exist_ok=True)
    data_output.parent.mkdir(parents=True, exist_ok=True)
    relative_data = os.path.relpath(data_output, output.parent).replace(os.sep, "/")
    html = render_report(snapshot, analysis, relative_data)
    data_output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    output.write_text(html, encoding="utf-8")
    for r in sorted(analysis["reports"]["closed"], key=lambda x:x["total"], reverse=True):
        print(f"{r['ticker']} {r['date']} {r['total']}/100", flush=True)
    print(f"REPORT: {output.resolve()}", flush=True)
    return analysis


def main(argv=None):
    parser = argparse.ArgumentParser(description="美股本地评分报告（不发邮件、不调用AI）")
    parser.add_argument("--source", choices=("cloud", "yahoo"), default="cloud", help="默认导入GitHub最新公开快照；yahoo直接更新行情")
    parser.add_argument("--input-json", type=Path, help="从已保存的行情快照离线重算")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "stock-report-latest.html")
    parser.add_argument("--data-output", type=Path)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    snapshot = json.loads(args.input_json.read_text(encoding="utf-8")) if args.input_json else (fetch_cloud_snapshot() if args.source == "cloud" else fetch_yahoo_snapshot())
    analysis = save_report(snapshot, args.output, args.data_output)
    if not args.no_open:
        webbrowser.open(args.output.resolve().as_uri())
    if analysis["errors"]:
        raise SystemExit("部分行情未完成：" + "; ".join(analysis["errors"]))


if __name__ == "__main__":
    main()

