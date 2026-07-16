def _ema(closes, span):
    return closes.ewm(span=span, adjust=False).mean()


def _rsi14(closes, period=14):
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def get_indicators(bars):
    indicators = {}
    for symbol in bars.index.get_level_values(0).unique():
        closes = bars.loc[symbol]["close"]

        ema9 = _ema(closes, 9)
        ema21 = _ema(closes, 21)
        ema50 = _ema(closes, 50)
        ema200 = _ema(closes, 200)
        rsi14 = _rsi14(closes)

        prev_diff = ema9.iloc[-2] - ema21.iloc[-2]
        curr_diff = ema9.iloc[-1] - ema21.iloc[-1]
        if prev_diff <= 0 and curr_diff > 0:
            cross = "UP"
        elif prev_diff >= 0 and curr_diff < 0:
            cross = "DOWN"
        else:
            cross = "NONE"

        close = closes.iloc[-1]
        ema200_latest = ema200.iloc[-1]

        indicators[symbol] = {
            "close": close,
            "ema9": ema9.iloc[-1],
            "ema21": ema21.iloc[-1],
            "ema50": ema50.iloc[-1],
            "ema200": ema200_latest,
            "rsi14": rsi14.iloc[-1],
            "cross": cross,
            "pct_from_ema200": (close / ema200_latest - 1) * 100,
        }
    return indicators


def get_regime(spy_bars, qqq_bars):
    spy_close = spy_bars["close"]
    qqq_close = qqq_bars["close"]

    spy_above = spy_close.iloc[-1] > _ema(spy_close, 50).iloc[-1]
    qqq_above = qqq_close.iloc[-1] > _ema(qqq_close, 50).iloc[-1]

    if spy_above and qqq_above:
        return "RISK_ON"
    if not spy_above and not qqq_above:
        return "RISK_OFF"
    return "MIXED"


def generate_signal(indicators, regime):
    if indicators["cross"] == "UP" and indicators["rsi14"] < 65 and regime != "RISK_OFF":
        return "BUY"
    if indicators["cross"] == "DOWN":
        return "SELL"
    return "HOLD"
