"""Scan US stocks after market close and email one QQ alert for buy signals."""

import argparse
import os
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from openai import OpenAI


WATCHLIST = ["QQQ", "GOOGL", "NVDA", "AAPL", "MSFT"]
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


def check_buy_signals(df: pd.DataFrame) -> list[str]:
    latest, prev = df.iloc[-1], df.iloc[-2]
    signals = []
    if latest["Close"] <= latest["BOLL_LOWER"] * 1.015:
        signals.append("触及/跌破 BOLL 下轨支撑位")
    if latest["RSI"] < 35:
        signals.append(f"RSI 处于低位超卖区 ({latest['RSI']:.1f})")
    if prev["MACD_DIF"] < prev["MACD_DEA"] and latest["MACD_DIF"] > latest["MACD_DEA"]:
        signals.append("MACD 日线形成金叉")
    elif latest["MACD_HIST"] < 0 and latest["MACD_HIST"] > prev["MACD_HIST"]:
        signals.append("MACD 下跌绿柱缩短，空头动能减弱")
    return signals


def ask_ai_analyst(ticker: str, df: pd.DataFrame, signals: list[str]) -> str:
    api_key = os.getenv("AI_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 AI_API_KEY，无法生成 AI 研判。")
    recent = df[["Close", "BOLL_UPPER", "BOLL_MID", "BOLL_LOWER", "RSI",
                 "MACD_DIF", "MACD_DEA", "MACD_HIST"]].tail(5)
    prompt = f"""你是一位美股技术分析助手。用户只做 1 倍现货长线低吸，不加杠杆。
标的：{ticker}。初筛信号：{', '.join(signals)}。
最近 5 个交易日的收盘价与技术指标：
{recent.round(2).to_string()}
请用 150 字以内的中文说明：当前技术形态；是否适合分批低吸或继续观望；
若观望，等待什么价格或指标确认。说明技术信号有误判风险，不要承诺收益。"""
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


def send_qq_email(subject: str, content: str) -> None:
    qq_email = os.getenv("QQ_EMAIL", "").strip()
    auth_code = os.getenv("QQ_AUTH_CODE", "").strip()
    if not qq_email or not auth_code:
        raise RuntimeError("请配置 QQ_EMAIL 和 QQ_AUTH_CODE（QQ 邮箱 SMTP 授权码）。")
    if not qq_email.lower().endswith("@qq.com") or "\n" in qq_email or "\r" in qq_email:
        raise RuntimeError("QQ_EMAIL 需为有效的 @qq.com 地址。")
    msg = MIMEText(content, "plain", "utf-8")
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
    if not os.getenv("AI_API_KEY") or not os.getenv("QQ_EMAIL") or not os.getenv("QQ_AUTH_CODE"):
        raise RuntimeError("请配置 AI_API_KEY、QQ_EMAIL 和 QQ_AUTH_CODE 三个环境变量。")
    print("开始扫描美股自选列表...")
    alerts = []
    failed = []
    for ticker in WATCHLIST:
        try:
            df = yf.Ticker(ticker).history(period="3mo", interval="1d", auto_adjust=True)
            if df.empty or len(df) < 30:
                raise RuntimeError("日线数据不足 30 根")
            if require_today and not is_today_in_new_york(df):
                print(f"[{ticker}] 今日美股日线尚未更新，跳过推送。")
                continue
            df = calculate_indicators(df)
            latest = df.iloc[-1]
            if pd.isna(latest[["BOLL_LOWER", "RSI", "MACD_HIST"]]).any():
                raise RuntimeError("最新技术指标不完整")
            signals = check_buy_signals(df)
            print(f"[{ticker}] 收盘价 ${latest['Close']:.2f} | RSI {latest['RSI']:.1f} "
                  f"| BOLL 下轨 ${latest['BOLL_LOWER']:.2f} | 信号 {', '.join(signals) if signals else '无'}")
            if signals:
                advice = ask_ai_analyst(ticker, df, signals)
                alerts.append(f"【{ticker}】最新价 ${latest['Close']:.2f} | RSI {latest['RSI']:.1f}\n"
                              f"触发：{', '.join(signals)}\nAI 研判：{advice}")
        except Exception as exc:
            # 不记录含凭证的上游异常文本。
            print(f"[{ticker}] 扫描或研判失败：{type(exc).__name__}")
            failed.append(ticker)
    if alerts:
        send_qq_email(f"美股买点提醒：{len(alerts)} 只标的触发信号", "\n\n".join(alerts))
        print(f"已发送 {len(alerts)} 个标的的 QQ 邮件研判。")
    else:
        print("无可推送的买点信号。")
    if failed:
        raise RuntimeError(f"以下标的未完成扫描：{', '.join(failed)}")
    return len(alerts)


def main() -> None:
    parser = argparse.ArgumentParser(description="美股日线技术指标和 AI 买点提醒")
    parser.add_argument("--require-today", action="store_true", help="定时运行时只处理纽约当日收盘数据")
    parser.add_argument("--test-email", action="store_true", help="仅发送一封 QQ 测试邮件，不调用行情或 AI")
    args = parser.parse_args()
    if args.test_email:
        send_qq_email("美股扫描器测试", "GitHub Actions 的手动触发和 QQ SMTP 邮件已连通。")
        print("测试邮件已发送。")
    else:
        run_scan(require_today=args.require_today)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(f"错误：{exc}")
