"""Shared BOLL/RSI/MACD indicators and deterministic scoring rules."""
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd


WATCHLIST = ["QQQ", "GOOGL", "NVDA", "AAPL", "MSFT", "ASML", "COST"]


SCORE_THRESHOLD = 70


NEW_YORK = ZoneInfo("America/New_York")


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


def completed_daily_bars(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    index = pd.DatetimeIndex(df.index)
    index = index.tz_localize(NEW_YORK) if index.tz is None else index.tz_convert(NEW_YORK)
    return df[index.date < now.date()] if now.hour < 16 else df[index.date <= now.date()]

