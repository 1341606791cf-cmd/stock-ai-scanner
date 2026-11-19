"""Weekday QQ summaries; only fresh scores >= 70 call the AI analyst."""

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


WATCHLIST = ["QQQ", "GOOGL", "NVDA", "AAPL", "MSFT", "ASML", "COST"]
SCORE_THRESHOLD = 70
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.deepseek.com")
AI_MODEL = os.getenv("AI_MODEL", "deepseek-flash")
NEW_YORK = ZoneInfo("America/New_York")
yf.set_tz_cache_location(str(Path(__file__).parent / "work" / "yfinance-cache"))


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate BOLL(20,2), RSI(14), and MACD(12,26,9)."""
    df = df.copy()
    close = df["Close"].astype(float)
    df["BOLL_MID"] = close.rolling(window=20).mean()
    std = close.rolling(window=20).std()
    df["BOLL_UPPER"] = df["BOLL_MID"] + 2 * std
    df["BOLL_LOWER"] = df["BOLL_MID"] - 2 * std

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    df["RSI"] = 100 - (100 / (1 + rs))
    df.loc[(loss == 0) & (gain > 0), "RSI"] = 100.0
    df.loc[(loss == 0) & (gain == 0), "RSI"] = 50.0

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["MACD_DIF"] = ema12 - ema26
    df["MACD_DEA"] = df["MACD_DIF"].ewm(span=9, adjust=False).mean()
    df["MACD_HIST"] = (df["MACD_DIF"] - df["MACD_DEA"]) * 2
    return df


def score_stock(ticker: str, df: pd.DataFrame) -> dict:
    """Each component takes its highest matching band; only QQQ uses index bands."""
    if len(df) < 3:
        raise RuntimeError("评分至少需要三根日线。")
    columns = ["Close", "BOLL_LOWER", "BOLL_MID", "RSI", "MACD_DIF", "MACD_DEA", "MACD_HIST"]
    if df.iloc[-3:][columns].isna().any().any():
        raise RuntimeError("评分指标不完整。")
    last, prev, before = df.iloc[-1], df.iloc[-2], df.iloc[-3]
    is_index = ticker == "QQQ"
    close, lower, rsi = float(last.Close), float(last.BOLL_LOWER), float(last.RSI)
    if close < lower:
        boll, boll_reason = 40, "价格跌破 BOLL 下轨"
    elif close <= lower * (1.01 if is_index else 1.015) * (1 + 1e-12):
        boll = 30 if is_index else 25
        boll_reason = f"价格距离下轨不超过 {'1%' if is_index else '1.5%'}"
    elif close < last.BOLL_MID:
        boll, boll_reason = (15 if is_index else 10), "价格跌破 BOLL 中轨"
    else:
        boll, boll_reason = 0, "价格未进入低吸位置区间"
    if rsi < (32 if is_index else 30):
        rsi_points, rsi_reason = 35, "RSI 严重超卖"
    elif rsi < (38 if is_index else 35):
        rsi_points, rsi_reason = 25, "RSI 进入超卖区"
    elif rsi < (43 if is_index else 40):
        rsi_points, rsi_reason = (15 if is_index else 10), "RSI 处于回调 / 磨底区"
    else:
        rsi_points, rsi_reason = 0, "RSI 未达到超卖评分区间"
    if prev.MACD_DIF <= prev.MACD_DEA and last.MACD_DIF > last.MACD_DEA:
        macd, macd_reason = 25, "MACD 日线形成金叉"
    elif before.MACD_HIST < prev.MACD_HIST < last.MACD_HIST < 0:
        macd, macd_reason = 20, "负柱连续两天缩短"
    elif prev.MACD_HIST < last.MACD_HIST < 0:
        macd, macd_reason = 10, "负柱缩短一天"
    else:
        macd, macd_reason = 0, "MACD 暂无企稳加分信号"
    total = boll + rsi_points + macd
    return {"ticker": ticker, "date": pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d"),
            "close": close, "rsi": rsi, "boll_lower": lower, "boll_mid": float(last.BOLL_MID),
            "total": total, "eligible": total >= SCORE_THRESHOLD,
            "tier": "黄金坑候选" if total >= 80 else "轻仓试探" if total >= SCORE_THRESHOLD else "继续观察",
            "parts": [{"name": "BOLL 位置", "score": boll, "max": 40, "reason": boll_reason},
                      {"name": "RSI 超卖", "score": rsi_points, "max": 35, "reason": rsi_reason},
                      {"name": "MACD 企稳", "score": macd, "max": 25, "reason": macd_reason}]}


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


def completed_daily_bars(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    index = pd.DatetimeIndex(df.index)
    index = index.tz_localize(NEW_YORK) if index.tz is None else index.tz_convert(NEW_YORK)
    return df[index.date < now.date()] if now.hour < 16 else df[index.date <= now.date()]


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
    parser = argparse.ArgumentParser(description="美股日线技术指标和 AI 买点提醒")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--require-today", action="store_true", help="仅纽约当日完整日线调用 AI；旧日线标注日期并发送状态汇总")
    mode.add_argument("--test-email", action="store_true", help="发送标有虚构数据的 HTML 版式测试邮件")
    mode.add_argument("--preview-email", type=Path, help="保存 HTML 示例，不联网也不发信")
    args = parser.parse_args()
    if args.test_email or args.preview_email:
        examples = sample_reports()
        plain, formatted = render_email(examples, examples, preview=True)
        if args.preview_email:
            args.preview_email.parent.mkdir(parents=True, exist_ok=True)
            args.preview_email.write_text(formatted, encoding='utf-8')
            print(f"HTML 示例已保存：{args.preview_email}")
        else:
            send_qq_email("版式测试 | 美股低吸评分报告（虚构示例）", plain, formatted)
            print("HTML 测试邮件已发送。")
    else:
        run_scan(require_today=args.require_today)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(f"错误：{exc}")
