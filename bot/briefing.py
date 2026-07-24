import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from bot.config import (
    DEFAULT_SLEEVE,
    NEWS_SPIKE_SUMMARY_CAP,
    SECTOR_ETF_MAP,
    SECTOR_MAP,
)
from bot.journal import position_opened_date, read_entries
from bot.screener import HEADLINE_SPIKE_COUNT
from bot.signals import ema

logger = logging.getLogger("bot.briefing")

FORECAST_ACCURACY_PATH = Path("logs") / "forecast_accuracy.json"

_ETF_TO_SECTOR = {etf: sector for sector, etf in SECTOR_ETF_MAP.items()}
_FALLBACK_BENCHMARK = "SPY"

LEGEND = (
    "Legend: SYMBOL $close | 1d/5d/30d %chg | EMA9-vs-21 state + days since cross | "
    "%vs EMA200 | RSI14 | vol vs 20d avg | 52w range pctile | "
    "nbhd: benchmark %chg 5d, relative strength (candidate 30d% - benchmark 30d%), peer confirmation | "
    "pos: sleeve unrealized% days-held (held positions only)"
)


def _pct_change(closes, n):
    if len(closes) <= n:
        return float("nan")
    return (closes.iloc[-1] / closes.iloc[-1 - n] - 1) * 100


def _days_since_cross(closes, max_lookback=60):
    """Days since EMA9/21 last flipped sign, and the current bull/bear state.

    Recomputed here (not read from signals.get_indicators, which only
    carries *today's* cross event) because the stat-line needs the age of
    the current trend, not just whether today happened to be a crossing day.
    """
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    diff = ema9 - ema21
    state = "bull" if diff.iloc[-1] > 0 else "bear"

    lookback = min(max_lookback, len(diff) - 1)
    current_sign = diff.iloc[-1] > 0
    for age in range(1, lookback + 1):
        if (diff.iloc[-1 - age] > 0) != current_sign:
            return state, str(age)
    return state, f"{max_lookback}+"


def _volume_ratio(bars_df):
    volume = bars_df["volume"]
    if len(volume) < 21:
        return float("nan")
    trailing_avg = volume.iloc[-21:-1].mean()
    if trailing_avg == 0:
        return float("nan")
    return volume.iloc[-1] / trailing_avg


def _52w_percentile(closes):
    window = closes.iloc[-min(252, len(closes)):]
    low, high = window.min(), window.max()
    if high == low:
        return 50.0
    return (closes.iloc[-1] - low) / (high - low) * 100


def _neighborhood(symbol, bars):
    """Sector ETF trend + relative strength + peer confirmation.

    Every SECTOR_MAP sector has a dedicated ETF as of the 2026-07-24 build
    review (config.SECTOR_ETF_MAP), so the SPY fallback below only fires for
    non-sector ETFs (broad/bond/gold/international/hedge) — those have no
    "3 stocks from that sector" to use as peers at all, which is a different,
    still-real gap (not something more ETF coverage can close).
    """
    candidate_closes = bars.loc[symbol]["close"]
    candidate_pct_30d = _pct_change(candidate_closes, 30)

    if symbol in _ETF_TO_SECTOR:
        sector = _ETF_TO_SECTOR[symbol]
        peers = [s for s in SECTOR_MAP if SECTOR_MAP[s] == sector]
        benchmark = _FALLBACK_BENCHMARK
    else:
        sector = SECTOR_MAP.get(symbol)
        etf = SECTOR_ETF_MAP.get(sector) if sector else None
        if etf is not None:
            benchmark = etf
            peers = [s for s in SECTOR_MAP if SECTOR_MAP[s] == sector and s != symbol][:2]
        else:
            benchmark = _FALLBACK_BENCHMARK
            peers = []

    benchmark_closes = bars.loc[benchmark]["close"]
    benchmark_pct_5d = _pct_change(benchmark_closes, 5)
    benchmark_pct_30d = _pct_change(benchmark_closes, 30)
    relative_strength = candidate_pct_30d - benchmark_pct_30d

    peers_up = 0
    for peer in peers:
        peer_pct_5d = _pct_change(bars.loc[peer]["close"], 5)
        if peer_pct_5d > 0:
            peers_up += 1

    return (
        f"nbhd: {benchmark} {benchmark_pct_5d:+.1f}% 5d, "
        f"RS {relative_strength:+.1f}, peers {peers_up}/{len(peers)} up"
    )


def _position_line(symbol, positions_by_symbol, journal_entries):
    position = positions_by_symbol.get(symbol)
    if position is None:
        return None
    unrealized_pct = float(position.unrealized_plpc) * 100
    opened_at = position_opened_date(journal_entries, symbol)
    days_held = "n/a"
    if opened_at:
        days_held = str((datetime.now(timezone.utc) - datetime.fromisoformat(opened_at)).days)
    # No positions_state.json yet (lands with sleeve tagging in a later
    # step) — every held position is reported under DEFAULT_SLEEVE until then.
    return f"pos: {DEFAULT_SLEEVE} {unrealized_pct:+.1f}% {days_held}d"


def _stat_line(symbol, universe_data, bars, positions_by_symbol, journal_entries):
    indicators = universe_data[symbol]
    closes = bars.loc[symbol]["close"]

    pct_5d = _pct_change(closes, 5)
    pct_30d = _pct_change(closes, 30)
    cross_state, cross_days = _days_since_cross(closes)
    vol_ratio = _volume_ratio(bars.loc[symbol])
    pctile = _52w_percentile(closes)

    segments = [
        f"{symbol} ${indicators['close']:.1f}",
        f"1d {indicators['pct_1d']:+.1f}% 5d {pct_5d:+.1f}% 30d {pct_30d:+.1f}%",
        f"EMA9{'>' if cross_state == 'bull' else '<'}21 {cross_state} {cross_days}d",
        f"{indicators['pct_from_ema200']:+.0f}% vs E200",
        f"RSI {indicators['rsi14']:.0f}",
        f"vol {vol_ratio:.1f}x",
        f"52w pctile {pctile:.0f}",
        _neighborhood(symbol, bars),
    ]
    position_line = _position_line(symbol, positions_by_symbol, journal_entries)
    if position_line:
        segments.append(position_line)

    return " | ".join(segments)


def _headline_lines(symbol, news_by_symbol, include_summary):
    headlines = news_by_symbol.get(symbol, [])
    if not headlines:
        return []
    lines = [f"  news: {h['title']} ({h['age_hours']}h, {h['source']})" for h in headlines]
    if include_summary and headlines:
        lines.append(f"  news summary ({symbol}): {headlines[0]['summary']}")
    return lines


def load_forecast_accuracy():
    """{symbol: {"n": int, "s_hit_pct": float, "m_hit_pct": float}}.

    Populated by scorecard.py (not built yet — that's a daily, pure-code
    scoring job with a hard read-dependency here). Until it exists this
    always returns {}, and every candidate's line reads "n/a".
    """
    if not FORECAST_ACCURACY_PATH.exists():
        return {}
    with open(FORECAST_ACCURACY_PATH) as f:
        return json.load(f)


def _forecast_accuracy_line(symbol, forecast_accuracy):
    stats = forecast_accuracy.get(symbol)
    if not stats:
        return "  fc acc 30d: n/a (no scored forecasts yet)"
    return (
        f"  fc acc 30d: S {stats['s_hit_pct']:.0f}% M {stats['m_hit_pct']:.0f}% "
        f"n={stats['n']}"
    )


def build_briefing(
    candidates,
    universe_data,
    bars,
    positions,
    news_by_symbol,
    macro_headlines,
    regime,
    breaker_status,
    sleeve_utilization,
):
    """Assemble the full user-message text for brain.py's single API call.

    Returns {"text": ..., "sections": {...}} — sections holds the raw text
    of each §5 T-rule category (global block, stat-lines, headlines, macro)
    so brain.py can log per-section token estimates without recomputing
    them (T1/T3/T4/T5).
    """
    positions_by_symbol = {p.symbol: p for p in positions}
    forecast_accuracy = load_forecast_accuracy()
    journal_entries = read_entries()

    utilization_str = " ".join(f"{sleeve} {pct:.0f}%" for sleeve, pct in sleeve_utilization.items())
    global_block = f"Regime: {regime} | Breaker: {breaker_status} | Sleeve utilization: {utilization_str}"

    macro_lines = [f"  macro: {h['title']} ({h['age_hours']}h, {h['source']})" for h in macro_headlines]
    macro_text = "\n".join(macro_lines) if macro_lines else "  macro: (none in window)"

    # Up to NEWS_SPIKE_SUMMARY_CAP candidates get their top headline's full
    # summary (T5) — the ones the screener actually flagged as news-spikes,
    # ranked by headline count, never more than the cap regardless of how
    # many candidates qualify.
    spike_candidates = sorted(
        (s for s in candidates if len(news_by_symbol.get(s, [])) >= HEADLINE_SPIKE_COUNT),
        key=lambda s: len(news_by_symbol.get(s, [])),
        reverse=True,
    )[:NEWS_SPIKE_SUMMARY_CAP]

    stat_lines = []
    headline_lines = []
    fc_lines = []
    for symbol in candidates:
        stat_lines.append(_stat_line(symbol, universe_data, bars, positions_by_symbol, journal_entries))
        headline_lines.extend(_headline_lines(symbol, news_by_symbol, symbol in spike_candidates))
        fc_lines.append(_forecast_accuracy_line(symbol, forecast_accuracy))

    stat_text = "\n".join(stat_lines)
    headlines_text = "\n".join(headline_lines) if headline_lines else "  news: (none in window)"
    fc_text = "\n".join(fc_lines)

    coverage_instruction = (
        f"Coverage rule: return exactly one entry in `candidates` for each of the "
        f"{len(candidates)} symbols listed above ({', '.join(candidates)}), in that "
        f"order. Do not omit any — a HOLD with low confidence is a valid, complete "
        f"entry; skipping a symbol is not."
    )

    text = (
        f"{global_block}\n\n"
        f"Macro headlines:\n{macro_text}\n\n"
        f"{LEGEND}\n"
        f"{stat_text}\n\n"
        f"Ticker news:\n{headlines_text}\n\n"
        f"Forecast track record:\n{fc_text}\n\n"
        f"{coverage_instruction}"
    )

    return {
        "text": text,
        "sections": {
            "global": global_block,
            "stat_lines": stat_text,
            "headlines": headlines_text,
            "macro": macro_text,
            "forecast_accuracy": fc_text,
            "instructions": coverage_instruction,
        },
    }
