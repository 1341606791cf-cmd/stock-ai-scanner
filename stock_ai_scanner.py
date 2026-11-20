"""Scoring formulas; the default command opens a local report, without email or AI."""

import argparse
import os
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from openai import OpenAI
from email_reports import render_email, report_status, sample_reports
from scoring_core import WATCHLIST, SCORE_THRESHOLD, NEW_YORK, calculate_indicators, score_stock, completed_daily_bars


AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.deepseek.com")
AI_MODEL = os.getenv("AI_MODEL", "deepseek-flash")
yf.set_tz_cache_location(str(Path(__file__).parent / "work" / "yfinance-cache"))






def ask_ai_analyst(ticker: str, df: pd.DataFrame, score: dict) -> str:
    if not score["eligible"]:
        raise RuntimeError("低于 70 分的标的不调用 AI。")
    api_key = os.getenv("AI_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 AI_API_KEY，无法生成 AI 研判。")
    recent = df[["Close", "BOLL_UPPER", "BOLL_MID", "BOLL_LOWER", "RSI",
                 "MACD_DIF", "MACD_DEA", "MACD_HIST"]].tail(5)
    details = '; '.join(f"{p['name']} {p['score']}/{p['max']}，{p['reason']}" for p in score['parts'])
    prompt = f"""你是一位美股技术分析助手。用户只做 1 倍现货长线低吸，不加杠杆。
标的：{ticker}，数据日期：{score['date']}。总分 {score['total']}/100，分层为“{score['tier']}”。
分项：{details}。评分表示规则匹配程度，不是实际胜率；不要把“黄金坑候选”说成必涨。
最近 5 个交易日的收盘价与技术指标：
{recent.round(2).to_string()}
请用 200 字以内中文分三行回答，纯文本输出：
形态：当前技术形态。
观察：分批低吸或继续观察，给出基于表内数据的确认价位或指标条件。
风险：一个关键失效条件或继续下跌风险。不要承诺收益。"""
    client = OpenAI(api_key=api_key, base_url=AI_BASE_URL, timeout=30.0)
    response = client.chat.completions.create(
        model=AI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError(f"{ticker} 的 AI 响应为空。")
    return content.strip()


def send_qq_email(subject: str, content: str, html_content: str | None = None) -> None:
    qq_email = os.getenv("QQ_EMAIL", "").strip()
    auth_code = os.getenv("QQ_AUTH_CODE", "").strip()
    if not qq_email or not auth_code:
        raise RuntimeError("请配置 QQ_EMAIL 和 QQ_AUTH_CODE（QQ 邮箱 SMTP 授权码）。")
    if not qq_email.lower().endswith("@qq.com") or "\n" in qq_email or "\r" in qq_email:
        raise RuntimeError("QQ_EMAIL 需为有效的 @qq.com 地址。")
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(content, "plain", "utf-8"))
    if html_content:
        msg.attach(MIMEText(html_content, "html", "utf-8"))
    msg["From"] = formataddr((str(Header("美股监控助手", "utf-8")), qq_email))
    msg["To"] = qq_email
    msg["Subject"] = str(Header(subject, "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=20) as server:
            server.login(qq_email, auth_code)
            server.sendmail(qq_email, [qq_email], msg.as_string())
    except (smtplib.SMTPException, OSError):
        # SMTP 异常可能包含账号信息，不将其写入公开 Actions 日志。
        raise RuntimeError("QQ 邮件发送失败；请检查 SMTP 服务、授权码和网络。") from None




def is_today_in_new_york(df: pd.DataFrame) -> bool:
    last = pd.Timestamp(df.index[-1])
    if last.tzinfo is None:
        last = last.tz_localize(NEW_YORK)
    return last.tz_convert(NEW_YORK).date() == datetime.now(NEW_YORK).date()


def run_scan(require_today: bool = False) -> int:
    if not os.getenv("QQ_EMAIL") or not os.getenv("QQ_AUTH_CODE"):
        raise RuntimeError("请配置 QQ_EMAIL 和 QQ_AUTH_CODE 两个邮件环境变量。")
    print(f"开始扫描 {len(WATCHLIST)} 个标的；每次发送汇总，仅新日线 >= 70 分时调用 AI。", flush=True)
    alerts = []
    scanned = []
    failed = []
    issues = []
    for ticker in WATCHLIST:
        try:
            df = yf.Ticker(ticker).history(period="3mo", interval="1d", auto_adjust=True, timeout=15)
            df = completed_daily_bars(df)
            if df.empty or len(df) < 30:
                raise RuntimeError("日线数据不足 30 根")
            df = calculate_indicators(df)
            score = score_stock(ticker, df)
            score["fresh"] = not require_today or is_today_in_new_york(df)
            scanned.append(score)
            points = '/'.join(str(p['score']) for p in score['parts'])
            print(f"[{ticker}] 日期 {score['date']} | 复权收盘 ${score['close']:.2f} | "
                  f"BOLL/RSI/MACD {points} | 总分 {score['total']}/100 | {score['tier']}", flush=True)
            if not score["fresh"]:
                print(f"[{ticker}] 无新的纽约当日完整日线，仅展示历史评分，不重复调用 AI。")
            elif score['eligible']:
                try:
                    advice = ask_ai_analyst(ticker, df, score)
                except Exception as exc:
                    # AI 故障也要发送评分，且不在邮件或日志中泄露上游异常文本。
                    advice = "AI 研判暂不可用；本邮件仍展示实际技术指标和评分。"
                    issues.append(f"{ticker}：AI 研判失败（{type(exc).__name__}）")
                    failed.append(ticker)
                alerts.append({**score, 'advice': advice})
        except Exception as exc:
            # 不记录含凭证的上游异常文本。
            print(f"[{ticker}] 扫描或研判失败：{type(exc).__name__}")
            failed.append(ticker)
            issues.append(f"{ticker}：行情读取或评分失败（{type(exc).__name__}）")
    plain, formatted = render_email(alerts, scanned, issues=issues)
    send_qq_email(f"美股每日扫描 | {report_status(scanned, issues)}", plain, formatted)
    print(f"已发送每日汇总：{report_status(scanned, issues)}。", flush=True)
    if failed:
        raise RuntimeError(f"以下标的未完成扫描：{', '.join(failed)}")
    return len(alerts)


def main() -> None:
    from local_stock_report import main as local_main
    local_main()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(f"错误：{exc}")

