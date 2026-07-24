import argparse
import logging
import sys
import traceback

from bot import brain
from bot.breakers import TRADING_OK, check_circuit_breakers
from bot.briefing import build_briefing
from bot.config import DEFAULT_SLEEVE, HEADLINES_PER_TICKER_CAP, HEADLINES_MACRO_CAP, RISK, UNIVERSE
from bot.data import get_daily_bars
from bot.execute import execute_order, get_equity, get_positions, stop_loss_sweep
from bot.journal import count_trades_today, journal_error, journal_run, journal_token_usage, read_entries, symbols_in_cooldown
from bot.news import get_macro_headlines, get_ticker_headlines
from bot.screener import select_candidates
from bot.signals import generate_signal, get_indicators, get_regime

# EMA200 needs ~2-3x its period of history to converge (see signals.py's step-2
# rationale), and the briefing's 52-week percentile needs ~252 trading days —
# both want more than the v0 flow's days=250 calendar days (~178 trading days,
# short of even one EMA200 period). The full/dry-run path uses its own,
# larger fetch rather than "fixing" the live v0 flow's window in this pass.
FULL_RUN_BAR_DAYS = 400

logger = logging.getLogger("bot.main")

SLEEVE = "MOMENTUM"
# Pinned at 6% (NOT RISK["momentum_position_pct"], which is 10%) — per the
# 2026-07-24 build review, this v0 EMA9/21-crossover signal is semantically
# MOMENTUM, not QUALITY_VALUE, but it keeps its original TACTICAL-era 6%
# size until step 13 retires v0 execution in favor of the full
# screener/briefing/brain flow. Deriving the size from momentum_position_pct
# here would silently 1.67x every v0 trade the moment that RISK key gets
# tuned for the real MOMENTUM sleeve, with no corresponding review of
# whether v0 should trade at that size at all — no silent size jump.
POSITION_SIZE_PCT = 0.06


def _pregate(status, symbol, action, usd_amount, sleeve, reason):
    logger.info("%s %s %s $%.2f (%s): %s", status, action, symbol, usd_amount, sleeve, reason)
    return {
        "status": status,
        "symbol": symbol,
        "action": action,
        "usd_amount": usd_amount,
        "sleeve": sleeve,
        "reason": reason,
        "quote": None,
    }


def _run():
    symbols = UNIVERSE["STOCKS"] + UNIVERSE["ETFS"] + UNIVERSE["HEDGE"]

    journal_entries = read_entries()
    equity = get_equity()
    breaker_status = check_circuit_breakers(journal_entries, equity)
    if breaker_status != TRADING_OK:
        logger.warning("Circuit breaker active: %s — BUYs will be rejected this run.", breaker_status)

    # Stop-losses run unconditionally, breaker or not: they only reduce risk.
    results = stop_loss_sweep()

    held_symbols = {p.symbol for p in get_positions()}
    trades_today_count = count_trades_today(journal_entries, action="BUY")
    cooldown_symbols = symbols_in_cooldown(journal_entries, RISK["ticker_cooldown_days"], action="BUY")

    # Sized off live account equity, not a hardcoded dollar figure, so this
    # tracks deposits/withdrawals/resets on the paper account automatically.
    buy_usd_amount = equity * POSITION_SIZE_PCT

    bars = get_daily_bars(symbols, days=250)

    indicators = get_indicators(bars)
    regime = get_regime(bars.loc["SPY"], bars.loc["QQQ"])

    print(f"Regime: {regime}  Breaker: {breaker_status}\n")
    header = (
        f"{'Symbol':<8}{'Close':>10}{'EMA9':>10}{'EMA21':>10}{'EMA50':>10}"
        f"{'EMA200':>10}{'%vsEMA200':>11}{'RSI14':>8}{'Cross':>8}{'Signal':>8}"
    )
    print(header)
    print("-" * len(header))

    analyzed = {}
    for symbol in symbols:
        ind = indicators[symbol]
        signal = generate_signal(ind, regime)
        analyzed[symbol] = {**ind, "signal": signal}
        print(
            f"{symbol:<8}{ind['close']:>10.2f}{ind['ema9']:>10.2f}{ind['ema21']:>10.2f}"
            f"{ind['ema50']:>10.2f}{ind['ema200']:>10.2f}{ind['pct_from_ema200']:>10.1f}%"
            f"{ind['rsi14']:>8.1f}{ind['cross']:>8}{signal:>8}"
        )

        if signal == "BUY":
            if symbol in held_symbols:
                results.append(
                    _pregate(
                        "SKIPPED_ALREADY_HELD",
                        symbol,
                        "BUY",
                        buy_usd_amount,
                        SLEEVE,
                        "already holding a position in this ticker",
                    )
                )
            elif breaker_status != TRADING_OK:
                results.append(
                    _pregate(
                        "REJECTED",
                        symbol,
                        "BUY",
                        buy_usd_amount,
                        SLEEVE,
                        f"BREAKER_DEFENSIVE ({breaker_status})",
                    )
                )
            elif symbol in cooldown_symbols:
                results.append(
                    _pregate(
                        "SKIPPED_COOLDOWN",
                        symbol,
                        "BUY",
                        buy_usd_amount,
                        SLEEVE,
                        f"bought within the last {RISK['ticker_cooldown_days']}d cooldown window",
                    )
                )
            elif trades_today_count >= RISK["max_trades_per_day"]:
                results.append(
                    _pregate(
                        "SKIPPED_TRADE_LIMIT",
                        symbol,
                        "BUY",
                        buy_usd_amount,
                        SLEEVE,
                        f"daily trade limit reached ({RISK['max_trades_per_day']})",
                    )
                )
            else:
                result = execute_order(symbol, "BUY", buy_usd_amount, SLEEVE)
                results.append(result)
                if result["status"] == "SUBMITTED":
                    trades_today_count += 1
        elif signal == "SELL" and symbol in held_symbols:
            position = next(p for p in get_positions() if p.symbol == symbol)
            results.append(execute_order(symbol, "SELL", float(position.market_value), SLEEVE))

    print("\nExecution summary:")
    if not results:
        print("  no actions taken")
    for r in results:
        if r["status"] == "SUBMITTED":
            size = f"notional=${r['notional']:.2f}" if r["notional"] is not None else f"qty={r['qty']}"
            print(
                f"  {r['symbol']:<6} {r['action']:<4} SUBMITTED {size} "
                f"bid={r['quote']['bid']:.2f} ask={r['quote']['ask']:.2f} order_id={r['order_id']}"
            )
        elif r["status"].startswith("SKIPPED_"):
            print(f"  {r['symbol']:<6} {r['action']:<4} {r['status']} — {r['reason']}")
        else:
            print(f"  {r['symbol']:<6} {r['action']:<4} REJECTED — {r['reason']}")

    final_positions = get_positions()
    final_equity = get_equity()
    journal_run(regime, analyzed, results, final_positions, final_equity, breaker_status)


# Rough chars-per-token heuristic (~4:1 for English prose), good enough to
# spot budget drift (§5) without pulling in a tokenizer just to print a debug view.
def _estimate_tokens(text):
    return round(len(text) / 4)


def _print_headline_line(headline):
    line = f"{headline['title']} | {headline['age_hours']}h | {headline['source']}"
    print(f"    {line}  (~{_estimate_tokens(line)} tok)")
    return _estimate_tokens(line)


def _print_news():
    symbols = UNIVERSE["STOCKS"] + UNIVERSE["ETFS"] + UNIVERSE["HEDGE"]

    print("=== Ticker headlines (last 24h, cap 5/ticker) ===")
    ticker_headlines = get_ticker_headlines(symbols, hours=24, cap=5)
    ticker_tokens = 0
    quiet_count = 0
    for symbol, headlines in ticker_headlines.items():
        if not headlines:
            quiet_count += 1
            continue
        print(f"  {symbol}:")
        for headline in headlines:
            ticker_tokens += _print_headline_line(headline)
    print(f"  ({quiet_count}/{len(symbols)} tickers with no headlines in window)")
    print(f"  subtotal: ~{ticker_tokens} tok (summaries excluded from this estimate — prompt-excluded by default)\n")

    print("=== Macro headlines (last 24h, cap 8) ===")
    macro_headlines = get_macro_headlines(hours=24, cap=8)
    macro_tokens = 0
    for headline in macro_headlines:
        macro_tokens += _print_headline_line(headline)
    print(f"  subtotal: ~{macro_tokens} tok\n")

    print(f"TOTAL headline tokens (prompt estimate): ~{ticker_tokens + macro_tokens} tok")


def _sleeve_utilization(positions, equity):
    # Every position is reported under DEFAULT_SLEEVE until positions_state.json
    # sleeve tagging lands (step 13) — same known gap as briefing._position_line.
    invested = {"MOMENTUM": 0.0, "QUALITY_VALUE": 0.0, "DEFENSIVE": 0.0}
    for p in positions:
        invested[DEFAULT_SLEEVE] += float(p.market_value)
    if not equity:
        return {sleeve: 0.0 for sleeve in invested}
    return {sleeve: value / equity * 100 for sleeve, value in invested.items()}


def _print_forecast(label, forecast):
    print(
        f"      {label}: {forecast['direction']:<5} conf={forecast['confidence']:.2f} — {forecast['rationale']}"
    )


def _print_brain_result(label, result):
    print(f"\n--- {label} ({result['model']}) ---")
    if result["fallback"]:
        print("  FALLBACK_HOLD — no usable output this run (see log for reason)")
        return
    for candidate in result["candidates"]:
        proposal = candidate["proposal"]
        print(f"  {candidate['symbol']}: {proposal['action']} {proposal['sleeve']} ${proposal['usd_amount']:.0f} conf={proposal['confidence']:.2f}")
        print(f"      thesis: {proposal['thesis']}")
        print(f"      falsifier: {proposal['falsifier']}")
        print(f"      risks: {', '.join(proposal['risks'])}")
        _print_forecast("1w", candidate["forecasts"]["h1w"])
        _print_forecast("1m", candidate["forecasts"]["h1m"])
        _print_forecast("3m", candidate["forecasts"]["h3m"])

    if result["usage"] is not None:
        usage = result["usage"]
        print(
            f"\n  tokens: input={usage['input_tokens']} output={usage['output_tokens']} "
            f"cache_read={usage['cache_read_input_tokens']} cache_write={usage['cache_creation_input_tokens']}"
        )
        for section, tokens in result["section_estimates"].items():
            print(f"    {section}: ~{tokens} tok")


def _dry_run():
    symbols = UNIVERSE["STOCKS"] + UNIVERSE["ETFS"] + UNIVERSE["HEDGE"]

    journal_entries = read_entries()
    equity = get_equity()
    breaker_status = check_circuit_breakers(journal_entries, equity)
    positions = get_positions()

    bars = get_daily_bars(symbols, days=FULL_RUN_BAR_DAYS)
    universe_data = get_indicators(bars)
    regime = get_regime(bars.loc["SPY"], bars.loc["QQQ"])

    ticker_headlines = get_ticker_headlines(symbols, cap=HEADLINES_PER_TICKER_CAP)
    macro_headlines = get_macro_headlines(cap=HEADLINES_MACRO_CAP)

    candidates, scores = select_candidates(universe_data, positions, ticker_headlines)
    sleeve_utilization = _sleeve_utilization(positions, equity)

    briefing = build_briefing(
        candidates,
        universe_data,
        bars,
        positions,
        ticker_headlines,
        macro_headlines,
        regime,
        breaker_status,
        sleeve_utilization,
    )

    print(f"Regime: {regime}  Breaker: {breaker_status}  Candidates: {len(candidates)}\n")
    print(briefing["text"])

    full_result, shadow_result = brain.run_full_analysis(briefing, len(candidates))
    _print_brain_result("FULL run", full_result)
    if full_result["usage"] is not None:
        journal_token_usage("FULL", full_result["model"], full_result["section_estimates"], full_result["usage"])

    if shadow_result is not None:
        _print_brain_result("SHADOW run (not executed)", shadow_result)
        if shadow_result["usage"] is not None:
            journal_token_usage(
                "SHADOW", shadow_result["model"], shadow_result["section_estimates"], shadow_result["usage"]
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--news",
        action="store_true",
        help="print ticker + macro headlines with token-count estimates, then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run screener -> briefing -> brain (full) and print results without executing, then exit",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.news:
        _print_news()
        return

    if args.dry_run:
        _dry_run()
        return

    try:
        _run()
    except Exception as exc:
        journal_error(exc)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
