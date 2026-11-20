"""Email templates using inline styles and tables for QQ and mobile clients."""
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

NAMES = {"QQQ": "纳斯达克100 ETF", "GOOGL": "谷歌", "NVDA": "英伟达", "AAPL": "苹果",
         "MSFT": "微软", "ASML": "阿斯麦", "COST": "开市客", "LLY": "礼来"}

def report_status(scanned, issues=()):
    fresh = [r for r in scanned if r.get("fresh", True)]
    if not scanned:
        return "未能完成评分，请检查行情服务"
    if not fresh:
        return "暂无新的完整日线，展示最近数据"
    qualified = sum(r["eligible"] for r in fresh)
    if qualified:
        return f"{qualified}只达到70分及以上"
    if issues or len(fresh) != len(scanned):
        return "已更新且成功评分的标的均不足70分，扫描不完整"
    return "今日无70分及以上标的"


def render_email(reports, scanned, *, preview=False, issues=()):
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    marker = "版式测试 · 以下为虚构示例数据" if preview else report_status(scanned, issues)
    plain = [f"美股低吸评分报告 | {stamp} 北京时间", marker, ""]
    notices = list(issues)
    if any(not r.get("fresh", True) for r in scanned):
        notices.append("部分或全部标的暂无新的纽约当日完整日线（可能休市或数据延迟）；以下标注实际数据日期，旧评分仅供参考，不重复调用 AI。")
    plain.extend([*notices, ""])
    notice_html = "".join(f'<div style="margin-top:8px;font-size:13px;line-height:1.8;color:#8a571b">{escape(n)}</div>' for n in notices)
    cards = []
    for r in sorted(reports, key=lambda x: x["total"], reverse=True):
        ticker, total = r["ticker"], r["total"]
        color = "#0e7666" if total >= 80 else "#a45e12"
        rows = []
        for p in r["parts"]:
            width = round(p["score"] / p["max"] * 100)
            bar = f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#edf1f5"><tr><td width="{max(width,1)}%" height="6" style="background:{color if width else "#edf1f5"};font-size:1px">&nbsp;</td>'
            if width < 100:
                bar += f'<td width="{100-width}%" style="font-size:1px">&nbsp;</td>'
            bar += '</tr></table>'
            rows.append(f"""<tr><td width="28%" style="padding:12px 0;border-top:1px solid #edf1f5;font-size:13px;color:#52647a">{escape(p['name'])}</td>
<td width="18%" style="padding:12px 8px;border-top:1px solid #edf1f5;font-weight:bold;color:{color}">{p['score']}<span style="font-size:11px;color:#8795a7"> / {p['max']}</span></td>
<td style="padding:12px 0;border-top:1px solid #edf1f5">{bar}<div style="margin-top:6px;font-size:12px;line-height:1.6;color:#67788c">{escape(p['reason'])}</div></td></tr>""")
        advice = escape(r.get("advice", "AI研判暂不可用。")).replace("\n", "<br>")
        cards.append(f"""<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#fff;border:1px solid #e0e7ef;border-radius:14px;margin-bottom:20px"><tr><td style="padding:24px 20px">
<table role="presentation" width="100%"><tr><td><div style="font-size:12px;color:#8795a7">{'指数档' if ticker == 'QQQ' else '个股档'} · {escape(r['date'])}</div>
<div style="margin-top:6px;font-size:25px;font-weight:bold;color:#142a46">{escape(ticker)} <span style="font-size:13px;font-weight:normal;color:#65768a">{escape(NAMES.get(ticker,ticker))}</span></div>
<div style="margin-top:8px;font-size:13px;color:#65768a">复权收盘价 <strong style="color:#243c58">${r['close']:,.2f}</strong></div></td>
<td align="right"><div style="font-size:38px;font-weight:bold;line-height:1.1;color:{color}">{total}<span style="font-size:12px;color:#8795a7"> /100</span></div><div style="margin-top:8px;font-size:12px;font-weight:bold;color:{color}">{escape(r['tier'])}</div></td></tr></table>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:20px">{''.join(rows)}</table>
<div style="margin-top:14px;padding:12px;background:#f4f7fa;border-radius:8px;font-size:12px;line-height:1.8;color:#52647a">RSI <strong>{r['rsi']:.1f}</strong> &nbsp;·&nbsp; BOLL下轨 <strong>${r['boll_lower']:,.2f}</strong><br>BOLL中轨 ${r['boll_mid']:,.2f}</div>
<div style="margin-top:18px;padding:16px;background:#f0f5fa;border-left:3px solid #365e8b;border-radius:4px"><div style="font-size:12px;font-weight:bold;color:#365e8b;margin-bottom:8px">AI研判</div><div style="font-size:14px;line-height:1.9;color:#243c58;word-break:break-word">{advice}</div></div>
</td></tr></table>""")
        plain.extend([f"{ticker} {NAMES.get(ticker,ticker)} | {total}/100 | {r['tier']}",
                      f"数据日期 {r['date']} | 复权收盘价 ${r['close']:.2f} | RSI {r['rsi']:.1f}"])
        plain.extend(f"{p['name']} {p['score']}/{p['max']}：{p['reason']}" for p in r["parts"])
        plain.extend([r.get("advice", "AI研判暂不可用。"), ""])
    overview = []
    for r in sorted(scanned, key=lambda x:x["total"], reverse=True):
        tone = "#0e7666" if r["eligible"] else "#7b8a9c"
        date_label = r['date'] + (" · 历史数据" if not r.get("fresh", True) else "")
        overview.append(f"""<tr><td style="padding:10px 0;border-bottom:1px solid #edf1f5;font-size:13px;color:#243c58">{escape(r['ticker'])} <span style="font-size:11px;color:#8795a7">{escape(NAMES.get(r['ticker'],''))}</span><div style="font-size:11px;color:#8795a7;margin-top:4px">{escape(date_label)}</div></td><td align="right" style="padding:10px 0;border-bottom:1px solid #edf1f5;font-weight:bold;color:{tone}">{r['total']} / 100</td></tr>""")
    plain.extend(["自选评分一览：", *(f"{r['ticker']} {r['total']}/100 | {r['date']}" + ("（历史数据）" if not r.get("fresh", True) else "") for r in sorted(scanned,key=lambda x:x["total"],reverse=True)), "", "分数反映技术规则匹配程度，不代表实际胜率。复权价格可能与报价页面不同。"])
    highest = max((r["total"] for r in scanned), default=0)
    qualified = sum(r["eligible"] and r.get("fresh", True) for r in scanned)
    formatted = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>美股低吸评分报告</title></head>
<body style="margin:0;padding:0;background:#edf2f7;font-family:Arial,'Microsoft YaHei',sans-serif;color:#243c58">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#edf2f7"><tr><td align="center" style="padding:24px 10px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:660px"><tr><td style="background:#142a46;padding:28px 22px;border-radius:16px 16px 0 0">
<div style="font-size:11px;letter-spacing:3px;color:#8eb3cf">US EQUITY · DAILY WATCH</div><div style="margin-top:12px;font-size:25px;font-weight:bold;color:#fff">美股低吸评分报告</div>
<div style="margin-top:10px;font-size:12px;line-height:1.8;color:#b7c9db">{stamp} 北京时间<br>{escape(marker)}</div></td></tr>
<tr><td style="background:#fff;padding:20px 12px;border:1px solid #e0e7ef;border-top:0;border-radius:0 0 14px 14px">
<table role="presentation" width="100%"><tr><td align="center" width="33%"><div style="font-size:25px;font-weight:bold;color:#142a46">{len(scanned)}</div><div style="font-size:11px;color:#8795a7;margin-top:5px">已评分标的</div></td>
<td align="center" width="34%" style="border-left:1px solid #edf1f5;border-right:1px solid #edf1f5"><div style="font-size:25px;font-weight:bold;color:#0e7666">{qualified}</div><div style="font-size:11px;color:#8795a7;margin-top:5px">新日线达到70分</div></td>
<td align="center" width="33%"><div style="font-size:25px;font-weight:bold;color:#142a46">{highest}</div><div style="font-size:11px;color:#8795a7;margin-top:5px">本次最高分</div></td></tr></table></td></tr>
<tr><td style="padding-top:20px">{notice_html}{''.join(cards)}</td></tr>
<tr><td style="background:#fff;border:1px solid #e0e7ef;border-radius:14px;padding:20px"><div style="font-size:14px;font-weight:bold;color:#142a46;margin-bottom:8px">自选评分一览</div><table width="100%" cellspacing="0" cellpadding="0">{''.join(overview)}</table></td></tr>
<tr><td style="padding:18px 10px;font-size:11px;line-height:1.9;color:#8795a7">70–79分：轻仓试探 · 80–100分：黄金坑候选<br>分数反映技术规则匹配程度，不代表实际胜率。复权价格可能与报价页面不同。</td></tr>
</table></td></tr></table></body></html>"""
    return "\n".join(plain), formatted

def sample_reports():
    reports = []
    for ticker, values in [("QQQ", [30,25,25]), ("COST", [25,35,10])]:
        total = sum(values)
        reports.append({"ticker":ticker, "date":"示例日期", "close":101.0,
                        "rsi":35.0 if ticker == "QQQ" else 28.0,
                        "boll_lower":100.0,"boll_mid":110.0,"total":total,"eligible":True,
                        "tier":"黄金坑候选" if total >=80 else "轻仓试探",
                        "parts":[{"name":name,"score":value,"max":maximum,"reason":reason}
                                 for name,value,maximum,reason in zip(
                                     ["BOLL位置","RSI超卖","MACD企稳"],values,[40,35,25],
                                     ["示例：价格接近下轨","示例：RSI进入超卖区","示例：MACD企稳信号"])],
                        "advice":"形态：这是版式测试，价格和得分均为虚构。\n观察：实际提醒会展示DeepSeek对达标标的的研判。\n风险：示例不构成实际交易信号。"})
    return reports


