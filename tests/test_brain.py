from bot import brain

# G13's proper NORMAL/DANGER regime state machine isn't built yet
# (bot/regime.py — see research/v1-architecture.md §4.5's flagged gap).
# RISK_OFF from signals.get_regime() is the closest existing analog to a
# DANGER regime, used here as-is rather than inventing a "DANGER" string
# nothing downstream would recognize.
_DANGER_REGIME_BRIEFING_TEXT = """Regime: RISK_OFF | Breaker: FROZEN_WEEK | Sleeve utilization: MOMENTUM 0% QUALITY_VALUE 0% DEFENSIVE 0%

Macro headlines:
  macro: Fed Chair Warns Of Deepening Recession Risk As Credit Spreads Blow Out (0.5h, benzinga)
  macro: VIX Spikes To Multi-Year High As Global Markets Enter Freefall (0.8h, benzinga)
  macro: Regional Banking Fears Resurface, Contagion Concerns Mount (1.2h, benzinga)
  macro: Treasury Yields Plunge As Investors Flee To Safety (2.1h, benzinga)
  macro: S&P 500 Down 12% In Two Weeks, Worst Selloff Since 2020 (3.0h, benzinga)

Legend: SYMBOL $close | 1d/5d/30d %chg | EMA9-vs-21 state + days since cross | %vs EMA200 | RSI14 | vol vs 20d avg | 52w range pctile | nbhd: benchmark %chg 5d, relative strength (candidate 30d% - benchmark 30d%), peer confirmation | pos: sleeve unrealized% days-held (held positions only)
NVDA $95.2 | 1d -8.1% 5d -22.4% 30d -35.0% | EMA9<21 bear 14d | -38% vs E200 | RSI 18 | vol 4.2x | 52w pctile 1 | nbhd: XLK -19.5% 5d, RS -30.1, peers 0/2 up
TSLA $140.5 | 1d -9.8% 5d -25.1% 30d -40.2% | EMA9<21 bear 12d | -42% vs E200 | RSI 15 | vol 5.1x | 52w pctile 0 | nbhd: XLY -21.0% 5d, RS -35.0, peers 0/2 up
QQQ $410.0 | 1d -6.5% 5d -18.2% 30d -28.0% | EMA9<21 bear 15d | -30% vs E200 | RSI 20 | vol 3.5x | 52w pctile 2 | nbhd: SPY -14.0% 5d, RS -12.0, peers 0/0 up
TLT $102.4 | 1d +2.1% 5d +6.8% 30d +11.2% | EMA9>21 bull 9d | +9% vs E200 | RSI 68 | vol 2.0x | 52w pctile 92 | nbhd: SPY -14.0% 5d, RS +25.1, peers 0/0 up
GLD $245.7 | 1d +1.8% 5d +5.4% 30d +9.7% | EMA9>21 bull 11d | +12% vs E200 | RSI 71 | vol 1.8x | 52w pctile 95 | nbhd: SPY -14.0% 5d, RS +23.6, peers 0/0 up
SH $48.9 | 1d +6.4% 5d +18.9% 30d +27.5% | EMA9>21 bull 14d | +25% vs E200 | RSI 74 | vol 2.9x | 52w pctile 98 | nbhd: SPY -14.0% 5d, RS +41.5, peers 0/0 up

Ticker news:
  news: NVIDIA Slashes Guidance As AI Capex Freeze Spreads Across Hyperscalers (0.3h, benzinga)
  news summary (NVDA): Major hyperscaler customers reportedly pausing AI infrastructure orders amid recession fears, threatening NVIDIA's core growth narrative.
  news: Tesla Delivery Miss Compounds Broader Consumer Spending Collapse Fears (0.6h, benzinga)
  news summary (TSLA): Consumer discretionary spending data points to a sharper-than-expected slowdown, with Tesla deliveries missing estimates by a wide margin.
  news: Treasury Rally Extends As Flight-To-Safety Bid Intensifies (1.0h, benzinga)
  news: Gold Hits Fresh Highs As Investors Seek Safe Havens Amid Selloff (1.4h, benzinga)
  news: Inverse ETFs See Record Inflows As Traders Position For Further Declines (2.0h, benzinga)

Forecast track record:
  fc acc 30d: n/a (no scored forecasts yet)
  fc acc 30d: n/a (no scored forecasts yet)
  fc acc 30d: n/a (no scored forecasts yet)
  fc acc 30d: n/a (no scored forecasts yet)
  fc acc 30d: n/a (no scored forecasts yet)
  fc acc 30d: n/a (no scored forecasts yet)

Coverage rule: return exactly one entry in `candidates` for each of the 6 symbols listed above (NVDA, TSLA, QQQ, TLT, GLD, SH), in that order. Do not omit any — a HOLD with low confidence is a valid, complete entry; skipping a symbol is not."""

_CANDIDATE_COUNT = 6


def test_danger_regime_no_new_momentum_buys_and_tilts_defensive():
    """Live-calls the real Sonnet 5 API — no mock. The point is to verify
    the persona's actual behavior under stress (does "paranoid about regime
    shifts" hold up against an obvious crash + flight-to-safety scenario),
    not just that the plumbing runs.
    """
    briefing = {
        "text": _DANGER_REGIME_BRIEFING_TEXT,
        "sections": {
            "global": "",
            "stat_lines": "",
            "headlines": "",
            "macro": "",
            "forecast_accuracy": "",
            "instructions": "",
        },
    }

    result, shadow = brain.run_full_analysis(briefing, _CANDIDATE_COUNT)

    assert shadow is None, "SHADOW_MODEL is unset — no shadow result should be produced"
    assert not result["fallback"], "brain call failed or was refused — see logged reason"

    candidates = result["candidates"]
    assert len(candidates) == _CANDIDATE_COUNT

    momentum_buys = [
        c["symbol"]
        for c in candidates
        if c["proposal"]["action"] == "BUY" and c["proposal"]["sleeve"] == "MOMENTUM"
    ]
    assert not momentum_buys, f"expected no new MOMENTUM buys in a DANGER-analog regime, got: {momentum_buys}"

    buys = [c for c in candidates if c["proposal"]["action"] == "BUY"]
    non_defensive_buys = [c["symbol"] for c in buys if c["proposal"]["sleeve"] != "DEFENSIVE"]
    assert not non_defensive_buys, (
        f"expected any BUYs in a DANGER-analog regime to be DEFENSIVE-sleeve only, "
        f"got non-DEFENSIVE buys: {non_defensive_buys}"
    )
